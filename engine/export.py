"""判定結果の CSV/JSON 出力（仕様 §10）。"""
from __future__ import annotations

import csv
import json
import os

from .core import Engine

_CYCLE_COLS = [
    "cycle_id", "task_id", "phase", "start_t", "end_t", "close_reason", "state", "hs", "hs_reason",
    "hs_conflict", "ru", "ru_reason", "ru_basis", "D", "D_norm", "S", "S_dist", "C", "T", "T_runs",
    "E", "I", "Q", "text_similarity", "mapped_block", "mapping_kind", "intervened", "support_types",
    "off_task", "idle_gaps", "blocked_pastes", "away_count", "away_s", "away_unknown", "burst_typed_max",
    "interruptions", "interruption_s", "gap_reasons", "contamination_suspected",
    "unknown_reasons", "review", "prompt_event_ids", "rule_version", "config_hash",
]
_DECISION_COLS = [
    "opportunity_id", "prompt_event_id", "cycle_id", "t", "group", "phase_name", "eligible", "cooldown",
    "window_cycle_ids", "window_states", "candidate_cells", "support_type", "timing", "reason",
    "rule_version", "config_hash",
]


def _cell(v):
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return v


def export(eng: Engine, out_dir: str, participant_id: str | None = None) -> dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    pid = participant_id or eng.participant_id or "unknown"
    paths = {
        "json": os.path.join(out_dir, f"{pid}_judgments.json"),
        "cycles_csv": os.path.join(out_dir, f"{pid}_cycles.csv"),
        "decisions_csv": os.path.join(out_dir, f"{pid}_decisions.csv"),
    }
    with open(paths["json"], "w", encoding="utf-8") as f:
        json.dump({
            "participant_id": pid, "group": eng.group, "rule_version": eng.rule_version,
            "config_hash": eng.cfg_hash, "config": eng.cfg,
            "cycles": [c.to_dict() for c in eng.cycles],
            "decisions": [d.to_dict() for d in eng.decisions],
            "anomalies": eng.anomalies,
        }, f, ensure_ascii=False, indent=2)
    with open(paths["cycles_csv"], "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["participant_id"] + _CYCLE_COLS)
        w.writeheader()
        for c in eng.cycles:
            d = c.to_dict()
            row = {"participant_id": pid}
            for k in _CYCLE_COLS:
                row[k] = _cell(d[k] if k in d else c.features.get(k))
            w.writerow(row)
    with open(paths["decisions_csv"], "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["participant_id"] + _DECISION_COLS)
        w.writeheader()
        for dec in eng.decisions:
            d = dec.to_dict()
            w.writerow({"participant_id": pid, **{k: _cell(d[k]) for k in _DECISION_COLS}})
    return paths
