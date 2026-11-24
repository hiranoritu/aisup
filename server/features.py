"""質問特徴の抽出（仕様 §3, §13-1 案A）。prompts/prompt_features.md を使う。

検証（0/1・根拠スパン必須など）はエンジン側の parse_prompt_features が行う。
ここでは LLM 呼び出しと JSON の読み取りだけを行い、失敗は ok:false として返す（Unknown にするのはエンジン）。
"""
from __future__ import annotations

from typing import Any

from .llm import LLMClient, LLMResult, parse_json_text
from .prompts import Prompt
from .tasks import Task


def extract_features(llm: LLMClient, prompt: Prompt, task: Task, prev_response: str, text: str,
                     timeout_s: float) -> tuple[LLMResult, dict[str, Any]]:
    msgs = prompt.messages(task_title=task.title, prev_response_excerpt=(prev_response or "（なし）")[:400],
                           prompt_text=text)
    res = llm.complete("features", msgs, json_mode=True, timeout_s=timeout_s)
    out: dict[str, Any] = {"model": res.model, "prompt_version": prompt.version, "raw": res.text}
    if not res.ok:
        out.update(ok=False, error="llm_failed:" + res.error)
        return res, out
    try:
        feats = parse_json_text(res.text)
    except ValueError as e:
        out.update(ok=False, error=f"json_parse_error:{e}")
        return res, out
    if not isinstance(feats, dict):
        out.update(ok=False, error="json_not_object")
        return res, out
    out.update(ok=True, features=feats)
    return res, out
