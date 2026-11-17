"""介入ポリシー（仕様 §5, §6, §7）。"""
from __future__ import annotations

from typing import Optional

from .models import CycleResult

SUPPORT_FOR_CELL = {
    "P-P": "decompose",          # 問題の分解
    "P-A": "clarify_question",   # 質問の具体化（送信前）
    "A-P": "self_explain",       # 自己説明
    "C-P": "predict_verify",     # 動作予測・検証
    "C-A": "apply_variation",    # 自力応用
}
UNIFORM_SUPPORT = "trace_predict"   # 一律群の Trace-and-Predict
PRE_SEND = {"clarify_question"}


def candidate_cell(c: CycleResult, history: list[CycleResult], cfg: dict) -> tuple[Optional[str], str]:
    """このサイクルが候補か。(候補セル or None, 理由)。

    history は同じ判定範囲で c より前に完了したサイクル（C-A の判定に使う）。
    """
    pol = cfg["policy"]
    th = cfg["thresholds"]
    if c.state == "Pending":
        return None, "pending"
    if c.off_task:
        return None, "off_task"
    if c.state in ("P-P", "A-P", "C-P"):
        return c.state, "candidate"
    if c.state == "P-A":
        if not pol["enable_PA"]:
            return None, "PA_disabled"
        f = c.hs_features or {}
        clear_mod = any(b in ("D", "S") for b in c.ru_basis) and not any(
            r.startswith("active_by_rename_only") for r in c.review)
        if f.get("h2") == 0 and clear_mod:
            return "P-A", "conditional_candidate"
        return None, "PA_condition_unmet"
    if c.state == "C-A":
        if not pol["enable_CA"]:
            return None, "CA_disabled"
        # 暫定述語：AI利用後の編集あり（D/S）、検証的な転移なし（E==0 確定）、
        # 現在の判定範囲で自力適用（I==1）が一度も観察されていない
        edited = any(b in ("D", "S") for b in c.ru_basis)
        no_verify = c.features.get("E") == 0
        self_applied_seen = any(h.features.get("I") == 1 for h in history + [c])
        if edited and no_verify and not self_applied_seen:
            return "C-A", "conditional_candidate"
        return None, "CA_condition_unmet"
    return None, "non_intervention_cell"


def choose(group: str, window: list[CycleResult], history_before: dict[str, list[CycleResult]],
           cfg: dict) -> tuple[Optional[str], str, list[Optional[str]]]:
    """適格・非クールダウンの機会で、支援の種類を決める。"""
    if group == "uniform":
        return UNIFORM_SUPPORT, "uniform_policy", []
    cells: list[Optional[str]] = []
    reasons: list[str] = []
    for c in window:
        cell, why = candidate_cell(c, history_before[c.cycle_id], cfg)
        cells.append(cell)
        reasons.append(why)
    if any(r == "pending" for r in reasons):
        return None, "hold_pending", cells
    if any(r == "off_task" for r in reasons):
        return None, "hold_off_task", cells
    if all(cells):
        return SUPPORT_FOR_CELL[cells[-1]], f"two_candidates_latest={cells[-1]}", cells
    return None, "not_two_candidates:" + ",".join(reasons), cells
