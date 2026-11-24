"""AI 回答を画面表示用の区切り（文章・コードブロック）に分ける。

block_id は engine/codeblocks.py と同じ正規表現・同じ採番（フェンスの出現順、0始まり）。
フロントはこの block_id をそのまま COPY_FROM_AI に入れる（由来追跡の一致を保証する）。
"""
from __future__ import annotations

from typing import Any

from engine.codeblocks import _FENCE


def segments(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pos = 0
    for idx, m in enumerate(_FENCE.finditer(text)):
        if m.start() > pos:
            out.append({"type": "text", "text": text[pos:m.start()]})
        out.append({"type": "code", "block_id": str(idx), "lang": m.group(1).strip().lower(), "code": m.group(2)})
        pos = m.end()
    if pos < len(text):
        out.append({"type": "text", "text": text[pos:]})
    return out
