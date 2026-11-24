"""イベントスキーマ（仕様 §2）。

ログは追記専用の JSON Lines。1行1イベント。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Iterator

EVENT_TYPES = frozenset({
    "SESSION_START",        # {group: "adaptive"|"uniform"}
    "TASK_START",           # {phase: "intro"|"main"|"post", starter_code: str}
    "TASK_END",
    "PROMPT_DRAFT_SUBMIT",  # {text}  学習者が送信ボタンを押した時点の原文
    "PROMPT_FEATURES",      # {prompt_event_id, ok, features?, error?, model?, raw?}  LLM抽出結果
    "PROMPT_SENT",          # {prompt_event_id, text, revised: bool}
    "AI_RESPONSE",          # {response_id, prompt_event_id, text}
    "CODE_EDIT",            # {offset, delete_len, insert_text, kind: typed|paste|undo|redo|other,
                            #  source?: {type: ai|external, response_id?, block_id?}}
    "COPY_FROM_AI",         # {response_id, block_id, text}
    "CODE_SNAPSHOT",        # {text}
    "COMPILE",              # {ok, diagnostics}
    "RUN",                  # {stdin, stdout, exit_code, error}
    "SUPPORT_SHOWN",        # {opportunity_id, support_type, phase}
    "SUPPORT_ANSWER",
    "SUPPORT_HINT",
    "SUPPORT_RUN",          # 支援UI内の実行。RU から除外
    "SUPPORT_END",          # {opportunity_id, reason: completed|skipped|aborted|timeout}
    "SUPPORT_INFEASIBLE",   # {opportunity_id, reason}
    "ADOPT_UNCHANGED",      # 明確な無変更採用イベント（UIに採用ボタンを置く場合）
    # 以下はサーバが記録する監査用イベント。エンジンの判定には影響しない（ハンドラなし）。
    "SESSION_END",
    "LLM_CALL",             # {purpose, model, params, prompt_version, messages, text, raw, ok, error, latency_ms}
    "DECISION",             # 送信時点の介入判断（Decision.to_dict()）。再生でも同じものが出る
    "AI_ERROR",             # {prompt_event_id, llm_call_id, error}  学習者用AIの応答失敗
    "SANDBOX_ERROR",        # {error, image}  実行環境の障害（学習者のコードの誤りではない）
    "CLIENT_INFO",          # 画面側の補助情報（ページ再読込など）
    "MATERIAL_VIEW",        # {material_id, action: open|close, material_version}  教材の閲覧
})


@dataclass(frozen=True)
class Event:
    event_id: str
    type: str
    t: float                 # サーバ時刻（秒）
    participant_id: str
    task_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    in_support: bool = False

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Event":
        return Event(
            event_id=str(d["event_id"]),
            type=str(d["type"]),
            t=float(d["t"]),
            participant_id=str(d.get("participant_id", "")),
            task_id=str(d.get("task_id", "")),
            payload=dict(d.get("payload") or {}),
            in_support=bool(d.get("in_support", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_jsonl(path: str) -> list[Event]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(Event.from_dict(json.loads(line)))
    return out


def dump_jsonl(events: Iterable[Event], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")


def ordered(events: Iterable[Event]) -> list[Event]:
    """時刻順（同時刻は入力順）に並べる。"""
    indexed = list(enumerate(events))
    indexed.sort(key=lambda p: (p[1].t, p[0]))
    return [e for _, e in indexed]
