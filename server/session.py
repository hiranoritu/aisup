"""参加者ごとのセッション：イベントの保存 → エンジンへの投入 → 画面用の状態（仕様 v1.2）。

- すべての状態変化はイベントとして保存してから _apply() で反映する。再起動時は同じ _apply() で
  保存済みイベントを先頭から再生して復元する（本番と再生が同じ経路を通る）。
- 外部呼び出し（LLM・サンドボックス）の結果はイベントとして保存してから使う。
- ブラウザのイベントには連番 client_seq が付く。サーバは連番の順にだけ適用し（到着順が入れ替わっても同じ）、
  重複は捨て、欠落が gap_timeout_s 埋まらなければ CLIENT_GAP を記録して先へ進む（§2）。
  ブラウザは適用済み（acked）でないイベントを端末に保持して再送する。
- 画面からの操作（実行・質問など）は after_seq（その時点までに付けた連番）を伴い、それまでのイベントが
  すべて適用済みでなければ受け付けない（NotSynced）。これで操作の結果と編集の順序が常に一致する。
- 時刻はサーバ時刻。同一参加者内で狭義単調増加にする。
"""
from __future__ import annotations

import hashlib
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from engine.core import Engine
from engine.events import Event

from .features import extract_features
from .render import segments
from .support import (LLM_SUPPORT_TYPES, build_clarify, grade_choice, grade_code, grade_text_llm, norm_output)
from .tasks import Task

if TYPE_CHECKING:
    from .app import App

CLIENT_EVENT_TYPES = {"CODE_EDIT", "COPY_FROM_AI", "COPY_IN_EDITOR", "CODE_SNAPSHOT", "CLIENT_INFO",
                      "MATERIAL_VIEW", "FOCUS_CHANGE", "PASTE_BLOCKED"}
# 課題フェーズ以外でも受け付けるもの（テスト中の離脱記録など）
ANY_PHASE_TYPES = {"FOCUS_CHANGE", "CLIENT_INFO", "PASTE_BLOCKED"}
EDIT_KINDS = {"typed", "paste", "undo", "redo", "other"}
ACTIVE = "active"


class Busy(Exception):
    pass


class BadRequest(Exception):
    pass


class NotSynced(Exception):
    def __init__(self, acked: int) -> None:
        super().__init__("not_synced")
        self.acked = acked


class OtherPage(Exception):
    pass


