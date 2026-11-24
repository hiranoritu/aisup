"""エディタ内容の再構成と、文字ごとの由来追跡（仕様 §2, §4 の C・I）。

由来ラベルは "種別@サイクルID[@付加情報]"。
  starter         : 課題の初期コード
  typed@c3        : サイクル c3 中の打鍵
  ai@c3@r12:0     : サイクル c3 中に AI 回答 r12 のブロック 0 から貼り付け
  external@c3     : AI 以外からの貼り付け
  other@c3        : 取り消し・やり直し等、由来を特定できない挿入
  unknown         : スナップショットとの不一致で再同期した文字
"""
from __future__ import annotations

from dataclasses import dataclass


def origin_kind(label: str) -> str:
    return label.split("@", 1)[0]


def origin_cycle(label: str) -> str | None:
    parts = label.split("@")
    return parts[1] if len(parts) > 1 else None


@dataclass
class EditResult:
    ok: bool
    reason: str = ""


class OriginDoc:
    def __init__(self, text: str = "", origin: str = "starter") -> None:
        self.chars: list[str] = list(text)
        self.origins: list[str] = [origin] * len(text)

    @property
    def text(self) -> str:
        return "".join(self.chars)

    def apply(self, offset: int, delete_len: int, insert: str, origin: "str | list[str]") -> EditResult:
        """origin は1つのラベル、または挿入文字と同じ長さのラベル列（エディタ内の貼り付けで由来を引き継ぐ）。"""
        n = len(self.chars)
        if offset < 0 or delete_len < 0 or offset + delete_len > n:
            return EditResult(False, f"out_of_range(offset={offset},delete={delete_len},len={n})")
        labels = [origin] * len(insert) if isinstance(origin, str) else list(origin)
        if len(labels) != len(insert):
            return EditResult(False, "origin_length_mismatch")
        self.chars[offset:offset + delete_len] = list(insert)
        self.origins[offset:offset + delete_len] = labels
        return EditResult(True)

    def resync(self, text: str) -> None:
        """スナップショットに合わせて内容を置き換える。由来は unknown。

        共通の接頭辞・接尾辞の由来は保持する（不一致部分だけを unknown にする）。
        """
        old = self.chars
        new = list(text)
        p = 0
        while p < len(old) and p < len(new) and old[p] == new[p]:
            p += 1
        s = 0
        while (s < len(old) - p and s < len(new) - p
               and old[len(old) - 1 - s] == new[len(new) - 1 - s]):
            s += 1
        mid = len(new) - p - s
        self.origins = self.origins[:p] + ["unknown"] * mid + (self.origins[len(old) - s:] if s else [])
        self.chars = new

    def origins_for(self, start: int, end: int) -> list[str]:
        return self.origins[start:end]

    def copy(self) -> "OriginDoc":
        d = OriginDoc()
        d.chars = list(self.chars)
        d.origins = list(self.origins)
        return d


def edit_origin(payload: dict, cycle_id: str) -> str:
    kind = payload.get("kind", "other")
    cid = cycle_id or "none"
    if kind == "typed":
        return f"typed@{cid}"
    if kind == "paste":
        src = payload.get("source") or {}
        if src.get("type") == "ai" and src.get("response_id") is not None:
            return f"ai@{cid}@{src['response_id']}:{src.get('block_id', '')}"
        return f"external@{cid}"
    return f"other@{cid}"
