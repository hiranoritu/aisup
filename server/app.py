"""HTTP サーバ（標準ライブラリのみ）。

    python -m server [--config server_config.json] [--data data/] [--port 8000] [--host 127.0.0.1]

学習者画面: /?t=<参加者トークン>   管理画面: /admin   （管理APIは X-Admin-Token ヘッダ = 環境変数 ADMIN_TOKEN）
API キーはサーバの環境変数だけに置き、ブラウザには渡さない（仕様 §10）。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import mimetypes
import os
import secrets
import sys
import threading
import time
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from engine.config import load_config, make_config
from engine.core import replay
from engine.events import dump_jsonl
from engine.export import export

from .fake_llm import make_fake_llm
from .llm import LLMClient, OpenAIClient
from .prompts import load_prompt
from .sandbox import DockerSandbox, SandboxConfig
from .session import BadRequest, Busy, ParticipantSession
from .settings import abspath, load_server_config, server_config_hash, validate_server_config
from .store import EventStore
from .support import LLM_SUPPORT_TYPES, SupportGenerator
from .tasks import load_tasks

WEB = abspath("web")
MATERIALS = abspath("materials")


class App:
    def __init__(self, server_cfg: dict, data_dir: str, engine_cfg: Optional[dict] = None,
                 llm: Optional[LLMClient] = None, sandbox: Optional[DockerSandbox] = None,
                 admin_token: Optional[str] = None, clock=time.time) -> None:
        self.server_cfg = server_cfg
        self.server_cfg_hash = server_config_hash(server_cfg)
        self.engine_cfg = engine_cfg or make_config()
        self.clock = clock
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        self.store = EventStore(os.path.join(data_dir, "events.sqlite3"))
        self.tasks = load_tasks(abspath(server_cfg["tasks_dir"]), server_cfg["task_sequence"])
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
            self.materials.append({**m, "content": text,
                                   "version": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]})
        self.material_by_id = {m["id"]: m for m in self.materials}
        self._sessions: dict[str, ParticipantSession] = {}
        self._slock = threading.Lock()
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

    # ---------------------------------------------------------------- 管理
    def create_participant(self, pid: Optional[str], group: str, note: str = "") -> dict[str, Any]:
        assignment = "manual"
        if group == "random":
            group = secrets.choice(["adaptive", "uniform"])
            assignment = "random"
        if group not in ("adaptive", "uniform"):
            raise BadRequest("group must be adaptive|uniform|random")
        existing = {p["participant_id"] for p in self.store.participants()}
        if not pid:
            k = len(existing) + 1
            while f"P{k:03d}" in existing:
                k += 1
            pid = f"P{k:03d}"
        if pid in existing:
            raise BadRequest("participant exists")
        if not all(c.isalnum() or c in "-_" for c in pid):
            raise BadRequest("participant_id: alnum, - and _ only")
        token = secrets.token_urlsafe(16)
        self.store.add_participant(pid, token, group, self.clock(), note)
        s = self.session(pid)
        s.start_session(assignment)
        return {"participant_id": pid, "group": group, "token": token, "assignment": assignment}

    def overview(self) -> list[dict[str, Any]]:
        out = []
        for p in self.store.participants():
            s = self.session(p["participant_id"])
            with s.lock:
                out.append({
                    "participant_id": p["participant_id"], "group": p["group"], "token": p["token"],
                    "note": p["note"], "task_id": s.task.task_id if s.task else None,
                    "task_index": s.task_idx, "ended": s.ended, "events": s.n, "last_t": s.last_t,
                    "cycles": len(s.engine.cycles), "decisions": len(s.engine.decisions),
                    "interventions": sum(1 for c in s.engine.cycles if c.intervened)
                                     + (1 if s.engine.open is not None and s.engine.open.intervened else 0),
                    "support_active": s.support["support_type"] if s.support else None,
                    "busy": s.flow.locked(), "anomalies": len(s.engine.anomalies),
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
                "participants": [{k: v for k, v in p.items() if k != "token"} for p in self.store.participants()],
            }, ensure_ascii=False, indent=2))
            for p in self.store.participants():
                pid = p["participant_id"]
                evs = self.store.events(pid)
                path = os.path.join(tmp, f"{pid}_events.jsonl")
                dump_jsonl(evs, path)
                z.write(path, f"events/{pid}_events.jsonl")
                eng = replay(evs, self.engine_cfg)
                for k, fp in export(eng, os.path.join(tmp, "judgments"), pid).items():
                    z.write(fp, f"judgments/{os.path.basename(fp)}")
        return buf.getvalue()


# ==================================================================== HTTP
class Handler(BaseHTTPRequestHandler):
    app: App = None  # type: ignore[assignment]
    server_version = "aisup/1"

    def log_message(self, fmt, *args):  # アクセスログは標準エラーへ（本文は出さない）
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    # -------------------------------------------------------------- 共通
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
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

    def _admin(self) -> None:
        want = self.app.admin_token
        got = self.headers.get("X-Admin-Token", "")
        if not want or not secrets.compare_digest(want, got):
            raise PermissionError("admin token required")

    def _dispatch(self, method: str) -> None:
        url = urlparse(self.path)
        path = url.path
        try:
            if method == "GET" and not path.startswith("/api/"):
                return self._static(path)
            route = ROUTES.get((method, path))
            if route is None:
                return self._json(404, {"error": "not found"})
            route(self, parse_qs(url.query))
        except PermissionError as e:
            self._json(403, {"error": str(e)})
        except Busy as e:
            self._json(409, {"error": "busy", "detail": str(e)})
        except BadRequest as e:
            self._json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001  画面を止めないため 500 を返す
            import traceback
            traceback.print_exc()
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

    # -------------------------------------------------------------- 学習者 API
    def api_state(self, _q):
        self._json(200, self._participant().state())

    def api_material(self, q):
        self._participant()
        mid = (q.get("id") or [""])[0]
        m = self.app.material_by_id.get(mid)
        if m is None:
            raise BadRequest("unknown material")
        self._json(200, {"id": m["id"], "title": m["title"], "content": m["content"], "version": m["version"]})

    def api_events(self, _q):
        b = self._body()
        items = b.get("events")
        if not isinstance(items, list):
            raise BadRequest("events must be a list")
        self._json(200, self._participant().client_events(items))

    def api_run(self, _q):
        b = self._body()
        code, stdin = b.get("code"), b.get("stdin", "")
        if not isinstance(code, str) or not isinstance(stdin, str):
            raise BadRequest("code/stdin")
        self._json(200, self._participant().run_code(code, stdin))

    def api_prompt(self, _q):
        b = self._body()
        if not isinstance(b.get("text"), str):
            raise BadRequest("text")
        self._json(200, self._participant().submit_prompt(b["text"]))

    def api_clarify(self, _q):
        b = self._body()
        ch = b.get("choice")
        self._json(200, self._participant().clarify_submit(
            str(b.get("opportunity_id", "")), ch if isinstance(ch, int) else None,
            str(b.get("sentence", "")), str(b.get("mode", ""))))

    def api_support_answer(self, _q):
        b = self._body()
        self._json(200, self._participant().support_answer(str(b.get("opportunity_id", "")), b.get("answer")))

    def api_support_self_assess(self, _q):
        b = self._body()
        r = b.get("ratings")
        if not isinstance(r, list) or not all(x in (0, 1) for x in r):
            raise BadRequest("ratings")
        self._json(200, self._participant().support_self_assess(str(b.get("opportunity_id", "")), r))

    def api_support_hint(self, _q):
        b = self._body()
        self._json(200, self._participant().support_hint(str(b.get("opportunity_id", ""))))

    def api_support_run(self, _q):
        b = self._body()
        code, stdin = b.get("code"), b.get("stdin", "")
        if not isinstance(code, str) or not isinstance(stdin, str):
            raise BadRequest("code/stdin")
        self._json(200, self._participant().support_run(str(b.get("opportunity_id", "")), code, stdin))

    def api_support_end(self, _q):
        b = self._body()
        self._json(200, self._participant().support_end(str(b.get("opportunity_id", "")), str(b.get("reason", ""))))

    # -------------------------------------------------------------- 管理 API
    def api_admin_overview(self, _q):
        self._admin()
        self._json(200, {"participants": self.app.overview(), "tasks": self.app.task_ids,
                         "rule_version": self.app.engine_cfg["rule_version"],
                         "server_config_hash": self.app.server_cfg_hash,
                         "llm_provider": self.app.server_cfg["llm_provider"]})

    def api_admin_create(self, _q):
        self._admin()
        b = self._body()
        self._json(200, self.app.create_participant(b.get("participant_id") or None, str(b.get("group", "")),
                                                    str(b.get("note", ""))))

    def api_admin_advance(self, _q):
        self._admin()
        b = self._body()
        pids = [p["participant_id"] for p in self.app.store.participants()] if b.get("all") else [b.get("participant_id")]
        out = {}
        for pid in pids:
            try:
                out[pid] = self.app.session(str(pid)).advance()
            except KeyError:
                raise BadRequest(f"unknown participant {pid}")
            except Busy:
                out[pid] = "busy"
        self._json(200, {"result": out})

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
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="aisup_export_{int(time.time())}.zip"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


ROUTES = {
    ("GET", "/api/state"): Handler.api_state,
    ("POST", "/api/events"): Handler.api_events,
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
    ("POST", "/api/admin/advance"): Handler.api_admin_advance,
    ("GET", "/api/admin/participant"): Handler.api_admin_participant,
    ("GET", "/api/admin/export"): Handler.api_admin_export,
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
    if not admin:
        sys.exit("環境変数 ADMIN_TOKEN を設定してください（管理画面の認証）")
    ecfg = load_config(a.engine_config) if a.engine_config else make_config()
    app = App(cfg, a.data, ecfg, admin_token=admin)
    if not app.sandbox.available():
        sys.exit(f"サンドボックスのイメージ {cfg['sandbox']['image']} がない。"
                 f" `docker build -t {cfg['sandbox']['image']} sandbox/` を実行してください。")
    srv = make_server(app, a.host, a.port)
    print(f"llm={cfg['llm_provider']} rule_version={ecfg['rule_version']} server_config={app.server_cfg_hash}")
    print(f"学習者画面: http://{a.host}:{a.port}/?t=<token>   管理画面: http://{a.host}:{a.port}/admin")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        app.store.close()
