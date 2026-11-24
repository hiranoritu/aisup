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
        if 'printf("ok\\n")' in src:                      # 環境確認の試し実行
            return RunOutcome(True, {"ok": True, "exit_code": 0, "diagnostics": ""},
                              [{"stdin": s, "stdout": "ok\n", "stderr": "", "exit_code": 0, "timed_out": False,
                                "truncated": False, "signal": None, "limit": None, "sanitizer": False} for s in stdins],
                              image="fake")
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
        if role in ("features", "features_shadow"):
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
    # 実験の流れはテスト用に固定する（本番の phases を変えてもテストが壊れないように）
    base = {"llm_provider": "fake", "phases": TEST_PHASES, "allocation_file": None}
    cfg = make_server_config({**base, **(server_overrides or {})})
    kw = {} if clock is None else {"clock": clock}
    app = App(cfg, str(tmp_path), llm=FuncLLM(script, "scripted"), sandbox=sandbox or FakeSandbox(),
              admin_token="admintoken", **kw)
    app.background = lambda fn: fn()      # 裏の処理（比較用の特徴抽出）をテストでは同期で行う
    return app


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


TEST_PHASES = [
    {"id": "consent", "kind": "consent", "time_limit_s": None, "doc": "instruments/consent.md"},
    {"id": "envcheck", "kind": "envcheck", "time_limit_s": None},
    {"id": "intro", "kind": "tasks", "time_limit_s": 600, "tasks": ["intro_sum"]},
    {"id": "pretest", "kind": "test", "time_limit_s": 600, "instrument": "instruments/pretest.json"},
    {"id": "main", "kind": "tasks", "time_limit_s": 3000, "tasks": ["main_swap", "main_minmax"]},
    {"id": "post", "kind": "tasks", "time_limit_s": 1500, "tasks": ["post_reverse"]},
    {"id": "posttest", "kind": "test", "time_limit_s": 600, "instrument": "instruments/posttest.json"},
    {"id": "questionnaire", "kind": "questionnaire", "time_limit_s": None, "instrument": "instruments/questionnaire.json"},
]
PC_INFO = {"user_agent": "test", "mobile": False, "viewport_w": 1400, "net_ms": 50, "supported_browser": True}


def cev(s, items, page_id=None):
    """画面からのイベントを、連番を付けて送る（テスト用）。"""
    seq = s.next_seq
    out = []
    for it in items:
        out.append({**it, "client_seq": seq, "client_t": it.get("client_t", seq * 100), "page_id": page_id or s.page_id})
        seq += 1
    return s.client_events(out, page_id or s.page_id)


def to_phase(s, phase_id):
    """指定したフェーズまで進める（課題は合格させ、テストは空のまま提出）。"""
    while s.phase()["id"] != phase_id:
        kind = s.phase()["kind"]
        if kind == "consent":
            s.consent(True)
        elif kind == "envcheck":
            s.env_check(PC_INFO)
        elif kind == "tasks":
            s.task_submit(s.engine.doc.text)
            s.task_next()
        elif kind == "test":
            s.save_answers({}, final=True)
        else:
            raise AssertionError(kind)


def last_assistant(s):
    return [m for m in s.state()["chat"] if m["role"] == "assistant"][-1]


def paste_from_response(s, replace_all: bool = True):
    """最新の AI 回答の最初のコードブロックをコピーして、エディタ全体を置き換えて貼り付ける。"""
    m = last_assistant(s)
    seg = next(x for x in m["segments"] if x["type"] == "code")
    cev(s, [
        {"type": "COPY_FROM_AI", "payload": {"response_id": m["response_id"], "block_id": seg["block_id"],
                                             "text": seg["code"]}},
        {"type": "CODE_EDIT", "payload": {"offset": 0, "delete_len": len(s.engine.doc.text) if replace_all else 0,
                                          "insert_text": seg["code"], "kind": "paste"}},
    ])
    return seg["code"]


def type_at(s, anchor: str, text: str):
    doc = s.engine.doc.text
    i = doc.index(anchor) + len(anchor)
    cev(s, [{"type": "CODE_EDIT", "payload": {"offset": i, "delete_len": 0, "insert_text": text,
                                               "kind": "typed"}}])


def start(app, group: str, phase: str = "main"):
    """参加者を作り、同意→環境確認→練習→事前テストを経て主課題（main_swap）まで進める。"""
    p = app.create_participant(None, group)
    s = app.session(p["participant_id"])
    s.hello("page1", PC_INFO)
    to_phase(s, phase)
    return s


def close_cycle(s):
    """開いているサイクルを閉じる（フェーズを先へ進める）。"""
    s.force_next_phase()


__all__ = ["FakeSandbox", "Script", "make_app", "Clock", "last_assistant", "paste_from_response", "type_at", "start",
           "cev", "to_phase", "close_cycle", "TEST_PHASES", "PC_INFO", "F", "PROG_A", "PROG_B", "code_block"]
