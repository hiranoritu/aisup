"""エンジン内部のデータ構造。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .document import OriginDoc
from .hs import PromptFeatures


@dataclass
class PromptRec:
    event_id: str
    t: float
    text: str
    features: Optional[PromptFeatures]
    parse_error: str
    is_new: bool
    review: list[str] = field(default_factory=list)


@dataclass
class RunRec:
    t: float
    stdin: str
    code_hash: str
    exit_code: Any


@dataclass
class EditRec:
    t: float
    kind: str
    origin: "str | list[str]"
    changed: bool = True     # コードの中身が変わったか（同じ文字列での置き換えは False）


@dataclass
class OpenCycle:
    cycle_id: str
    task_id: str
    phase: str
    start_t: float
    start_doc: OriginDoc
    prompts: list[PromptRec] = field(default_factory=list)
    responses: list[tuple[str, str]] = field(default_factory=list)   # (response_id, text)
    edits: list[EditRec] = field(default_factory=list)
    runs: list[RunRec] = field(default_factory=list)
    adopt_unchanged: int = 0
    gap: bool = False
    gap_reasons: list[str] = field(default_factory=list)
    intervened: bool = False
    support_types: list[str] = field(default_factory=list)
    last_activity_t: float = 0.0
    idle_gaps: int = 0
    review: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    revised_prompt: Optional[str] = None
    # v1.2：外部AI利用の疑いの補助特徴・中断
    blocked_pastes: int = 0
    away_count: int = 0
    away_s: float = 0.0
    away_unknown: int = 0          # 離脱中にページが読み直されて長さが分からない離脱
    burst_typed_max: int = 0       # 離脱から戻った直後の打鍵（挿入文字数）の最大
    interruptions: int = 0
    interruption_s: float = 0.0


@dataclass
class CycleResult:
    cycle_id: str
    task_id: str
    phase: str
    start_t: float
    end_t: float
    close_reason: str
    prompt_event_ids: list[str]
    hs: Optional[str]
    hs_reason: str
    hs_conflict: bool
    hs_features: Optional[dict]
    ru: str                       # Passive|Active|Constructive|Unknown
    ru_reason: str
    ru_basis: list[str]           # 規則を満たした特徴（例: ["D"]）
    features: dict[str, Any]      # D,S,C,T,T_runs,E,I,Q,text_similarity,D_norm,...
    unknown_reasons: list[str]
    state: str                    # "A-P" など、または "Pending"
    intervened: bool
    support_types: list[str]
    off_task: Optional[bool]
    idle_gaps: int
    review: list[str]
    event_ids: list[str]
    rule_version: str
    config_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Decision:
    opportunity_id: str
    prompt_event_id: str
    cycle_id: str
    t: float
    group: str
    phase_name: str               # 実験フェーズ（intro/main/post）
    eligible: bool                # 両群共通の適格性（判定窓が揃っている）
    cooldown: bool
    window_cycle_ids: list[str]
    window_states: list[str]
    support_type: Optional[str]   # None なら非介入
    timing: Optional[str]         # "pre_send" | "post_response"
    reason: str
    candidate_cells: list[Optional[str]]
    rule_version: str
    config_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
