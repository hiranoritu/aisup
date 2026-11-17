"""AI 回答からの C コード候補の抽出（仕様 §4）。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .clex import lex

_FENCE = re.compile(r"```[ \t]*([\w+#.-]*)[^\n]*\n(.*?)(?:```|\Z)", re.S)
_C_LANGS = {"c", "h", ""}


@dataclass(frozen=True)
class CodeBlock:
    response_id: str
    block_id: str   # response_id 内の通し番号（フロントのコピー追跡と同じ採番にする）
    lang: str
    code: str


def extract_c_blocks(response_id: str, text: str) -> list[CodeBlock]:
    """フェンス付きコードブロックのうち C らしいものを返す。

    block_id はフェンスの出現順（0始まり）。言語指定のない出力例などを除くため、
    `;` か `{` を1つ以上含むものだけを C コードとみなす。
    """
    blocks = []
    for idx, m in enumerate(_FENCE.finditer(text)):
        lang = m.group(1).strip().lower()
        code = m.group(2)
        if lang not in _C_LANGS:
            continue
        vals = lex(code).values
        if ";" not in vals and "{" not in vals:
            continue
        blocks.append(CodeBlock(response_id, str(idx), lang, code))
    return blocks
