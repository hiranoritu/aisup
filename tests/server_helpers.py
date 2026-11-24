"""サーバのテスト用部品：台本どおりに答える LLM と、決定的な偽サンドボックス。"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

from server.app import App
from server.llm import FuncLLM
from server.sandbox import RunOutcome
from server.settings import make_server_config

from builder import F, PROG_A, PROG_B, code_block


class FakeSandbox:
    """コンパイルは常に成功。出力は outputs[stdin]（なければ "out\\n"）。呼び出しを記録する。"""

    def __init__(self, outputs: Optional[dict[str, str]] = None, fail_compile: bool = False) -> None:
        self.outputs = outputs or {}
        self.fail_compile = fail_compile
        self.calls: list[tuple[str, list[str]]] = []

    def available(self) -> bool:
        return True

    def run(self, src: str, stdins: list[str]) -> RunOutcome:
        self.calls.append((src, list(stdins)))
        if self.fail_compile or "SYNTAX_ERROR" in src:
            return RunOutcome(True, {"ok": False, "exit_code": 1, "diagnostics": "main.c:1:1: error: expected ';'"}, [],
                              image="fake")
        runs = [{"stdin": s, "stdout": self.outputs.get(s, "out\n"), "stderr": "", "exit_code": 0,
                 "timed_out": False, "truncated": False, "signal": None, "limit": None, "sanitizer": False}
                for s in stdins]
        return RunOutcome(True, {"ok": True, "exit_code": 0, "diagnostics": ""}, runs, image="fake")


class Script:
    """質問文ごとに特徴を、役割ごとに応答を返す LLM の台本。"""

    def __init__(self) -> None:
        self.features: dict[str, dict] = {}
        self.chat: Callable[[str], str] = lambda q: code_block(PROG_A)
        self.support: Callable[[str, list], str] = lambda sysmsg, msgs: "{}"
        self.grade: Callable[[str], str] = lambda u: json.dumps({"criteria": [], "feedback": ""})
        self.fail_roles: set[str] = set()
        self.calls: list[str] = []

    def __call__(self, role: str, messages: list[dict]) -> str:
        self.calls.append(role)
        if role in self.fail_roles:
            raise TimeoutError("scripted failure")
        user = messages[-1]["content"]
        if role == "features":
            q = re.search(r"学習者の質問: (.*)$", user, re.S).group(1).strip()
            return json.dumps(self.features.get(q, F(h2=1)), ensure_ascii=False)
        if role == "chat":
            return self.chat(user)
        if role == "support":
            return self.support(messages[0]["content"], messages)
        if role == "grade":
            return self.grade(user)
        raise ValueError(role)


def make_app(tmp_path, script: Script, sandbox=None, server_overrides: Optional[dict] = None,
             clock=None) -> App:
    # 課題の並びはテスト用に固定する（本番の task_sequence を変えてもテストが壊れないように）
    base = {"llm_provider": "fake", "task_sequence": ["intro_sum", "main_swap", "main_minmax", "post_reverse"]}
    cfg = make_server_config({**base, **(server_overrides or {})})
    kw = {} if clock is None else {"clock": clock}
    return App(cfg, str(tmp_path), llm=FuncLLM(script, "scripted"), sandbox=sandbox or FakeSandbox(),
               admin_token="admintoken", **kw)


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


def last_assistant(s):
    return [m for m in s.state()["chat"] if m["role"] == "assistant"][-1]


def paste_from_response(s, replace_all: bool = True):
    """最新の AI 回答の最初のコードブロックをコピーして、エディタ全体を置き換えて貼り付ける。"""
    m = last_assistant(s)
    seg = next(x for x in m["segments"] if x["type"] == "code")
    s.client_events([
        {"type": "COPY_FROM_AI", "payload": {"response_id": m["response_id"], "block_id": seg["block_id"],
                                             "text": seg["code"]}},
        {"type": "CODE_EDIT", "payload": {"offset": 0, "delete_len": len(s.engine.doc.text) if replace_all else 0,
                                          "insert_text": seg["code"], "kind": "paste"}},
    ])
    return seg["code"]


def type_at(s, anchor: str, text: str):
    doc = s.engine.doc.text
    i = doc.index(anchor) + len(anchor)
    s.client_events([{"type": "CODE_EDIT", "payload": {"offset": i, "delete_len": 0, "insert_text": text,
                                                       "kind": "typed"}}])


def start(app, group: str):
    p = app.create_participant(None, group)
    s = app.session(p["participant_id"])
    s.advance()          # intro
    s.advance()          # main_swap（支援ありフェーズ）
    return s


__all__ = ["FakeSandbox", "Script", "make_app", "Clock", "last_assistant", "paste_from_response", "type_at", "start",
           "F", "PROG_A", "PROG_B", "code_block"]
