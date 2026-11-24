"""LLM 呼び出し（OpenAI Chat Completions）と開発用の偽 LLM。

呼び出しはすべて LLMResult として返し、サーバが LLM_CALL イベントとして保存する（仕様 §1 【v1.1】）。
エンジンは LLM を呼ばない。再生は保存済みの出力を使う。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional


@dataclass
class LLMResult:
    ok: bool
    text: str
    model: str
    params: dict[str, Any]
    messages: list[dict[str, str]]
    latency_ms: int
    error: str = ""
    raw: Optional[dict[str, Any]] = None     # API の生レスポンス
    usage: dict[str, Any] = field(default_factory=dict)

    def to_payload(self, purpose: str, prompt_version: str) -> dict[str, Any]:
        d = asdict(self)
        d["purpose"] = purpose
        d["prompt_version"] = prompt_version
        return d


class LLMClient:
    def complete(self, role: str, messages: list[dict[str, str]], *, json_mode: bool = False,
                 timeout_s: Optional[float] = None) -> LLMResult:
        raise NotImplementedError


class OpenAIClient(LLMClient):
    def __init__(self, api_key: str, role_cfg: dict[str, dict], base_url: str) -> None:
        self._key = api_key
        self._roles = role_cfg
        self._base = base_url.rstrip("/")

    def complete(self, role, messages, *, json_mode=False, timeout_s=None) -> LLMResult:
        rc = self._roles[role]
        model, params = rc["model"], dict(rc.get("params") or {})
        body: dict[str, Any] = {"model": model, "messages": messages, **params}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            self._base + "/chat/completions", data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
        t0 = time.monotonic()
        timeout = timeout_s if timeout_s is not None else rc.get("timeout_s", 30)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            return LLMResult(False, "", model, params, messages, _ms(t0), f"http_{e.code}:{detail}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return LLMResult(False, "", model, params, messages, _ms(t0), f"network:{type(e).__name__}:{e}")
        except ValueError as e:
            return LLMResult(False, "", model, params, messages, _ms(t0), f"bad_json:{e}")
        try:
            text = raw["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return LLMResult(False, "", model, params, messages, _ms(t0), "unexpected_response", raw)
        return LLMResult(True, text, raw.get("model", model), params, messages, _ms(t0), "", raw,
                         raw.get("usage") or {})


class FuncLLM(LLMClient):
    """関数で応答を決める LLM（テスト・開発用）。fn(role, messages) -> str（例外で失敗を表す）。"""

    def __init__(self, fn: Callable[[str, list[dict[str, str]]], str], model: str = "fake") -> None:
        self._fn = fn
        self._model = model

    def complete(self, role, messages, *, json_mode=False, timeout_s=None) -> LLMResult:
        t0 = time.monotonic()
        try:
            text = self._fn(role, messages)
        except Exception as e:  # noqa: BLE001  失敗を LLMResult で表す
            return LLMResult(False, "", self._model, {}, messages, _ms(t0), f"{type(e).__name__}:{e}")
        return LLMResult(True, text, self._model, {}, messages, _ms(t0))


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def parse_json_text(text: str) -> Any:
    """JSON のみ、または ```json フェンス付きの出力を読む。失敗は ValueError。"""
    s = text.strip()
    m = re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", s, re.S)
    if m:
        s = m.group(1)
    return json.loads(s)
