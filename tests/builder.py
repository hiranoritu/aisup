"""テスト用のイベントログ生成器。フロントエンドが送るイベント列を模す。"""
from __future__ import annotations

from typing import Optional

from engine.events import Event


def F(h1=0, h2=0, h3=0, h4=0, followup=0, conf=0.95, asks=0, off=0) -> dict:
    """PROMPT_FEATURES の features（LLM 出力を模したもの）。"""
    spans = {k: [f"<{k}の根拠>"] for k, v in (("h1", h1), ("h2", h2), ("h3", h3), ("h4", h4)) if v}
    return {"h1": h1, "h2": h2, "h3": h3, "h4": h4, "is_followup": followup,
            "followup_confidence": conf, "asks_about_answer": asks, "off_task": off, "spans": spans}


def code_block(code: str) -> str:
    return f"説明です。\n\n```c\n{code}\n```\n\n以上です。"


PROG_A = """#include <stdio.h>
int main(void) {
    int a = 3;
    int *p = &a;
    printf("%d\\n", *p);
    return 0;
}"""

PROG_B = """#include <stdio.h>
void swap(int *x, int *y) {
    int tmp = *x;
    *x = *y;
    *y = tmp;
}
int main(void) {
    int a = 1, b = 2;
    swap(&a, &b);
    printf("%d %d\\n", a, b);
    return 0;
}"""


class Log:
    def __init__(self, pid: str = "p01", group: str = "adaptive") -> None:
        self.pid = pid
        self.events: list[Event] = []
        self.t = 0.0
        self.n = 0
        self.task_id = ""
        self.doc = ""
        self.add("SESSION_START", {"group": group})

    # 基本
    def add(self, typ: str, payload: Optional[dict] = None, dt: float = 1.0, in_support: bool = False) -> str:
        self.t += dt
        self.n += 1
        eid = f"e{self.n}"
        self.events.append(Event(eid, typ, self.t, self.pid, self.task_id, payload or {}, in_support))
        return eid

    def task(self, task_id: str, phase: str = "main", starter: str = "") -> None:
        self.task_id = task_id
        self.doc = starter
        self.add("TASK_START", {"phase": phase, "starter_code": starter})

    def end_task(self) -> None:
        self.add("TASK_END")

    # 質問と回答
    def ask(self, text: str, feats: Optional[dict], ok: bool = True) -> str:
        pid = self.add("PROMPT_DRAFT_SUBMIT", {"text": text})
        if ok:
            self.add("PROMPT_FEATURES", {"prompt_event_id": pid, "ok": True, "features": feats}, dt=0.5)
        else:
            self.add("PROMPT_FEATURES", {"prompt_event_id": pid, "ok": False, "error": "timeout"}, dt=0.5)
        self.add("PROMPT_SENT", {"prompt_event_id": pid, "text": text, "revised": False}, dt=0.1)
        return pid

    def respond(self, text: str, prompt_id: str = "") -> str:
        rid = f"r{self.n + 1}"
        self.add("AI_RESPONSE", {"response_id": rid, "prompt_event_id": prompt_id, "text": text}, dt=3)
        return rid

    # 編集
    def edit(self, offset: int, delete_len: int, insert: str, kind: str = "typed",
             source: Optional[dict] = None, in_support: bool = False) -> None:
        payload = {"offset": offset, "delete_len": delete_len, "insert_text": insert, "kind": kind}
        if source:
            payload["source"] = source
        self.add("CODE_EDIT", payload, in_support=in_support)
        self.doc = self.doc[:offset] + insert + self.doc[offset + delete_len:]

    def paste_replace_all(self, code: str, response_id: str, block_id: str = "0") -> None:
        self.edit(0, len(self.doc), code, "paste", {"type": "ai", "response_id": response_id, "block_id": block_id})

    def type_replace_all(self, code: str) -> None:
        self.edit(0, len(self.doc), code, "typed")

    def type_at(self, anchor: str, text: str, after: bool = True) -> None:
        i = self.doc.index(anchor) + (len(anchor) if after else 0)
        self.edit(i, 0, text, "typed")

    def snapshot(self, text: Optional[str] = None) -> None:
        self.add("CODE_SNAPSHOT", {"text": self.doc if text is None else text})

    def run(self, stdin: str = "", in_support: bool = False) -> None:
        self.add("RUN", {"stdin": stdin, "stdout": "", "exit_code": 0}, in_support=in_support)

    # 支援
    def support(self, support_type: str, opportunity_id: str, runs_inside: int = 0) -> None:
        self.add("SUPPORT_SHOWN", {"opportunity_id": opportunity_id, "support_type": support_type})
        for _ in range(runs_inside):
            self.add("SUPPORT_RUN", {"stdin": "5"}, in_support=True)
            self.add("RUN", {"stdin": "5", "exit_code": 0}, in_support=True)
        self.add("SUPPORT_ANSWER", {"opportunity_id": opportunity_id, "answer": "x"})
        self.add("SUPPORT_END", {"opportunity_id": opportunity_id, "reason": "completed"})

    def infeasible(self, opportunity_id: str, reason: str = "generation_failed") -> None:
        self.add("SUPPORT_INFEASIBLE", {"opportunity_id": opportunity_id, "reason": reason})

    # よく使うサイクルの型
    def cycle_passive_adopt(self, feats: dict, prog: str = PROG_A) -> None:
        """AIコードをそのまま貼って1回実行（RU=Passive の典型）。"""
        p = self.ask("質問", feats)
        r = self.respond(code_block(prog), p)
        self.paste_replace_all(prog, r)
        self.run("")

    def cycle_active_modify(self, feats: dict, prog: str = PROG_A) -> None:
        """AIコードを貼り、処理を手で書き足して1回実行（RU=Active の典型）。"""
        p = self.ask("質問", feats)
        r = self.respond(code_block(prog), p)
        self.paste_replace_all(prog, r)
        self.type_at("int *p = &a;", "\n    *p = *p + 10;\n    if (*p > 5) {\n        printf(\"big\\n\");\n    }")
        self.run("")
