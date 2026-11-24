"""課題メタデータ（仕様 §14）。tasks/<task_id>.json を読む。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

PHASES = ("intro", "main", "post")


@dataclass(frozen=True)
class Task:
    task_id: str
    phase: str                    # intro（導入共通活動）/ main（支援あり）/ post（支援停止後）
    title: str
    description: str
    concepts: list[str]
    target_functions: list[str]
    starter_code: str
    reference_solution: str
    sample_inputs: list[str] = field(default_factory=list)
    variation_seeds: list[str] = field(default_factory=list)
    placeholder: bool = False

    def public(self) -> dict:
        """学習者に見せる部分（参照解・変形の種は出さない）。"""
        return {"task_id": self.task_id, "phase": self.phase, "title": self.title,
                "description": self.description, "starter_code": self.starter_code}


def load_task(path: str) -> Task:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    t = Task(**d)
    if t.phase not in PHASES:
        raise ValueError(f"{path}: phase must be one of {PHASES}")
    return t


def load_tasks(tasks_dir: str, sequence: list[str]) -> list[Task]:
    out = []
    for tid in sequence:
        t = load_task(os.path.join(tasks_dir, f"{tid}.json"))
        if t.task_id != tid:
            raise ValueError(f"task_id mismatch: {tid} vs {t.task_id}")
        out.append(t)
    return out
