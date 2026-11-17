"""HS：LLM 抽出特徴の検証と決定規則（仕様 §3）。

LLM 呼び出し自体はこのモジュールに含めない。サーバが LLM を呼び、
結果を PROMPT_FEATURES イベントとして保存し、エンジンはその保存値だけを使う（再生可能性, §10）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

BINARY_KEYS = ("h1", "h2", "h3", "h4", "is_followup", "asks_about_answer", "off_task")


@dataclass(frozen=True)
class PromptFeatures:
    h1: int  # 完成コード要求
    h2: int  # 問題・構文・エラーの具体的指示
    h3: int  # 自分の仮説や考えの提示
    h4: int  # 希望する支援形式の主体的指定
    is_followup: int          # 直前の AI 回答・同じ問題への確認質問か
    followup_confidence: float
    asks_about_answer: int    # 回答の内容・正しさを問う（Q 用。確認質問のときのみ意味を持つ）
    off_task: int             # 学習目標外（課題と無関係な雑談・環境設定など）
    spans: dict[str, list[str]] = field(default_factory=dict)


def parse_prompt_features(raw: Any) -> tuple[Optional[PromptFeatures], str]:
    """LLM の生出力（str か dict）を検証する。失敗時は (None, 理由)。"""
    try:
        obj = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (ValueError, TypeError) as e:
        return None, f"json_parse_error:{type(e).__name__}"
    vals: dict[str, Any] = {}
    for k in BINARY_KEYS:
        v = obj.get(k)
        if isinstance(v, bool):
            v = int(v)
        if v not in (0, 1):
            return None, f"invalid_{k}"
        vals[k] = v
    conf = obj.get("followup_confidence", 1.0)
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        return None, "invalid_followup_confidence"
    if not 0.0 <= conf <= 1.0:
        return None, "invalid_followup_confidence"
    spans = obj.get("spans") or {}
    if not isinstance(spans, dict):
        return None, "invalid_spans"
    # 1 と判定した h 特徴には根拠スパンが必須（仕様 §3）
    for k in ("h1", "h2", "h3", "h4"):
        if vals[k] == 1 and not spans.get(k):
            return None, f"missing_span_{k}"
    return PromptFeatures(followup_confidence=conf, spans={k: list(v) for k, v in spans.items()}, **vals), ""


@dataclass(frozen=True)
class HSResult:
    label: Optional[str]          # "Passive"|"Active"|"Constructive"|None(=Unknown)
    reason: str
    conflict: bool = False        # h1=1 かつ (h3=1 or h4=1)


def classify_hs(f: Optional[PromptFeatures], parse_error: str = "") -> HSResult:
    if f is None:
        return HSResult(None, f"features_unavailable:{parse_error or 'missing'}")
    conflict = f.h1 == 1 and (f.h3 == 1 or f.h4 == 1)
    if f.h3 == 1 or f.h4 == 1:
        return HSResult("Constructive", "h3_or_h4", conflict)
    if f.h2 == 1:
        return HSResult("Active", "h2", conflict)
    return HSResult("Passive", "none_of_h2_h3_h4", conflict)
