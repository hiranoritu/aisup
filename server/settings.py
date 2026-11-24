"""サーバ設定（判定規則以外）。

判定規則・閾値は engine/config.py。ここは LLM・サンドボックス・支援UI・運用の設定で、
内容のハッシュを SESSION_START に記録する（本実験前に凍結する）。
秘密情報（API キー・管理トークン）は環境変数からのみ読み、ハッシュにも含めない。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from typing import Any, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_SERVER_CONFIG: dict[str, Any] = {
    "server_config_version": "s1",
    # "openai" | "fake"（fake は開発・テスト用の決定的な応答。本実験では使わない）
    "llm_provider": "openai",
    # §13-6：モデルは役割ごとに固定。model が空のまま openai で起動するとエラーにする。
    # params は API にそのまま渡す（例 {"reasoning_effort": "low"}）。temperature を受け付けないモデルがあるので
    # 既定では入れない。response_format は JSON を返させる役割で使う（null にすると送らない）。
    "llm": {
        "chat":     {"model": "", "params": {}, "timeout_s": 60},   # 学習者用AI（両群で固定）
        "features": {"model": "", "params": {}, "timeout_s": 8, "response_format": {"type": "json_object"}},
        "support":  {"model": "", "params": {}, "timeout_s": 12, "response_format": {"type": "json_object"}},
        "grade":    {"model": "", "params": {}, "timeout_s": 12, "response_format": {"type": "json_object"}},
    },
    "openai_base_url": "https://api.openai.com/v1",
    "prompts": {
        "learner_system": "prompts/learner_system.md",
        "features": "prompts/prompt_features.md",
        "support_dir": "prompts/support",
        "grade": "prompts/grade_rubric.md",
    },
    # §6：支援問題の生成と検証
    "support": {
        "generation_timeout_s": 15,     # 1回の生成（LLM＋サンドボックス検証）の上限
        "regenerate_on_failure": 1,     # 失敗時の再生成回数
        "verify_repeats": 3,            # 同一入力での実行回数（出力完全一致）
        "max_duration_s": 300,          # 支援UIの最大所要時間（両群共通）
        "allow_skip": True,             # §13-5 既定：許す
        "block_copy_during_support": True,   # §13-4 既定：支援終了まで AI 回答からのコピー禁止
        "freeform_feedback": "rubric_llm",   # §13-3 案A "rubric_llm" / 案B "self_assess"
    },
    "chat": {
        "history_scope": "task",        # 学習者用AIに渡す会話履歴の範囲（課題ごとにリセット）
        "max_history_messages": 20,
    },
    "sandbox": {
        "image": "aisup-sandbox:1",
        "memory": "256m",
        "cpus": "1",
        "pids": 64,
        "wall_s": 3.0,
        "cpu_s": 2,
        "output_bytes": 65536,
    },
    "tasks_dir": "tasks",
    # 課題の順序（導入共通活動 → 主課題 → 支援停止後の後続課題）
    "task_sequence": ["intro_sum", "main_swap", "main_minmax", "main_count", "post_reverse", "post_divmod"],
    "snapshot_interval_s": 10,
    # 教材（画面上部の教材バーに並ぶ順）。file は Markdown（materials/ 以下）。
    # phases を指定するとそのフェーズの課題中だけ表示する（省略時は常に表示）。
    "materials": [
        {"id": "howto", "title": "この学習環境の使い方", "file": "materials/howto.md"},
        {"id": "pointer_basics", "title": "ポインタの基本", "file": "materials/pointer_basics.md",
         "phases": ["main", "post"]},
        {"id": "strings_pointers", "title": "文字列とポインタ", "file": "materials/strings_pointers.md",
         "phases": ["main", "post"]},
        {"id": "errors", "title": "エラーメッセージの読み方", "file": "materials/errors.md"},
    ],
}


def make_server_config(overrides: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_SERVER_CONFIG)
    if overrides:
        _deep_update(cfg, overrides)
    return cfg


def load_server_config(path: Optional[str]) -> dict[str, Any]:
    if not path:
        return make_server_config()
    with open(path, encoding="utf-8") as f:
        return make_server_config(json.load(f))


def _deep_update(base: dict, upd: dict) -> None:
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v


def server_config_hash(cfg: dict[str, Any]) -> str:
    blob = json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def validate_server_config(cfg: dict[str, Any]) -> list[str]:
    errs = []
    if cfg["llm_provider"] not in ("openai", "fake"):
        errs.append(f"llm_provider must be openai|fake: {cfg['llm_provider']}")
    if cfg["llm_provider"] == "openai":
        for role, c in cfg["llm"].items():
            if not c.get("model"):
                errs.append(f"llm.{role}.model is not set（§13-6 で決めたモデル名を設定する）")
        if not os.environ.get("OPENAI_API_KEY"):
            errs.append("環境変数 OPENAI_API_KEY がない")
    if cfg["support"]["freeform_feedback"] not in ("rubric_llm", "self_assess"):
        errs.append("support.freeform_feedback must be rubric_llm|self_assess")
    return errs


def abspath(rel: str) -> str:
    return rel if os.path.isabs(rel) else os.path.join(ROOT, rel)
