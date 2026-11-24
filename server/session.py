"""参加者ごとのセッション：イベントの保存 → エンジンへの投入 → 画面用の状態。

- すべての状態変化はイベントとして保存してから _apply() で反映する。再起動時は同じ _apply() で
  保存済みイベントを先頭から再生して復元する（本番と再生が同じ経路を通る）。
- 外部呼び出し（LLM・サンドボックス）の結果はイベントとして保存してから使う。
- 時刻はサーバ時刻。同一参加者内で狭義単調増加にする（同時刻による順序の曖昧さを避ける）。
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
from .support import (LLM_SUPPORT_TYPES, build_clarify, grade_choice, grade_code, grade_text_llm)
from .tasks import Task

if TYPE_CHECKING:
    from .app import App

CLIENT_EVENT_TYPES = {"CODE_EDIT", "COPY_FROM_AI", "CODE_SNAPSHOT", "CLIENT_INFO", "MATERIAL_VIEW"}
EDIT_KINDS = {"typed", "paste", "undo", "redo", "other"}


class Busy(Exception):
    pass


class BadRequest(Exception):
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
        self.flow = threading.Lock()           # 質問の送信フロー（同時に1つ）
        self.engine = Engine(app.engine_cfg)
        self.n = 0
        self.last_t = 0.0
        # 画面用の派生状態（_apply で更新）
        self.started = False
        self.ended = False
        self.task: Optional[Task] = None
        self.task_idx = -1
        self.chat: dict[str, list[dict[str, Any]]] = {}
        self.last_copy: Optional[dict[str, Any]] = None
        self.last_compile: Optional[dict] = None
        self.last_run: Optional[dict] = None
        self.support: Optional[dict[str, Any]] = None
        self.pending: dict[str, dict[str, Any]] = {}   # P-A 中の未送信の質問
        self.last_decision: Optional[dict] = None
        self.version = 0                               # 課題切替などで増える（画面の再読込用）

    # ================================================================ イベント
    def restore(self, events: list[Event]) -> None:
        with self.lock:
            for ev in events:
                self.n = max(self.n, int(ev.event_id.lstrip("e") or 0))
                self.last_t = max(self.last_t, ev.t)
                self._apply(ev)

    def emit(self, typ: str, payload: Optional[dict[str, Any]] = None, task_id: Optional[str] = None) -> Event:
        with self.lock:
            self.n += 1
            t = max(self.clock(), self.last_t + 0.001)
            self.last_t = t
            in_support = self.support is not None and typ != "SUPPORT_SHOWN"
            ev = Event(f"e{self.n}", typ, round(t, 3), self.pid,
                       task_id if task_id is not None else (self.task.task_id if self.task else ""),
                       payload or {}, in_support)
            self.app.store.append(ev)
            self._apply(ev)
            return ev

    def _apply(self, ev: Event) -> None:
        dec = self.engine.ingest(ev)
        p = ev.payload
        typ = ev.type
        if typ == "SESSION_START":
            self.started = True
        elif typ == "TASK_START":
            self.task = self.app.task_by_id[ev.task_id]
            self.task_idx = self.app.task_ids.index(ev.task_id)
            self.chat.setdefault(ev.task_id, [])
            self.last_copy = None
            self.last_compile = self.last_run = None
            self.pending.clear()
            self.version += 1
        elif typ == "SESSION_END":
            self.ended = True
            self.task = None
            self.version += 1
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

    # ================================================================ 画面状態
    def state(self) -> dict[str, Any]:
        with self.lock:
            sc = self.app.server_cfg
            chat = []
            for m in self.chat.get(self.task.task_id, []) if self.task else []:
                item = {"role": m["role"], "content": m["content"]}
                if m["role"] == "assistant":
                    item["response_id"] = m["response_id"]
                    item["segments"] = segments(m["content"])
                chat.append(item)
            return {
                "participant_id": self.pid, "started": self.started, "ended": self.ended,
                "version": self.version,
                "task": self.task.public() if self.task else None,
                "task_index": self.task_idx, "task_count": len(self.app.task_ids),
                "materials": [{"id": m["id"], "title": m["title"]} for m in self.app.materials
                              if not m.get("phases") or (self.task and self.task.phase in m["phases"])],
                "code": self.engine.doc.text if self.task else "",
                "chat": chat,
                "support": self._support_public(),
                "busy": self.flow.locked(),
                "settings": {"allow_skip": sc["support"]["allow_skip"],
                             "block_copy_during_support": sc["support"]["block_copy_during_support"],
                             "max_duration_s": sc["support"]["max_duration_s"],
                             "snapshot_interval_s": sc["snapshot_interval_s"],
                             "freeform_feedback": sc["support"]["freeform_feedback"]},
            }

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

    # ================================================================ 管理操作
    def start_session(self, assignment: str) -> None:
        with self.lock:
            if self.started:
                return
            self.emit("SESSION_START", {
                "group": self.group, "assignment": assignment,
                "rule_version": self.engine.rule_version, "config_hash": self.engine.cfg_hash,
                "server_config_hash": self.app.server_cfg_hash, "task_sequence": self.app.task_ids,
                "sandbox_image": self.app.server_cfg["sandbox"]["image"]}, task_id="")

    def advance(self) -> str:
        """次の課題へ（管理画面）。支援中なら中断として閉じる。"""
        if not self.flow.acquire(timeout=30):
            raise Busy("prompt in flight")
        try:
            with self.lock:
                if self.ended:
                    return "ended"
                if self.support is not None:
                    self.emit("SUPPORT_END", {"opportunity_id": self.support["opportunity_id"], "reason": "aborted",
                                              "by": "admin_advance"})
                if self.task is not None:
                    self.emit("TASK_END", {"by": "admin"})
                nxt = self.task_idx + 1
                if nxt >= len(self.app.task_ids):
                    self.emit("SESSION_END", {"by": "admin"}, task_id="")
                    return "ended"
                t = self.app.tasks[nxt]
                self.emit("TASK_START", {"phase": t.phase, "starter_code": t.starter_code, "title": t.title},
                          task_id=t.task_id)
                return t.task_id
        finally:
            self.flow.release()

    # ================================================================ エディタ・コピー
    def client_events(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        with self.lock:
            if self.task is None:
                raise BadRequest("no active task")
            for it in items:
                typ = it.get("type")
                if typ not in CLIENT_EVENT_TYPES:
                    raise BadRequest(f"type not allowed: {typ}")
                p = dict(it.get("payload") or {})
                p["client_t"] = it.get("client_t")
                if typ == "CODE_EDIT":
                    p = self._normalize_edit(p)
                elif typ == "COPY_FROM_AI":
                    if self.support is not None and self.app.server_cfg["support"]["block_copy_during_support"]:
                        p["blocked"] = True   # §13-4：支援中のコピーは禁止（試みのみ記録）
                elif typ == "CODE_SNAPSHOT":
                    if not isinstance(p.get("text"), str):
                        raise BadRequest("snapshot text")
                elif typ == "MATERIAL_VIEW":
                    if p.get("material_id") not in self.app.material_by_id or p.get("action") not in ("open", "close"):
                        raise BadRequest("material_view")
                    m = self.app.material_by_id[p["material_id"]]
                    p["material_version"] = m["version"]
                self.emit(typ, p)
            return {"ok": True, "version": self.version}

    def _normalize_edit(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            p["offset"] = int(p["offset"])
            p["delete_len"] = int(p.get("delete_len", 0))
        except (KeyError, TypeError, ValueError):
            raise BadRequest("edit offset/delete_len")
        if not isinstance(p.get("insert_text", ""), str):
            raise BadRequest("edit insert_text")
        p.setdefault("insert_text", "")
        if p.get("kind") not in EDIT_KINDS:
            p["kind"] = "other"
        p.pop("source", None)   # 由来はサーバが決める
        if p["kind"] == "paste":
            # 仕様 §2：直前の COPY_FROM_AI と文字列が一致すれば source=ai と由来を付ける
            c = self.last_copy
            if c is not None and _norm_clip(c.get("text", "")) == _norm_clip(p["insert_text"]) and p["insert_text"].strip():
                p["source"] = {"type": "ai", "response_id": c.get("response_id"), "block_id": c.get("block_id")}
            else:
                p["source"] = {"type": "external"}
        return p

    # ================================================================ 実行
    def run_code(self, code: str, stdin: str) -> dict[str, Any]:
        with self.lock:
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
        r["state"] = self.state()   # フロー終了後の状態（busy=false）を返す
        return r

    def _submit_prompt(self, text: str) -> dict[str, Any]:
        with self.lock:
            if self.task is None:
                raise BadRequest("no active task")
            if self.support is not None:
                raise BadRequest("support active")
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


__all__ = ["ParticipantSession", "Busy", "BadRequest", "LLM_SUPPORT_TYPES"]