def _norm_clip(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n").rstrip()


class ParticipantSession:
    def __init__(self, app: "App", pinfo: dict[str, Any], clock: Callable[[], float] = time.time) -> None:
        self.app = app
        self.pid: str = pinfo["participant_id"]
        self.group: str = pinfo["group"]
        self.clock = clock
        self.lock = threading.RLock()          # イベント追記と状態更新
        self.flow = threading.Lock()           # 質問・採点・フェーズ移行（同時に1つ）
        self.engine = Engine(app.engine_cfg)
        self.n = 0                             # サーバ発のイベント番号（e{n}）
        self.last_t = 0.0
        # 連番の受信
        self.next_seq = 1
        self.gap_since: Optional[float] = None
        self.page_id: Optional[str] = None
        # 実験の進行
        self.started = False
        self.status = "not_started"            # not_started/active/done/withdrawn/stopped/expired
        self.opened_t: Optional[float] = None  # 最初に開いた時刻（期限の起点）
        self.phase_idx = -1
        self.phase_t0 = 0.0
        self.task_pos = -1
        self.task_passed = False
        self.last_submit: Optional[dict] = None
        self.answers: dict[str, dict[str, Any]] = {}
        self.env_ok = False
        self.phases_reached: list[str] = []
        # 課題・チャット・支援
        self.task: Optional[Task] = None
        self.chat: dict[str, list[dict[str, Any]]] = {}
        self.last_copy: Optional[dict[str, Any]] = None
        self.last_compile: Optional[dict] = None
        self.last_run: Optional[dict] = None
        self.support: Optional[dict[str, Any]] = None
        self.pending: dict[str, dict[str, Any]] = {}
        self.last_decision: Optional[dict] = None
        # LLM の利用量
        self.questions = 0
        self.cost_usd = 0.0
        self.limits_hit: set[str] = set()
        self.version = 0                       # 画面の作り直しが必要な変化で増える
        self.errors = 0

    # ================================================================ イベント
    def restore(self, events: list[Event]) -> None:
        with self.lock:
            for ev in events:
                if ev.event_id.startswith("e"):
                    self.n = max(self.n, int(ev.event_id[1:] or 0))
                self.last_t = max(self.last_t, ev.t)
                self._apply(ev)

    def _new_event(self, event_id: str, typ: str, payload: dict[str, Any], task_id: Optional[str]) -> Event:
        t = max(self.clock(), self.last_t + 0.001)
        self.last_t = t
        in_support = self.support is not None and typ != "SUPPORT_SHOWN"
        ev = Event(event_id, typ, round(t, 3), self.pid,
                   task_id if task_id is not None else (self.task.task_id if self.task else ""),
                   payload, in_support)
        self.app.store.append(ev)
        self._apply(ev)
        return ev

    def emit(self, typ: str, payload: Optional[dict[str, Any]] = None, task_id: Optional[str] = None) -> Event:
        """サーバ発のイベント。"""
        with self.lock:
            self.n += 1
            return self._new_event(f"e{self.n}", typ, payload or {}, task_id)

    def _apply(self, ev: Event) -> None:
        dec = self.engine.ingest(ev)
        p = ev.payload
        typ = ev.type
        if ev.event_id.startswith("c"):
            self.next_seq = max(self.next_seq, int(p.get("client_seq", 0)) + 1)
        if typ == "SESSION_START":
            self.started = True
        elif typ == "PHASE_START":
            if self.opened_t is None:
                self.opened_t = ev.t
            self.status = ACTIVE
            self.phase_idx = self.app.phase_index[p["phase"]]
            self.phase_t0 = ev.t
            self.task_pos = -1
            self.task = None
            self.phases_reached.append(p["phase"])
            self.version += 1
        elif typ == "CONSENT" and p.get("agree"):
            pass
        elif typ == "ENV_CHECK":
            self.env_ok = bool(p.get("ok"))
        elif typ == "TASK_START":
            self.task = self.app.task_by_id[ev.task_id]
            ph = self.phase()
            self.task_pos = ph["tasks"].index(ev.task_id) if ph and ev.task_id in ph.get("tasks", []) else 0
            self.task_passed = False
            self.last_submit = None
            self.chat.setdefault(ev.task_id, [])
            self.last_copy = None
            self.last_compile = self.last_run = None
            self.pending.clear()
            self.version += 1
        elif typ == "TASK_END":
            self.task = None
            self.version += 1
        elif typ == "TASK_SUBMIT":
            self.last_submit = p
            if p.get("passed"):
                self.task_passed = True
        elif typ in ("TEST_ANSWER", "QUESTIONNAIRE_ANSWER"):
            self.answers.setdefault(p["phase"], {})[p["item_id"]] = p.get("answer")
        elif typ == "SESSION_END":
            self.status = "withdrawn" if p.get("reason") == "declined" else "done"
            self.task = None
            self.version += 1
        elif typ == "PARTICIPANT_STOPPED":
            self.status = "stopped"
            self.task = None
            self.version += 1
        elif typ == "SESSION_EXPIRED":
            self.status = "expired"
            self.task = None
            self.version += 1
        elif typ == "CLIENT_INFO" and p.get("event") == "page_open":
            self.page_id = p.get("page_id")
        elif typ == "SESSION_RESUME":
            self.page_id = p.get("page_id")
        elif typ == "CLIENT_GAP":
            self.next_seq = max(self.next_seq, int(p.get("to_seq", 0)) + 1)
        elif typ == "PROMPT_DRAFT_SUBMIT":
            self.questions += 1
        elif typ == "LLM_CALL":
            self.cost_usd += self.app.llm_cost(p.get("model", ""), p.get("usage") or {})
        elif typ == "LIMIT_REACHED":
            self.limits_hit.add(f'{p.get("action", "notify")}:{p.get("limit", "")}')
        elif typ in ("SANDBOX_ERROR", "AI_ERROR"):
            self.errors += 1
        elif typ == "PROMPT_FEATURES" and dec is not None:
            self.last_decision = dec.to_dict()
        elif typ == "PROMPT_SENT":
            self._chat().append({"role": "user", "content": p["text"], "prompt_event_id": p["prompt_event_id"]})
            self.pending.pop(p["prompt_event_id"], None)
        elif typ == "AI_RESPONSE":
            self._chat().append({"role": "assistant", "content": p["text"], "response_id": p["response_id"]})
        elif typ == "AI_ERROR":
            self._chat().append({"role": "error", "content": "AIの応答を取得できませんでした。もう一度送ってください。"})
        elif typ == "COPY_FROM_AI" and not p.get("blocked"):
            self.last_copy = p
        elif typ == "COMPILE":
            self.last_compile = p
            if not p.get("ok"):
                self.last_run = None
        elif typ == "RUN":
            self.last_run = p
        elif typ == "SUPPORT_SHOWN":
            self.support = {"shown_t": ev.t, "attempts": 0, "hint_used": False, "done": False,
                            "results": [], **p}
            if p.get("support_type") == "clarify_question":
                self.pending[p["prompt_event_id"]] = {"text": p["public"]["original_text"]}
        elif typ == "SUPPORT_ANSWER" and self.support:
            self.support["attempts"] = p.get("attempt", self.support["attempts"] + 1)
            self.support["results"].append(p.get("result"))
            self.support["done"] = bool(p.get("done"))
        elif typ == "SUPPORT_HINT" and self.support:
            self.support["hint_used"] = True
        elif typ == "SUPPORT_END":
            self.support = None

    def _chat(self) -> list[dict[str, Any]]:
        return self.chat.setdefault(self.task.task_id if self.task else "", [])

    def _llm_call(self, purpose: str, prompt_version: str, res) -> str:
        return self.emit("LLM_CALL", res.to_payload(purpose, prompt_version)).event_id

    # ================================================================ 進行
    def phase(self) -> Optional[dict[str, Any]]:
        return self.app.phases[self.phase_idx] if 0 <= self.phase_idx < len(self.app.phases) else None

    def deadline(self) -> Optional[float]:
        ph = self.phase()
        if ph is None or not ph.get("time_limit_s"):
            return None
        return self.phase_t0 + float(ph["time_limit_s"])

    def _start_phase(self, idx: int) -> None:
        ph = self.app.phases[idx]
        self.emit("PHASE_START", {"phase": ph["id"], "kind": ph["kind"], "time_limit_s": ph.get("time_limit_s"),
                                  "tasks": ph.get("tasks", [])}, task_id="")
        if ph["kind"] == "tasks":
            self._start_task(ph["tasks"][0])

    def _start_task(self, task_id: str) -> None:
        t = self.app.task_by_id[task_id]
        self.emit("TASK_START", {"phase": t.phase, "starter_code": t.starter_code, "title": t.title},
                  task_id=t.task_id)

    def _end_phase(self, reason: str) -> None:
        """現在のフェーズを閉じて次へ（lock を持って呼ぶ）。最後なら SESSION_END。"""
        ph = self.phase()
        if ph is None:
            return
        if self.support is not None:
            self.emit("SUPPORT_END", {"opportunity_id": self.support["opportunity_id"], "reason": "aborted",
                                      "by": "phase_" + reason})
        if self.task is not None:
            self.emit("TASK_END", {"reason": reason, "passed": self.task_passed})
        self.emit("PHASE_END", {"phase": ph["id"], "reason": reason}, task_id="")
        nxt = self.phase_idx + 1
        if nxt >= len(self.app.phases):
            self.emit("SESSION_END", {"reason": "completed", "phases_reached": list(self.phases_reached)}, task_id="")
            self.app.on_session_end(self)
        else:
            self._start_phase(nxt)

    def tick(self) -> None:
        """時間上限・期限の確認（サーバの定期処理から呼ぶ）。質問処理中なら次回に回す。"""
        if self.status != ACTIVE or not self.flow.acquire(blocking=False):
            return
        try:
            with self.lock:
                now = self.clock()
                if self.opened_t is not None and now - self.opened_t >= self.app.server_cfg["session"]["expiry_s"]:
                    ph = self.phase()
                    if self.support is not None:
                        self.emit("SUPPORT_END", {"opportunity_id": self.support["opportunity_id"], "reason": "aborted",
                                                  "by": "expired"})
                    if self.task is not None:
                        self.emit("TASK_END", {"reason": "expired", "passed": self.task_passed})
                    if ph is not None:
                        self.emit("PHASE_END", {"phase": ph["id"], "reason": "expired"}, task_id="")
                    self.emit("SESSION_EXPIRED", {"opened_t": self.opened_t}, task_id="")
                    self.app.notify(f"{self.pid}: 開始から期限を過ぎたため無効になりました")
                    return
                dl = self.deadline()
                if dl is not None and now >= dl:
                    self._end_phase("timeout")
        finally:
            self.flow.release()

    def require_active(self) -> None:
        if self.status != ACTIVE:
            raise BadRequest(f"session {self.status}")

    def _require_kind(self, kind: str) -> dict[str, Any]:
        self.require_active()
        ph = self.phase()
        if ph is None or ph["kind"] != kind:
            raise BadRequest(f"not in {kind} phase")
        return ph

    # ================================================================ 画面状態
    def state(self) -> dict[str, Any]:
        with self.lock:
            sc = self.app.server_cfg
            ph = self.phase()
            st: dict[str, Any] = {
                "participant_id": self.pid, "status": self.status, "version": self.version,
                "next_seq": self.next_seq, "now_t": self.clock(),
                "phase": None if ph is None else {
                    "id": ph["id"], "kind": ph["kind"], "index": self.phase_idx, "count": len(self.app.phases),
                    "deadline_t": self.deadline(), "title": self.app.phase_title(ph)},
                "busy": self.flow.locked(),
                "completion_message": sc["completion_message"] if self.status == "done" else None,
                "settings": {"allow_skip": sc["support"]["allow_skip"],
                             "block_copy_during_support": sc["support"]["block_copy_during_support"],
                             "max_duration_s": sc["support"]["max_duration_s"],
                             "snapshot_interval_s": sc["snapshot_interval_s"],
                             "freeform_feedback": sc["support"]["freeform_feedback"]},
            }
            if ph is None or self.status != ACTIVE:
                return st
            kind = ph["kind"]
            if kind == "consent":
                st["consent"] = {"text": self.app.docs[ph["doc"]]["text"], "version": self.app.docs[ph["doc"]]["version"]}
            elif kind in ("test", "questionnaire"):
                inst = self.app.instruments[ph["instrument"]]
                st["instrument"] = {"title": inst.get("title", ""), "intro": inst.get("intro", ""),
                                    "items": [{k: v for k, v in it.items() if k not in ("answer", "points")}
                                              for it in inst["items"]],
                                    "answers": self.answers.get(ph["id"], {})}
            elif kind == "tasks":
                chat = []
                for m in self.chat.get(self.task.task_id, []) if self.task else []:
                    item = {"role": m["role"], "content": m["content"]}
                    if m["role"] == "assistant":
                        item["response_id"] = m["response_id"]
                        item["segments"] = segments(m["content"])
                    chat.append(item)
                st.update({
                    "task": self.task.public() if self.task else None,
                    "task_pos": self.task_pos, "task_count": len(ph["tasks"]),
                    "task_passed": self.task_passed, "last_submit": self._submit_public(),
                    "materials": [{"id": m["id"], "title": m["title"]} for m in self.app.materials
                                  if not m.get("phases") or (self.task and self.task.phase in m["phases"])],
                    "code": self.engine.doc.text if self.task else "",
                    "chat": chat, "support": self._support_public(),
                    "ai_blocked": self.ai_blocked(),
                    "ai_blocked_message": sc["llm_limits"]["block_message"] if self.ai_blocked() else "",
                })
            return st

    def _submit_public(self) -> Optional[dict[str, Any]]:
        s = self.last_submit
        if s is None:
            return None
        return {"passed": s.get("passed"), "compile": s.get("compile"),
                "results": [r if r.get("public") else {"public": False, "pass": r.get("pass")}
                            for r in s.get("results", [])]}

    def _support_public(self) -> Optional[dict[str, Any]]:
        s = self.support
        if s is None:
            return None
        key = s.get("key") or {}
        return {"opportunity_id": s["opportunity_id"], "support_type": s["support_type"],
                "timing": s.get("timing"), "public": s["public"], "attempts": s["attempts"],
                "hint_used": s["hint_used"], "hint": key.get("hint") if s["hint_used"] else None,
                "done": s["done"], "results": s["results"],
                "deadline_t": s["shown_t"] + self.app.server_cfg["support"]["max_duration_s"],
                "now_t": self.clock(), "response_id": s.get("response_id")}

    # ================================================================ 参加の開始・再開・管理
    def start_session(self, assignment: str, slot: Optional[int] = None) -> None:
        with self.lock:
            if self.started:
                return
            self.emit("SESSION_START", {
                "group": self.group, "assignment": assignment, "allocation_slot": slot,
                "allocation_file_hash": self.app.allocation_hash,
                "rule_version": self.engine.rule_version, "config_hash": self.engine.cfg_hash,
                "server_config_hash": self.app.server_cfg_hash,
                "structure_analyzer": getattr(self.engine.analyzer, "version_label", self.engine.analyzer.name),
                "phases": [p["id"] for p in self.app.phases],
                "sandbox_image": self.app.server_cfg["sandbox"]["image"]}, task_id="")

    def hello(self, page_id: str, info: dict[str, Any]) -> dict[str, Any]:
        """ページを開いたとき。初回は同意フェーズを始め、2回目以降は SESSION_RESUME を記録する。"""
        if not isinstance(page_id, str) or not page_id:
            raise BadRequest("page_id")
        with self.flow:
            with self.lock:
                if self.status in ("done", "withdrawn", "stopped", "expired"):
                    return {"state": self.state()}
                if self.status == "not_started":
                    self.emit("CLIENT_INFO", {"event": "page_open", "page_id": page_id, **_clean_info(info)},
                              task_id="")
                    self._start_phase(0)
                else:
                    gap = max(0.0, self.clock() - self.last_t)
                    self.emit("SESSION_RESUME", {
                        "page_id": page_id, "gap_s": round(gap, 1),
                        "over_limit": gap > self.app.server_cfg["session"]["resume_notice_s"],
                        "phase": self.phase()["id"] if self.phase() else None,
                        "text": self.engine.doc.text if self.task else None, **_clean_info(info)})
        self.tick()
        return {"state": self.state()}

    def stop(self, reason: str) -> None:
        with self.lock:
            if self.status in ("done", "withdrawn", "stopped", "expired"):
                return
            if self.support is not None:
                self.emit("SUPPORT_END", {"opportunity_id": self.support["opportunity_id"], "reason": "aborted",
                                          "by": "admin_stop"})
            if self.task is not None:
                self.emit("TASK_END", {"reason": "stopped", "passed": self.task_passed})
            self.emit("PARTICIPANT_STOPPED", {"reason": reason}, task_id="")

    def force_next_phase(self) -> str:
        """管理画面からの強制移行（予備実験・緊急用）。"""
        if not self.flow.acquire(timeout=30):
            raise Busy("busy")
        try:
            with self.lock:
                self.require_active()
                self._end_phase("admin")
                return self.phase()["id"] if self.status == ACTIVE else self.status
        finally:
            self.flow.release()

    # ================================================================ 同意・環境確認
    def consent(self, agree: bool) -> dict[str, Any]:
        with self.flow, self.lock:
            ph = self._require_kind("consent")
            self.emit("CONSENT", {"agree": bool(agree), "doc_version": self.app.docs[ph["doc"]]["version"]}, task_id="")
            if agree:
                self._end_phase("completed")
            else:
                self.emit("PHASE_END", {"phase": ph["id"], "reason": "declined"}, task_id="")
                self.emit("SESSION_END", {"reason": "declined"}, task_id="")
        return {"state": self.state()}

    def env_check(self, info: dict[str, Any]) -> dict[str, Any]:
        """環境確認（§15.1）。スマートフォン・タブレットは止める。サンドボックスで試し実行する。"""
        with self.flow:
            with self.lock:
                self._require_kind("envcheck")
            out = self.app.sandbox.run('#include <stdio.h>\nint main(void){printf("ok\\n");return 0;}\n', [""])
            sandbox_ok = bool(out.ok and out.compile.get("ok") and out.runs and out.runs[0].get("stdout") == "ok\n")
            info = _clean_info(info)
            blocking, warnings = [], []
            if info.get("mobile"):
                blocking.append("mobile")
            if not sandbox_ok:
                blocking.append("sandbox")
            if (info.get("viewport_w") or 0) < 1000:
                warnings.append("narrow_screen")
            if (info.get("net_ms") or 0) > 3000:
                warnings.append("slow_network")
            if not info.get("supported_browser", True):
                warnings.append("browser")
            ok = not blocking
            with self.lock:
                self.emit("ENV_CHECK", {**info, "sandbox_ok": sandbox_ok, "sandbox_ms": out.run_ms,
                                        "blocking": blocking, "warnings": warnings, "ok": ok}, task_id="")
                if ok:
                    self._end_phase("completed")
        return {"ok": ok, "blocking": blocking, "warnings": warnings, "state": self.state()}

    # ================================================================ テスト・質問紙
    def save_answers(self, answers: dict[str, Any], final: bool) -> dict[str, Any]:
        with self.flow, self.lock:
            self.require_active()
            ph = self.phase()
            if ph is None or ph["kind"] not in ("test", "questionnaire"):
                raise BadRequest("not in test/questionnaire phase")
            inst = self.app.instruments[ph["instrument"]]
            items = {it["id"]: it for it in inst["items"]}
            typ = "TEST_ANSWER" if ph["kind"] == "test" else "QUESTIONNAIRE_ANSWER"
            saved = self.answers.get(ph["id"], {})
            for item_id, ans in (answers or {}).items():
                if item_id not in items:
                    raise BadRequest(f"unknown item {item_id}")
                _validate_answer(items[item_id], ans)
                if saved.get(item_id) != ans:
                    self.emit(typ, {"phase": ph["id"], "item_id": item_id, "answer": ans}, task_id="")
            if final:
                missing = [i for i, it in items.items() if it.get("required") and self.answers.get(ph["id"], {}).get(i) in (None, "")]
                if missing:
                    raise BadRequest("required: " + ",".join(missing))
                self._end_phase("completed")
        return {"state": self.state()}

    # ================================================================ 課題の提出と移動
    def task_submit(self, code: str) -> dict[str, Any]:
        if not isinstance(code, str):
            raise BadRequest("code")
        if not self.flow.acquire(blocking=False):
            raise Busy("busy")
        try:
            r = self._task_submit(code)
        finally:
            self.flow.release()
        r["state"] = self.state()   # 処理後の状態（busy=false）を返す
        return r

    def _task_submit(self, code: str) -> dict[str, Any]:
        with self.lock:
            self._require_kind("tasks")
            if self.task is None or self.support is not None:
                raise BadRequest("cannot submit now")
            task = self.task
            self.emit("CODE_SNAPSHOT", {"text": code, "reason": "submit"})
        judge = self.app.judge_inputs(task)
        out = self.app.sandbox.run(code, [j["stdin"] for j in judge])
        with self.lock:
            if not out.ok:
                self.emit("SANDBOX_ERROR", {"error": out.error, "image": out.image, "during": "submit"})
                return {"ok": False, "error": "採点環境でエラーが起きました。もう一度提出してください。"}
            results = []
            if out.compile.get("ok"):
                for j, r in zip(judge, out.runs):
                    ok = (not r.get("sanitizer") and not r.get("timed_out") and r.get("exit_code") == 0
                          and norm_output(r.get("stdout", "")) == norm_output(j["expected"]))
                    results.append({"stdin": j["stdin"] if j["public"] else None,
                                    "expected": j["expected"] if j["public"] else None,
                                    "actual": r.get("stdout", "") if j["public"] else None,
                                    "public": j["public"], "pass": ok,
                                    "sanitizer": r.get("sanitizer"), "timed_out": r.get("timed_out")})
            passed = bool(out.compile.get("ok")) and bool(results) and all(x["pass"] for x in results)
            # 判定用の完全な記録（非公開入力の出力も含む）はイベントに保存する
            self.emit("TASK_SUBMIT", {"code": code, "code_hash": _h(code), "passed": passed,
                                      "compile": out.compile, "results": results,
                                      "full": [{"stdin": j["stdin"], "stdout": r.get("stdout"), "exit_code": r.get("exit_code")}
                                               for j, r in zip(judge, out.runs)]})
            return {"ok": True, "passed": passed}

    def task_next(self) -> dict[str, Any]:
        with self.flow, self.lock:
            ph = self._require_kind("tasks")
            if not self.task_passed:
                raise BadRequest("task not passed")
            if self.support is not None:
                raise BadRequest("support active")
            if self.task_pos + 1 < len(ph["tasks"]):
                self.emit("TASK_END", {"reason": "completed", "passed": True})
                self._start_task(ph["tasks"][self.task_pos + 1])
            else:
                self._end_phase("completed")
        return {"state": self.state()}

    # ================================================================ 画面からのイベント（連番つき）
    def check_synced(self, after_seq: Any, page_id: Any = None) -> None:
        if page_id is not None and self.page_id is not None and page_id != self.page_id:
            raise OtherPage()
        if after_seq is None:
            return
        try:
            a = int(after_seq)
        except (TypeError, ValueError):
            raise BadRequest("after_seq")
        if a >= self.next_seq:
            raise NotSynced(self.next_seq - 1)

    def client_events(self, items: list[dict[str, Any]], page_id: Optional[str] = None) -> dict[str, Any]:
        with self.lock:
            if page_id is not None and self.page_id is not None and page_id != self.page_id:
                raise OtherPage()
            parsed = []
            for it in items:
                try:
                    seq = int(it["client_seq"])
                except (KeyError, TypeError, ValueError):
                    raise BadRequest("client_seq")
                if it.get("type") not in CLIENT_EVENT_TYPES:
                    raise BadRequest(f"type not allowed: {it.get('type')}")
                parsed.append((seq, it))
            parsed.sort(key=lambda x: x[0])
            now = self.clock()
            for seq, it in parsed:
                if seq < self.next_seq:
                    continue                                  # 重複（再送）は捨てる
                if seq > self.next_seq:
                    if self.gap_since is None:
                        self.gap_since = now
                    waited = now - self.gap_since
                    if waited < self.app.server_cfg["session"]["gap_timeout_s"]:
                        break                                 # 欠落が埋まるのを待つ（再送を期待）
                    self.emit("CLIENT_GAP", {"from_seq": self.next_seq, "to_seq": seq - 1,
                                             "waited_s": round(waited, 1)})
                self.gap_since = None
                self._apply_client(seq, it)
            if not any(seq > self.next_seq - 1 for seq, _ in parsed):
                self.gap_since = None
            return {"ok": True, "acked": self.next_seq - 1, "version": self.version, "status": self.status}

    def _apply_client(self, seq: int, it: dict[str, Any]) -> None:
        typ = it["type"]
        p = dict(it.get("payload") or {})
        p.update({"client_seq": seq, "client_t": it.get("client_t"), "page_id": it.get("page_id"),
                  "server_recv_t": round(self.clock(), 3)})
        in_task = self.status == ACTIVE and self.task is not None
        if typ not in ANY_PHASE_TYPES and not in_task:
            p = {"client_seq": seq, "ignored_type": typ, "reason": "not_in_task", "page_id": p.get("page_id")}
            typ = "CLIENT_INFO"
        elif typ == "CODE_EDIT":
            p = self._normalize_edit(p)
        elif typ == "COPY_FROM_AI":
            if self.support is not None and self.app.server_cfg["support"]["block_copy_during_support"]:
                p["blocked"] = True   # §13-4：支援中のコピーは禁止（試みのみ記録）
        elif typ == "CODE_SNAPSHOT":
            if not isinstance(p.get("text"), str):
                p = {"client_seq": seq, "ignored_type": typ, "reason": "bad_payload"}
                typ = "CLIENT_INFO"
        elif typ == "MATERIAL_VIEW":
            m = self.app.material_by_id.get(p.get("material_id"))
            if m is None or p.get("action") not in ("open", "close"):
                p = {"client_seq": seq, "ignored_type": typ, "reason": "bad_payload"}
                typ = "CLIENT_INFO"
            else:
                p["material_version"] = m["version"]
                # 支援の表示中に開閉したか（教材は支援中も開ける。2026-11-24 決定）
                p["during_support"] = self.support is not None
                if self.support is not None:
                    p["opportunity_id"] = self.support.get("opportunity_id")
        elif typ == "PASTE_BLOCKED":
            p = {k: p.get(k) for k in ("client_seq", "client_t", "page_id", "server_recv_t", "length", "via")}
            p["via"] = str(p.get("via") or "")[:40]     # 欄の種類（editor / chat / support_text / test_text など）:paste|drop
        self._new_event(f"c{seq}", typ, p, None)

    def _normalize_edit(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            p["offset"] = int(p["offset"])
            p["delete_len"] = int(p.get("delete_len", 0))
        except (KeyError, TypeError, ValueError):
            p["offset"], p["delete_len"] = -1, 0      # 適用に失敗させて欠落として扱う（画面は止めない）
        if not isinstance(p.get("insert_text", ""), str):
            p["insert_text"] = ""
        p.setdefault("insert_text", "")
        if p.get("kind") not in EDIT_KINDS:
            p["kind"] = "other"
        claimed = (p.pop("source", None) or {}).get("type")
        if p["kind"] == "paste":
            # 仕様 §2：直前の COPY_FROM_AI と文字列が一致すれば source=ai と由来を付ける
            c = self.last_copy
            clip = self.engine.clipboard_text
            ins = p["insert_text"]
            if c is not None and _norm_clip(c.get("text", "")) == _norm_clip(ins) and ins.strip():
                p["source"] = {"type": "ai", "response_id": c.get("response_id"), "block_id": c.get("block_id")}
            elif clip is not None and clip == ins:
                p["source"] = {"type": "editor"}       # エディタ内のコピー（由来を引き継ぐ）
            else:
                # 画面で遮断されるはずの外部貼り付けが届いた（記録して印を付ける）
                p["source"] = {"type": "external"}
                p["unblocked_external"] = True
                p["claimed_source"] = claimed
        return p

    # ================================================================ 実行
    def run_code(self, code: str, stdin: str) -> dict[str, Any]:
        with self.lock:
            self.require_active()
            if self.task is None:
                raise BadRequest("no active task")
            if self.support is not None:
                raise BadRequest("support active")
            self.emit("CODE_SNAPSHOT", {"text": code, "reason": "run"})
        out = self.app.sandbox.run(code, [stdin])
        with self.lock:
            if not out.ok:
                self.emit("SANDBOX_ERROR", {"error": out.error, "image": out.image})
                return {"ok": False, "error": "実行環境でエラーが起きました。もう一度実行してください。"}
            self.emit("COMPILE", {**out.compile, "code_hash": _h(code)})
            result: dict[str, Any] = {"ok": True, "compile": out.compile}
            if out.compile.get("ok"):
                r = out.runs[0]
                self.emit("RUN", {**r, "code_hash": _h(code), "image": out.image})
                result["run"] = r
            return result

    # ================================================================ 質問
    def submit_prompt(self, text: str) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise BadRequest("empty prompt")
        if not self.flow.acquire(blocking=False):
            raise Busy("prompt in flight")
        try:
            r = self._submit_prompt(text)
        finally:
            self.flow.release()
        with self.lock:
            self._check_limits()
        r["state"] = self.state()   # フロー終了後の状態（busy=false）を返す
        return r

    def _submit_prompt(self, text: str) -> dict[str, Any]:
        with self.lock:
            self.require_active()
            if self.task is None:
                raise BadRequest("no active task")       # テスト・質問紙中は AI チャットを使えない（§10）
            if self.support is not None:
                raise BadRequest("support active")
            if self.ai_blocked():
                raise BadRequest("llm limit reached")
            task = self.task
            draft = self.emit("PROMPT_DRAFT_SUBMIT", {"text": text})
            prev = next((m["content"] for m in reversed(self._chat()) if m["role"] == "assistant"), "")
        a = self.app
        res, fpayload = extract_features(a.llm, a.prompts["features"], task, prev, text,
                                         float(a.server_cfg["llm"]["features"]["timeout_s"]))
        with self.lock:
            call_id = self._llm_call("features", a.prompts["features"].version, res)
            self.emit("PROMPT_FEATURES", {"prompt_event_id": draft.event_id, "llm_call_id": call_id,
                                               **fpayload})
            dec = self.last_decision if self.last_decision and self.last_decision["prompt_event_id"] == draft.event_id else None
            if dec is not None:
                self.emit("DECISION", dec)
            if dec and dec["support_type"] and dec["timing"] == "pre_send":
                public, key = build_clarify(text, self.last_compile, self.last_run)
                self.emit("SUPPORT_SHOWN", {"opportunity_id": dec["opportunity_id"],
                                            "support_type": dec["support_type"], "timing": "pre_send",
                                            "phase": task.phase, "prompt_event_id": draft.event_id,
                                            "public": public, "key": key})
                return {"stage": "pre_send_support"}
        return self._send_to_ai(draft.event_id, text, text, dec)

    def ai_blocked(self) -> bool:
        return any(k.startswith("block:") for k in self.limits_hit)

    def _check_limits(self) -> None:
        """参加者ごとの質問回数・費用の上限（§15.6）。上限ごとに一度だけ記録して実験者に通知する。
        notify は通知のみ、block は以後の学習者用 AI への質問を止める（画面に理由を表示）。"""
        lim = self.app.server_cfg["llm_limits"]
        for action in ("notify", "block"):
            t = lim[action]
            for name, over in (("questions", self.questions >= t["max_questions"]),
                               ("cost", self.cost_usd >= t["max_cost_usd"])):
                if over and f"{action}:{name}" not in self.limits_hit:
                    self.emit("LIMIT_REACHED", {"limit": name, "action": action, "questions": self.questions,
                                                "cost_usd": round(self.cost_usd, 4),
                                                "threshold": t["max_questions"] if name == "questions" else t["max_cost_usd"]})
                    what = "通知" if action == "notify" else "停止（以後 AI に質問できません）"
                    self.app.notify(f"{self.pid}: LLM の利用上限（{name}・{what}）に達しました"
                                    f"（質問 {self.questions} 回・約 ${self.cost_usd:.2f}）")

    def clarify_submit(self, opportunity_id: str, choice: Optional[int], sentence: str, mode: str) -> dict[str, Any]:
        """P-A：選択・一文の記録 → 支援終了 → 「原文＋一文」か「原文のまま」で送信。"""
        if mode not in ("appended", "original", "skipped", "timeout"):
            raise BadRequest("mode")
        if not self.flow.acquire(blocking=False):
            raise Busy("prompt in flight")
        try:
            r = self._clarify_submit(opportunity_id, choice, sentence, mode)
        finally:
            self.flow.release()
        r["state"] = self.state()
        return r

    def _clarify_submit(self, opportunity_id: str, choice: Optional[int], sentence: str, mode: str) -> dict[str, Any]:
        with self.lock:
            s = self.support
            if s is None or s["opportunity_id"] != opportunity_id or s["support_type"] != "clarify_question":
                raise BadRequest("no clarify support")
            if mode == "skipped" and not self.app.server_cfg["support"]["allow_skip"]:
                raise BadRequest("skip not allowed")
            sentence = (sentence or "").strip()
            if mode == "appended" and not sentence:
                raise BadRequest("sentence required")
            pid = s["prompt_event_id"]
            original = s["public"]["original_text"]
            if mode in ("appended", "original"):
                self.emit("SUPPORT_ANSWER", {"opportunity_id": opportunity_id, "attempt": 1,
                                             "answer": {"choice": choice, "sentence": sentence, "mode": mode},
                                             "result": None, "done": True})
            reason = {"skipped": "skipped", "timeout": "timeout"}.get(mode, "completed")
            self.emit("SUPPORT_END", {"opportunity_id": opportunity_id, "reason": reason,
                                      "send_mode": "original" if mode in ("skipped", "timeout") else mode})
            text = original + "\n" + sentence if mode == "appended" else original
        return self._send_to_ai(pid, original, text, None)

    def _send_to_ai(self, prompt_event_id: str, original: str, text: str, dec: Optional[dict]) -> dict[str, Any]:
        a = self.app
        with self.lock:
            task = self.task
            assert task is not None
            revised = text != original
            payload = {"prompt_event_id": prompt_event_id, "text": text, "revised": revised}
            if revised:
                payload["original_text"] = original
                payload["appended_text"] = text[len(original):].lstrip("\n")
            history = [{"role": m["role"], "content": m["content"]} for m in self._chat()
                       if m["role"] in ("user", "assistant")][-int(a.server_cfg["chat"]["max_history_messages"]):]
            self.emit("PROMPT_SENT", payload)
            sys_prompt = a.prompts["learner_system"]
            msgs = sys_prompt.messages(task_title=task.title, task_description=task.description)
            msgs = [m for m in msgs if m["role"] == "system"] + history + [{"role": "user", "content": text}]
        res = a.llm.complete("chat", msgs, timeout_s=float(a.server_cfg["llm"]["chat"]["timeout_s"]))
        with self.lock:
            call_id = self._llm_call("chat", sys_prompt.version, res)
            if not res.ok:
                self.emit("AI_ERROR", {"prompt_event_id": prompt_event_id, "llm_call_id": call_id, "error": res.error})
                return {"stage": "ai_error"}
            rid = f"r{self.n + 1}"
            self.emit("AI_RESPONSE", {"response_id": rid, "prompt_event_id": prompt_event_id, "text": res.text,
                                      "model": res.model, "llm_call_id": call_id})
        if not (dec and dec["support_type"] and dec["timing"] == "post_response"):
            return {"stage": "done"}
        # 回答後型：AI 回答は読み取り専用で表示しつつ、支援問題を生成・検証
        gen = a.generator.generate(dec["support_type"], dec["opportunity_id"], task, rid, res.text)
        with self.lock:
            attempts = []
            for at in gen.attempts:
                cid = self._llm_call("support:" + dec["support_type"],
                                     a.prompts[dec["support_type"]].version, at.llm) if at.llm else None
                attempts.append({"llm_call_id": cid, "reason": at.reason, "verification": at.verification,
                                 "elapsed_ms": at.elapsed_ms})
            if not gen.ok:
                self.emit("SUPPORT_INFEASIBLE", {"opportunity_id": dec["opportunity_id"],
                                                 "support_type": dec["support_type"], "reason": gen.reason,
                                                 "attempts": attempts})
                return {"stage": "done", "infeasible": gen.reason}
            self.emit("SUPPORT_SHOWN", {"opportunity_id": dec["opportunity_id"], "support_type": dec["support_type"],
                                        "timing": "post_response", "phase": task.phase, "prompt_event_id": prompt_event_id,
                                        "response_id": rid, "source_block": gen.source_block,
                                        "public": gen.public, "key": gen.key, "generation_attempts": attempts})
            return {"stage": "post_response_support"}

    # ================================================================ 支援UI
    def _require_support(self, opportunity_id: str) -> dict[str, Any]:
        s = self.support
        if s is None or s["opportunity_id"] != opportunity_id:
            raise BadRequest("no such support")
        if self.clock() > s["shown_t"] + self.app.server_cfg["support"]["max_duration_s"]:
            self.emit("SUPPORT_END", {"opportunity_id": opportunity_id, "reason": "timeout"})
            raise BadRequest("support timed out")
        return s

    def support_answer(self, opportunity_id: str, answer: Any) -> dict[str, Any]:
        if not self.flow.acquire(blocking=False):   # 採点中の二重送信を防ぐ
            raise Busy("grading in progress")
        try:
            r = self._support_answer(opportunity_id, answer)
        finally:
            self.flow.release()
        r["state"] = self.state()
        return r

    def _support_answer(self, opportunity_id: str, answer: Any) -> dict[str, Any]:
        with self.lock:
            s = self._require_support(opportunity_id)
            if s["done"] or s["support_type"] == "clarify_question":
                raise BadRequest("not answerable")
            kind = s["public"]["kind"]
            attempt = s["attempts"] + 1
        a = self.app
        extra: dict[str, Any] = {}
        if kind == "choice":
            if not isinstance(answer, int):
                raise BadRequest("answer must be choice index")
            result = grade_choice(s["key"], answer)
        elif kind == "code":
            if not isinstance(answer, str):
                raise BadRequest("answer must be code")
            result = grade_code(a.sandbox, s["key"], answer)
        elif kind == "text":
            if not isinstance(answer, str) or not answer.strip():
                raise BadRequest("answer must be text")
            if a.server_cfg["support"]["freeform_feedback"] == "rubric_llm":
                res, result = grade_text_llm(a.llm, a.prompts["grade"], s["public"], s["key"], answer,
                                             float(a.server_cfg["llm"]["grade"]["timeout_s"]))
                with self.lock:
                    extra["llm_call_id"] = self._llm_call("grade", a.prompts["grade"].version, res)
            else:
                # §13-3 案B：正誤判定はせず、観点と模範解答を見せて自己評価させる
                result = {"correct": None, "self_assess": True, "rubric": s["key"]["rubric"],
                          "model_answer": s["key"]["model_answer"]}
        else:
            raise BadRequest("unknown kind")
        max_attempts = 2   # 1問＋再挑戦1回
        done = bool(result.get("correct")) or attempt >= max_attempts or result.get("self_assess", False)
        if done:
            result = self._final_feedback(s, result)
        with self.lock:
            s = self._require_support(opportunity_id)
            self.emit("SUPPORT_ANSWER", {"opportunity_id": opportunity_id, "attempt": attempt, "answer": answer,
                                         "result": result, "done": done, **extra})
            return {"result": result, "done": done}

    def _final_feedback(self, s: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        """終了時に見せる情報（正誤フィードバックの後に終了）。"""
        r = dict(result)
        key = s["key"]
        st = s["support_type"]
        if st in ("predict_verify", "trace_predict"):
            r["actual_output"] = key["actual_output"]   # 実行結果と対比
            r["answer_index"] = key["answer_index"]
        elif st == "decompose":
            r["answer_index"] = key["answer_index"]
            r["explanation"] = key.get("explanation", "")
        return r

    def support_self_assess(self, opportunity_id: str, ratings: list[int]) -> dict[str, Any]:
        with self.lock:
            s = self._require_support(opportunity_id)
            self.emit("SUPPORT_ANSWER", {"opportunity_id": opportunity_id, "attempt": s["attempts"],
                                         "self_assessment": ratings, "result": None, "done": True})
            return {"state": self.state()}

    def support_hint(self, opportunity_id: str) -> dict[str, Any]:
        with self.lock:
            s = self._require_support(opportunity_id)
            if s["hint_used"] or s["attempts"] < 1 or s["done"]:
                raise BadRequest("hint not available")
            self.emit("SUPPORT_HINT", {"opportunity_id": opportunity_id, "hint": s["key"].get("hint", "")})
            return {"hint": s["key"].get("hint", ""), "state": self.state()}

    def support_run(self, opportunity_id: str, code: str, stdin: str) -> dict[str, Any]:
        if not self.flow.acquire(blocking=False):
            raise Busy("run in progress")
        try:
            return self._support_run(opportunity_id, code, stdin)
        finally:
            self.flow.release()

    def _support_run(self, opportunity_id: str, code: str, stdin: str) -> dict[str, Any]:
        with self.lock:
            s = self._require_support(opportunity_id)
            if s["public"]["kind"] != "code":
                raise BadRequest("not a code support")
        out = self.app.sandbox.run(code, [stdin])
        with self.lock:
            run = out.runs[0] if out.ok and out.runs else None
            self.emit("SUPPORT_RUN", {"opportunity_id": opportunity_id, "code": code, "code_hash": _h(code),
                                      "stdin": stdin, "ok": out.ok, "compile": out.compile, "run": run,
                                      "error": out.error})
            return {"ok": out.ok, "compile": out.compile, "run": run}

    def support_end(self, opportunity_id: str, reason: str) -> dict[str, Any]:
        if reason not in ("completed", "skipped", "aborted", "timeout"):
            raise BadRequest("reason")
        with self.lock:
            s = self.support
            if s is None or s["opportunity_id"] != opportunity_id:
                raise BadRequest("no such support")
            if s["support_type"] == "clarify_question":
                raise BadRequest("use clarify_submit")
            if reason == "skipped" and not self.app.server_cfg["support"]["allow_skip"]:
                raise BadRequest("skip not allowed")
            if reason == "completed" and not s["done"]:
                raise BadRequest("not completed")
            self.emit("SUPPORT_END", {"opportunity_id": opportunity_id, "reason": reason})
            return {"state": self.state()}


def _h(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]


_INFO_KEYS = {"user_agent": str, "platform": str, "mobile": bool, "touch": bool, "viewport_w": int,
              "viewport_h": int, "screen_w": int, "screen_h": int, "net_ms": int, "supported_browser": bool,
              "language": str, "timezone": str}


def _clean_info(info: Any) -> dict[str, Any]:
    """画面から来た環境情報のうち、決めた項目・型だけを残す。"""
    out: dict[str, Any] = {}
    if not isinstance(info, dict):
        return out
    for k, typ in _INFO_KEYS.items():
        v = info.get(k)
        if typ is int and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = int(v)
        elif typ is bool and isinstance(v, bool):
            out[k] = v
        elif typ is str and isinstance(v, str):
            out[k] = v[:300]
    return out


def _validate_answer(item: dict[str, Any], ans: Any) -> None:
    t = item.get("type")
    if ans is None or ans == "":
        return
    if t == "choice":
        if not isinstance(ans, int) or not 0 <= ans < len(item.get("choices", [])):
            raise BadRequest(f"{item['id']}: choice index")
    elif t == "multi":
        if not isinstance(ans, list) or not all(isinstance(a, int) and 0 <= a < len(item.get("choices", [])) for a in ans):
            raise BadRequest(f"{item['id']}: choices")
    elif t == "scale":
        lo, hi = item.get("min", 1), item.get("max", 7)
        if not isinstance(ans, int) or not lo <= ans <= hi:
            raise BadRequest(f"{item['id']}: scale")
    elif t in ("text", "code"):
        if not isinstance(ans, str) or len(ans) > 5000:
            raise BadRequest(f"{item['id']}: text")
    else:
        raise BadRequest(f"{item['id']}: unknown item type")


__all__ = ["ParticipantSession", "Busy", "BadRequest", "NotSynced", "OtherPage", "LLM_SUPPORT_TYPES"]
