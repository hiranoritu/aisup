"""規則・閾値の設定。

本実験前に凍結し、config_hash() を全判定に記録する（仕様 §1, §14）。
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "rule_version": "v1.1-dev.1",
    "thresholds": {
        # RU（仕様 §4）
        "D_active": 0.10,          # D >= これで Active の根拠
        "S_dist": 0.10,            # 正規化構造差分 >= これ かつ構造変更ありで S=1
        "C_passive": 0.80,         # C >= これ かつ D < D_active で Passive
        "I_handtyped": 0.80,       # 手入力由来トークン比率
        "I_struct_sim_max": 0.70,  # AIコードとの構造類似度がこれ未満
        "T_min": 2,                # Constructive 規則の T 下限
        # 対応付け・由来追跡
        "map_min_overlap": 0.30,           # AIコードのトークンのうち学習者コードと一致する割合の下限
        "max_unknown_origin_ratio": 0.20,  # 由来不明トークンがこれを超えたら C/I は Unknown
        # サイクル（仕様 §2, §13-1 案A）
        "followup_confidence_min": 0.70,
        "idle_seconds": 90,
    },
    "policy": {
        "enable_PA": True,   # 予備実験で不成立なら False（非介入化）
        "enable_CA": True,
        # §13-2: "skip_intervened"（案A：介入サイクルのみ除外）
        #        "after_last_intervention"（案B：最後の介入より後のサイクルのみ）
        "window_rule": "skip_intervened",
        # 判定窓の範囲: "phase"（同じフェーズ内）/ "task"（同じ課題内）
        "window_scope": "phase",
        "support_enabled_phases": ["main"],
    },
    # 本番は "tree_sitter_c"。"lexer_approx" は開発用の近似（仕様外）。
    "structure_analyzer": "lexer_approx",
}


def make_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if overrides:
        _deep_update(cfg, overrides)
    return cfg


def _deep_update(base: dict, upd: dict) -> None:
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v


def config_hash(cfg: dict[str, Any]) -> str:
    blob = json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def load_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return make_config(json.load(f))
