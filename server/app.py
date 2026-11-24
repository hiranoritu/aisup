"""HTTP サーバ（標準ライブラリのみ）。仕様 v1.2：遠隔・完全非同期。

    python -m server [--config server_config.json] [--data data/] [--port 8000] [--host 127.0.0.1]

学習者画面: /?t=<参加コード>（コードがなければ何も見えない）   管理画面: /admin（X-Admin-Token = 環境変数 ADMIN_TOKEN）
死活監視: /healthz
API キーはサーバの環境変数だけに置き、ブラウザには渡さない（仕様 §10）。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import mimetypes
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from engine.config import load_config, make_config
from engine.structure import get_analyzer
from engine.core import replay
from engine.events import dump_jsonl
from engine.export import export

from .fake_llm import make_fake_llm
from .llm import LLMClient, OpenAIClient
from .prompts import load_prompt
from .sandbox import DockerSandbox, SandboxConfig, verify_program
from .session import BadRequest, Busy, NotSynced, OtherPage, ParticipantSession
from .settings import abspath, load_server_config, server_config_hash, validate_server_config
from .store import EventStore
from .support import LLM_SUPPORT_TYPES, SupportGenerator
from .tasks import load_tasks, task_ids_from_phases

WEB = abspath("web")
MATERIALS = abspath("materials")
PHASE_TITLES = {"consent": "研究参加への同意", "envcheck": "環境の確認", "intro": "練習（AIの使い方）",
                "pretest": "事前テスト", "main": "課題", "post": "課題（後半）", "posttest": "事後テスト",
                "questionnaire": "アンケート"}
PHASE_KINDS = {"consent", "envcheck", "tasks", "test", "questionnaire"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


class App:
    def __init__(self, server_cfg: dict, data_dir: str, engine_cfg: Optional[dict] = None,
                 llm: Optional[LLMClient] = None, sandbox: Optional[DockerSandbox] = None,
                 admin_token: Optional[str] = None, clock=time.time) -> None:
        self.server_cfg = server_cfg
        self.server_cfg_hash = server_config_hash(server_cfg)
        self.engine_cfg = engine_cfg or make_config()
        self._unpriced_models: set[str] = set()
        # 構文解析器の版が設定と違えばここで止める（参加者を受け付けてから失敗しない）
        get_analyzer(self.engine_cfg["structure_analyzer"], self.engine_cfg.get("structure_analyzer_versions"))
        self.clock = clock
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        self.store = EventStore(os.path.join(data_dir, "events.sqlite3"))
        # 実験の流れ・課題・同意書・テスト・質問紙
        self.phases: list[dict] = server_cfg["phases"]
        self.phase_index = {p["id"]: i for i, p in enumerate(self.phases)}
        self.docs: dict[str, dict] = {}
        self.instruments: dict[str, dict] = {}
        for ph in self.phases:
            if ph["kind"] not in PHASE_KINDS:
                raise ValueError(f"phase {ph['id']}: unknown kind {ph['kind']}")
            if ph.get("doc"):
                with open(abspath(ph["doc"]), encoding="utf-8") as f:
                    text = f.read()
                self.docs[ph["doc"]] = {"text": text, "version": _sha(text)}
            if ph.get("instrument"):
                with open(abspath(ph["instrument"]), encoding="utf-8") as f:
                    raw = f.read()
                inst = json.loads(raw)
                ids = [it["id"] for it in inst["items"]]
                if len(set(ids)) != len(ids):
                    raise ValueError(f"{ph['instrument']}: duplicate item id")
                inst["version"] = _sha(raw)
                self.instruments[ph["instrument"]] = inst
        self.tasks = load_tasks(abspath(server_cfg["tasks_dir"]), task_ids_from_phases(self.phases))
        self.task_ids = [t.task_id for t in self.tasks]
        self.task_by_id = {t.task_id: t for t in self.tasks}
        pc = server_cfg["prompts"]
        self.prompts = {"learner_system": load_prompt(abspath(pc["learner_system"])),
                        "features": load_prompt(abspath(pc["features"])),
                        "grade": load_prompt(abspath(pc["grade"]))}
        for st in LLM_SUPPORT_TYPES:
            self.prompts[st] = load_prompt(os.path.join(abspath(pc["support_dir"]), f"{st}.md"))
        if llm is None:
            if server_cfg["llm_provider"] == "fake":
                llm = make_fake_llm(self.tasks)
            else:
                llm = OpenAIClient(os.environ["OPENAI_API_KEY"], server_cfg["llm"], server_cfg["openai_base_url"])
        self.llm = llm
        self.sandbox = sandbox or DockerSandbox(SandboxConfig(**server_cfg["sandbox"]))
        self.generator = SupportGenerator(self.llm, self.prompts, self.sandbox, server_cfg)
        self.admin_token = admin_token
        # 教材（内容のハッシュを版として閲覧イベントとエクスポートに記録する）
        self.materials = []
        for m in server_cfg.get("materials", []):
            with open(abspath(m["file"]), encoding="utf-8") as f:
                text = f.read()
            self.materials.append({**m, "content": text, "version": _sha(text)})
        self.material_by_id = {m["id"]: m for m in self.materials}
        # ブロック無作為化の割付表（§15.5）
        self.allocation: list[dict] = []
        self.allocation_hash: Optional[str] = None
        alloc_path = abspath(server_cfg["allocation_file"]) if server_cfg.get("allocation_file") else None
        if alloc_path and os.path.exists(alloc_path):
            with open(alloc_path, encoding="utf-8") as f:
                raw = f.read()
            self.allocation = [r for r in csv.DictReader(io.StringIO(raw))]
            self.allocation_hash = _sha(raw)
        self._judge: dict[str, list[dict]] = {}
        self._judge_lock = threading.Lock()
        self._sessions: dict[str, ParticipantSession] = {}
        self._slock = threading.Lock()
        self._stop = threading.Event()
        self.alerts: list[dict] = []
        # 再起動時：保存済みイベントを再生して全参加者の状態を復元する
        for p in self.store.participants():
            self.session(p["participant_id"])

    def session(self, pid: str) -> ParticipantSession:
        with self._slock:
            s = self._sessions.get(pid)
            if s is None:
                info = next((p for p in self.store.participants() if p["participant_id"] == pid), None)
                if info is None:
                    raise KeyError(pid)
                s = ParticipantSession(self, info, self.clock)
                s.restore(self.store.events(pid))
                self._sessions[pid] = s
            return s

    def phase_title(self, ph: dict) -> str:
        return ph.get("title") or PHASE_TITLES.get(ph["id"], ph["id"])

    # ---------------------------------------------------------------- 採点・費用・通知
    def judge_inputs(self, task) -> list[dict]:
        """採点用の入力と期待出力。期待出力は参照解をサンドボックスで実行した結果（初回に作って保持）。"""
        with self._judge_lock:
            if task.task_id not in self._judge:
                pub = list(dict.fromkeys(task.sample_inputs))
                hid = [x for x in dict.fromkeys(task.hidden_inputs) if x not in pub]
                v = verify_program(self.sandbox, task.reference_solution, pub + hid,
                                   int(self.server_cfg["support"]["verify_repeats"]))
                if not v.ok:
                    raise RuntimeError(f"reference solution of {task.task_id} failed: {v.reason}")
                self._judge[task.task_id] = ([{"stdin": s, "expected": v.outputs[s], "public": True} for s in pub]
                                             + [{"stdin": s, "expected": v.outputs[s], "public": False} for s in hid])
            return self._judge[task.task_id]

    def background(self, fn) -> None:
        """応答を待たせない裏の処理（比較用の特徴抽出など）。テストでは同期実行に差し替える。"""
        threading.Thread(target=fn, daemon=True).start()

    def llm_cost(self, model: str, usage: dict) -> float:
        prices = self.server_cfg.get("llm_prices", {})
        price = next((v for k, v in prices.items() if model == k or model.startswith(k + "-")), None)
        if price is None:
            if model in ("", "fake", "scripted") or not prices:
                return 0.0
            # 料金表にないモデル：費用の上限が効かなくならないよう、表で最も高い料金で見積もり、一度だけ通知する
            price = [max(v[0] for v in prices.values()), max(v[1] for v in prices.values())]
            if model not in self._unpriced_models:
                self._unpriced_models.add(model)
                self.notify(f"料金表（llm_prices）にないモデル {model} が応答しました。最も高い料金で見積もっています")
        return (float(usage.get("prompt_tokens") or 0) * price[0]
                + float(usage.get("completion_tokens") or 0) * price[1]) / 1_000_000

    def notify(self, message: str) -> None:
        """実験者への通知。管理画面に出し、ALERT_WEBHOOK_URL があれば送る（Slack 等の互換 JSON）。"""
        self.alerts.append({"t": self.clock(), "message": message})
        del self.alerts[:-200]
        sys.stderr.write(f"[ALERT] {message}\n")
        url = os.environ.get("ALERT_WEBHOOK_URL")
        if url:
            def send():
                try:
                    req = urllib.request.Request(url, data=json.dumps({"text": message}).encode("utf-8"),
                                                 headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=10).close()
                except OSError as e:
                    sys.stderr.write(f"[ALERT] webhook failed: {e}\n")
            threading.Thread(target=send, daemon=True).start()

    def on_session_end(self, s: ParticipantSession) -> None:
        self.backup(f"end_{s.pid}")
        self.notify(f"{s.pid}: すべてのフェーズに到達しました")

    def backup(self, tag: str) -> Optional[str]:
        """SQLite のオンラインバックアップ（追記中でも一貫した複製）。サーバ外への複製は deploy/ のスクリプト。"""
        d = os.path.join(self.data_dir, "backups")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"events_{time.strftime('%Y%m%d_%H%M%S')}_{tag}.sqlite3")
        try:
            src = sqlite3.connect(os.path.join(self.data_dir, "events.sqlite3"))
            dst = sqlite3.connect(path)
            with dst:
                src.backup(dst)
            dst.close()
            src.close()
            return path
        except sqlite3.Error as e:
            self.notify(f"バックアップに失敗しました: {e}")
            return None

    # ---------------------------------------------------------------- 定期処理
    def start_ticker(self) -> threading.Thread:
        def loop():
            while not self._stop.wait(float(self.server_cfg["session"]["tick_s"])):
                for s in list(self._sessions.values()):
                    try:
                        s.tick()
                    except Exception as e:  # noqa: BLE001  1人の不具合で全体を止めない
                        self.notify(f"{s.pid}: 定期処理でエラー {type(e).__name__}: {e}")
        th = threading.Thread(target=loop, daemon=True, name="ticker")
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()

    # ---------------------------------------------------------------- 管理
    def create_participant(self, pid: Optional[str], group: str = "auto", note: str = "") -> dict[str, Any]:
        """参加コードの発行。group="auto" は割付表の次の枠（ブロック無作為化）を使う。"""
        existing = self.store.participants()
        slot = None
        if group == "auto":
            used = {p["slot"] for p in existing if p.get("slot") is not None}
            free = [r for r in self.allocation if int(r["slot"]) not in used]
            if not free:
                raise BadRequest("割付表がない、または使い切った（python -m server.allocation で作る）")
            slot = int(free[0]["slot"])
            group = free[0]["group"]
            assignment = "block_randomized"
        else:
            assignment = "manual"
        if group not in ("adaptive", "uniform"):
            raise BadRequest("group must be auto|adaptive|uniform")
        ids = {p["participant_id"] for p in existing}
        if not pid:
            k = len(ids) + 1
            while f"P{k:03d}" in ids:
                k += 1
            pid = f"P{k:03d}"
        if pid in ids:
            raise BadRequest("participant exists")
        if not all(c.isalnum() or c in "-_" for c in pid):
            raise BadRequest("participant_id: alnum, - and _ only")
        token = secrets.token_urlsafe(18)
        self.store.add_participant(pid, token, group, self.clock(), note, slot)
        s = self.session(pid)
        s.start_session(assignment, slot)
        return {"participant_id": pid, "group": group, "token": token, "assignment": assignment, "slot": slot}

    def overview(self) -> list[dict[str, Any]]:
        out = []
        now = self.clock()
        for p in self.store.participants():
            s = self.session(p["participant_id"])
            with s.lock:
                ph = s.phase()
                dl = s.deadline()
                out.append({
                    "participant_id": p["participant_id"], "group": p["group"], "token": p["token"],
                    "slot": p.get("slot"), "note": p["note"], "status": s.status,
                    "phase": ph["id"] if ph else None, "task_id": s.task.task_id if s.task else None,
                    "remaining_s": None if dl is None or s.status != "active" else max(0, int(dl - now)),
                    "last_t": s.last_t, "idle_s": int(now - s.last_t) if s.last_t else None,
                    "cycles": len(s.engine.cycles), "decisions": len(s.engine.decisions),
                    "interventions": sum(1 for c in s.engine.cycles if c.intervened)
                                     + (1 if s.engine.open is not None and s.engine.open.intervened else 0),
                    "support_active": s.support["support_type"] if s.support else None,
                    "busy": s.flow.locked(), "anomalies": len(s.engine.anomalies), "errors": s.errors,
                    "questions": s.questions, "cost_usd": round(s.cost_usd, 3),
                    "shadow_cost_usd": round(s.shadow_cost_usd, 3), "limits": sorted(s.limits_hit),
                    "contaminated_cycles": sum(1 for c in s.engine.cycles if c.features.get("contamination_suspected")),
                    "states": [c.state for c in s.engine.cycles],
                })
        return out

    def export_zip(self) -> bytes:
        """全参加者のイベントログ（JSONL）と、ログ再生による判定結果（CSV/JSON）を zip で返す。

        判定結果は保存済みイベントの再生から作る（仕様 §10：本番と同じ規則・LLM を呼ばない）。
        """
        buf = io.BytesIO()
        tmp = os.path.join(self.data_dir, "export_tmp", str(int(self.clock() * 1000)))
        os.makedirs(tmp, exist_ok=True)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps({
                "exported_t": self.clock(), "rule_version": self.engine_cfg["rule_version"],
                "server_config": self.server_cfg, "server_config_hash": self.server_cfg_hash,
                "prompts": {k: {"path": os.path.relpath(p.path, abspath(".")), "version": p.version}
                            for k, p in self.prompts.items()},
                "materials": [{"id": m["id"], "file": m["file"], "version": m["version"]} for m in self.materials],
                "documents": {k: v["version"] for k, v in self.docs.items()},
                "instruments": {k: v["version"] for k, v in self.instruments.items()},
                "allocation_file_hash": self.allocation_hash,
                "participants": [{k: v for k, v in p.items() if k != "token"} for p in self.store.participants()],
            }, ensure_ascii=False, indent=2))
            feat_rows: list[dict] = []
            for p in self.store.participants():
                pid = p["participant_id"]
                evs = self.store.events(pid)
                path = os.path.join(tmp, f"{pid}_events.jsonl")
                dump_jsonl(evs, path)
                z.write(path, f"events/{pid}_events.jsonl")
                feat_rows.extend(features_rows(pid, evs))
                eng = replay(evs, self.engine_cfg)
                for _k, fp in export(eng, os.path.join(tmp, "judgments"), pid).items():
                    z.write(fp, f"judgments/{os.path.basename(fp)}")
            z.writestr("summary/features_compare.csv", _csv_text(FEATURE_COLS, feat_rows))
        return buf.getvalue()


FEATURE_COLS = ["participant_id", "prompt_event_id", "task_id", "text"] + [
    f"{side}_{k}" for side in ("main", "shadow") for k in
    ("model", "prompt_version", "ok", "h1", "h2", "h3", "h4", "is_followup", "followup_confidence",
     "asks_about_answer", "off_task", "latency_ms")]


def features_rows(pid: str, evs) -> list[dict]:
    """質問ごとに、判定に使ったモデルと比較用モデル（features_shadow）の特徴を並べる（人手ラベルとの比較用）。"""
    calls = {e.event_id: e.payload for e in evs if e.type == "LLM_CALL"}
    rows: dict[str, dict] = {}
    for e in evs:
        if e.type == "PROMPT_DRAFT_SUBMIT":
            rows[e.event_id] = {"participant_id": pid, "prompt_event_id": e.event_id, "task_id": e.task_id,
                                "text": e.payload.get("text", "")}
        elif e.type in ("PROMPT_FEATURES", "PROMPT_FEATURES_SHADOW"):
            r = rows.get(e.payload.get("prompt_event_id"))
            if r is None:
                continue
            side = "main" if e.type == "PROMPT_FEATURES" else "shadow"
            f = e.payload.get("features") or {}
            call = calls.get(e.payload.get("llm_call_id"), {})
            r.update({f"{side}_model": e.payload.get("model"), f"{side}_prompt_version": e.payload.get("prompt_version"),
                      f"{side}_ok": e.payload.get("ok"), f"{side}_latency_ms": call.get("latency_ms"),
                      **{f"{side}_{k}": f.get(k) for k in ("h1", "h2", "h3", "h4", "is_followup",
                                                           "followup_confidence", "asks_about_answer", "off_task")}})
    return list(rows.values())


def _csv_text(cols: list[str], rows: list[dict]) -> str:
    sio = io.StringIO()
    w = csv.DictWriter(sio, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return "\ufeff" + sio.getvalue()


# ==================================================================== HTTP
class Handler(BaseHTTPRequestHandler):
    app: App = None  # type: ignore[assignment]
    server_version = "aisup/1"

    def log_message(self, fmt, *args):  # アクセスログは標準エラーへ（本文は出さない）
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    # -------------------------------------------------------------- 共通
    def _send(self, status: int, body: bytes, ctype: str, extra: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")     # 参加コード入り URL を外部に漏らさない
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise BadRequest("body too large")
        if n == 0:
            return {}
        try:
            obj = json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            raise BadRequest("invalid json")
        if not isinstance(obj, dict):
            raise BadRequest("body must be an object")
        return obj

    def _participant(self) -> ParticipantSession:
        tok = self.headers.get("X-Token", "")
        p = self.app.store.participant_by_token(tok) if tok else None
        if p is None:
            raise PermissionError("invalid token")
        return self.app.session(p["participant_id"])

    def _synced(self, b: dict) -> ParticipantSession:
        """操作の前に、それまでの画面のイベントがすべて適用済みかを確かめる。"""
        s = self._participant()
        s.check_synced(b.get("after_seq"), b.get("page_id"))
        return s

    def _admin(self) -> None:
        want = self.app.admin_token
        got = self.headers.get("X-Admin-Token", "")
        if not want or not secrets.compare_digest(want, got):
            raise PermissionError("admin token required")

    def _dispatch(self, method: str) -> None:
        url = urlparse(self.path)
        path = url.path
        try:
            if method == "GET" and not path.startswith("/api/") and path != "/healthz":
                return self._static(path)
            route = ROUTES.get((method, path))
            if route is None:
                return self._json(404, {"error": "not found"})
            route(self, parse_qs(url.query))
        except PermissionError as e:
            self._json(403, {"error": str(e)})
        except NotSynced as e:
            self._json(409, {"error": "not_synced", "acked": e.acked})
        except OtherPage:
            self._json(409, {"error": "other_page"})
        except Busy as e:
            self._json(409, {"error": "busy", "detail": str(e)})
        except BadRequest as e:
            self._json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001  画面を止めないため 500 を返す
            import traceback
            traceback.print_exc()
            self.app.notify(f"サーバ内部エラー {method} {path}: {type(e).__name__}")
            self._json(500, {"error": f"internal: {type(e).__name__}"})

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def _static(self, path: str) -> None:
        root = WEB
        if path in ("/", "/index.html"):
            path = "/index.html"
        elif path in ("/admin", "/admin/"):
            path = "/admin.html"
        elif path.startswith("/materials/"):   # 教材内の画像など
            root, path = MATERIALS, path[len("/materials"):]
        full = os.path.normpath(os.path.join(root, path.lstrip("/")))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            return self._json(404, {"error": "not found"})
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    # -------------------------------------------------------------- 公開
    def api_healthz(self, _q):
        """死活監視用。参加者情報は返さない。"""
        self._json(200, {"ok": True, "sandbox_running": self.app.sandbox.running,
                         "sandbox_waiting": self.app.sandbox.waiting})

    # -------------------------------------------------------------- 学習者 API
    def api_state(self, _q):
        self._json(200, self._participant().state())

    def api_hello(self, _q):
        b = self._body()
        self._json(200, self._participant().hello(str(b.get("page_id") or ""), b.get("info") or {}))

    def api_events(self, _q):
        b = self._body()
        items = b.get("events")
        if not isinstance(items, list):
            raise BadRequest("events must be a list")
        self._json(200, self._participant().client_events(items, b.get("page_id")))

    def api_consent(self, _q):
        b = self._body()
        self._json(200, self._participant().consent(bool(b.get("agree"))))

    def api_envcheck(self, _q):
        b = self._body()
        self._json(200, self._participant().env_check(b.get("info") or {}))

    def api_envcheck_blob(self, _q):
        """通信速度の確認用（256KB）。"""
        self._participant()
        self._send(200, b"0" * 262144, "application/octet-stream")

    def api_answers(self, _q):
        b = self._body()
        ans = b.get("answers") or {}
        if not isinstance(ans, dict):
            raise BadRequest("answers")
        self._json(200, self._participant().save_answers(ans, bool(b.get("final"))))

    def api_task_submit(self, _q):
        b = self._body()
        self._json(200, self._synced(b).task_submit(b.get("code")))

    def api_task_next(self, _q):
        b = self._body()
        self._json(200, self._synced(b).task_next())

    def api_material(self, q):
        self._participant()
        mid = (q.get("id") or [""])[0]
        m = self.app.material_by_id.get(mid)
        if m is None:
            raise BadRequest("unknown material")
        self._json(200, {"id": m["id"], "title": m["title"], "content": m["content"], "version": m["version"]})

    def api_run(self, _q):
        b = self._body()
        code, stdin = b.get("code"), b.get("stdin", "")
        if not isinstance(code, str) or not isinstance(stdin, str):
            raise BadRequest("code/stdin")
        self._json(200, self._synced(b).run_code(code, stdin))

    def api_prompt(self, _q):
        b = self._body()
        if not isinstance(b.get("text"), str):
            raise BadRequest("text")
        self._json(200, self._synced(b).submit_prompt(b["text"]))

    def api_clarify(self, _q):
        b = self._body()
        ch = b.get("choice")
        self._json(200, self._synced(b).clarify_submit(
            str(b.get("opportunity_id", "")), ch if isinstance(ch, int) else None,
            str(b.get("sentence", "")), str(b.get("mode", ""))))

    def api_support_answer(self, _q):
        b = self._body()
        self._json(200, self._synced(b).support_answer(str(b.get("opportunity_id", "")), b.get("answer")))

    def api_support_self_assess(self, _q):
        b = self._body()
        r = b.get("ratings")
        if not isinstance(r, list) or not all(x in (0, 1) for x in r):
            raise BadRequest("ratings")
        self._json(200, self._synced(b).support_self_assess(str(b.get("opportunity_id", "")), r))

    def api_support_hint(self, _q):
        b = self._body()
        self._json(200, self._synced(b).support_hint(str(b.get("opportunity_id", ""))))

    def api_support_run(self, _q):
        b = self._body()
        code, stdin = b.get("code"), b.get("stdin", "")
        if not isinstance(code, str) or not isinstance(stdin, str):
            raise BadRequest("code/stdin")
        self._json(200, self._synced(b).support_run(str(b.get("opportunity_id", "")), code, stdin))

    def api_support_end(self, _q):
        b = self._body()
        self._json(200, self._synced(b).support_end(str(b.get("opportunity_id", "")), str(b.get("reason", ""))))

    # -------------------------------------------------------------- 管理 API
    def api_admin_overview(self, _q):
        self._admin()
        used = {p.get("slot") for p in self.app.store.participants()}
        self._json(200, {"participants": self.app.overview(),
                         "phases": [{"id": p["id"], "kind": p["kind"], "time_limit_s": p.get("time_limit_s")}
                                    for p in self.app.phases],
                         "allocation": {"total": len(self.app.allocation),
                                        "remaining": sum(1 for r in self.app.allocation if int(r["slot"]) not in used),
                                        "hash": self.app.allocation_hash},
                         "alerts": self.app.alerts[-20:],
                         "sandbox": {"running": self.app.sandbox.running, "waiting": self.app.sandbox.waiting},
                         "rule_version": self.app.engine_cfg["rule_version"],
                         "server_config_hash": self.app.server_cfg_hash,
                         "llm_provider": self.app.server_cfg["llm_provider"]})

    def api_admin_create(self, _q):
        self._admin()
        b = self._body()
        self._json(200, self.app.create_participant(b.get("participant_id") or None, str(b.get("group") or "auto"),
                                                    str(b.get("note", ""))))

    def _admin_session(self, b: dict) -> ParticipantSession:
        try:
            return self.app.session(str(b.get("participant_id")))
        except KeyError:
            raise BadRequest("unknown participant")

    def api_admin_stop(self, _q):
        self._admin()
        b = self._body()
        self._admin_session(b).stop(str(b.get("reason") or "admin"))
        self._json(200, {"ok": True})

    def api_admin_force_next(self, _q):
        self._admin()
        b = self._body()
        self._json(200, {"phase": self._admin_session(b).force_next_phase()})

    def api_admin_participant(self, q):
        self._admin()
        pid = (q.get("id") or [""])[0]
        try:
            s = self.app.session(pid)
        except KeyError:
            raise BadRequest("unknown participant")
        with s.lock:
            eng = s.engine
            self._json(200, {"cycles": [c.to_dict() for c in eng.cycles],
                             "decisions": [d.to_dict() for d in eng.decisions],
                             "anomalies": eng.anomalies,
                             "open_cycle": None if eng.open is None else {
                                 "cycle_id": eng.open.cycle_id, "intervened": eng.open.intervened}})

    def api_admin_export(self, _q):
        self._admin()
        data = self.app.export_zip()
        self._send(200, data, "application/zip",
                   {"Content-Disposition": f'attachment; filename="aisup_export_{int(time.time())}.zip"'})

    def api_admin_backup(self, _q):
        self._admin()
        path = self.app.backup("manual")
        self._json(200 if path else 500, {"path": path})


ROUTES = {
    ("GET", "/healthz"): Handler.api_healthz,
    ("GET", "/api/state"): Handler.api_state,
    ("POST", "/api/hello"): Handler.api_hello,
    ("POST", "/api/events"): Handler.api_events,
    ("POST", "/api/consent"): Handler.api_consent,
    ("POST", "/api/envcheck"): Handler.api_envcheck,
    ("GET", "/api/envcheck/blob"): Handler.api_envcheck_blob,
    ("POST", "/api/answers"): Handler.api_answers,
    ("POST", "/api/task/submit"): Handler.api_task_submit,
    ("POST", "/api/task/next"): Handler.api_task_next,
    ("GET", "/api/material"): Handler.api_material,
    ("POST", "/api/run"): Handler.api_run,
    ("POST", "/api/prompt"): Handler.api_prompt,
    ("POST", "/api/clarify"): Handler.api_clarify,
    ("POST", "/api/support/answer"): Handler.api_support_answer,
    ("POST", "/api/support/self_assess"): Handler.api_support_self_assess,
    ("POST", "/api/support/hint"): Handler.api_support_hint,
    ("POST", "/api/support/run"): Handler.api_support_run,
    ("POST", "/api/support/end"): Handler.api_support_end,
    ("GET", "/api/admin/overview"): Handler.api_admin_overview,
    ("POST", "/api/admin/participants"): Handler.api_admin_create,
    ("POST", "/api/admin/stop"): Handler.api_admin_stop,
    ("POST", "/api/admin/force_next"): Handler.api_admin_force_next,
    ("GET", "/api/admin/participant"): Handler.api_admin_participant,
    ("GET", "/api/admin/export"): Handler.api_admin_export,
    ("POST", "/api/admin/backup"): Handler.api_admin_backup,
}


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="学習支援システムのサーバ")
    ap.add_argument("--config", help="サーバ設定 JSON（server/settings.py の既定値を上書き）")
    ap.add_argument("--engine-config", help="判定規則の設定 JSON（engine/config.py の既定値を上書き）")
    ap.add_argument("--data", default="data")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)
    cfg = load_server_config(a.config)
    errs = validate_server_config(cfg)
    if errs:
        sys.exit("設定エラー:\n  " + "\n  ".join(errs))
    admin = os.environ.get("ADMIN_TOKEN")
    if not admin or len(admin) < 8:
        sys.exit("環境変数 ADMIN_TOKEN を設定してください（管理画面の認証。8文字以上）")
    ecfg = load_config(a.engine_config) if a.engine_config else make_config()
    app = App(cfg, a.data, ecfg, admin_token=admin)
    if not app.sandbox.available():
        sys.exit(f"サンドボックスのイメージ {cfg['sandbox']['image']} がない。"
                 f" `docker build -t {cfg['sandbox']['image']} sandbox/` を実行してください。")
    for t in app.tasks:            # 参照解を起動時に検証（採点の期待出力を用意）
        app.judge_inputs(t)
    if not app.allocation:
        print("注意: 割付表がありません（python -m server.allocation で作成。手動割付のみ可能）", file=sys.stderr)
    app.start_ticker()
    srv = make_server(app, a.host, a.port)
    print(f"llm={cfg['llm_provider']} rule_version={ecfg['rule_version']} server_config={app.server_cfg_hash}")
    print(f"学習者画面: http://{a.host}:{a.port}/?t=<参加コード>   管理画面: http://{a.host}:{a.port}/admin")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        srv.server_close()
        app.store.close()
