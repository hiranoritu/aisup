"""プロンプトファイルの読み込み。

形式は prompts/prompt_features.md と同じ Markdown：
  `## system` と `## 出力形式…` の節 → system メッセージ（この順に連結）
  `## user…` の節                    → user メッセージのテンプレート
  それ以外の節（運用メモなど）は送らない。
テンプレートの `{name}` は render() に渡したキーだけを置換する（コード中の波括弧は触らない）。
版はファイル内容の SHA-256 先頭12桁。LLM_CALL に記録する。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Prompt:
    path: str
    system: str
    user: str
    version: str

    def messages(self, **vals: str) -> list[dict[str, str]]:
        msgs = []
        if self.system:
            msgs.append({"role": "system", "content": render(self.system, **vals)})
        if self.user:
            msgs.append({"role": "user", "content": render(self.user, **vals)})
        return msgs


def render(template: str, **vals: str) -> str:
    out = template
    for k, v in vals.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def load_prompt(path: str) -> Prompt:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        m = re.match(r"^## (.+)$", line)
        if m:
            sections.append((m.group(1).strip(), []))
        elif sections:
            sections[-1][1].append(line)
    system = [("\n".join(body)).strip() for title, body in sections
              if title.lower().startswith("system") or title.startswith("出力形式")]
    user = [("\n".join(body)).strip() for title, body in sections if title.lower().startswith("user")]
    return Prompt(path, "\n\n".join(s for s in system if s), "\n\n".join(u for u in user if u), version)
