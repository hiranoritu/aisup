"""RU：特徴量計算と決定規則（仕様 §4）。

未知値は None で表し、規則は Kleene の三値論理で評価する。
上位の規則が「真の可能性がある（None）」場合は下位の規則に進まず Unknown とする
（例：E が不明なら、Constructive の可能性を否定できないので Active とは判定しない）。
"""
from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional

from .clex import Token, find_functions, lex, normalize_identifiers
from .codeblocks import CodeBlock, extract_c_blocks
from .document import OriginDoc, origin_cycle, origin_kind
from .models import OpenCycle
from .structure import StructureAnalyzer

Tri = Optional[bool]


# ---------- 三値論理 ----------
def k_and(*xs: Tri) -> Tri:
    if any(x is False for x in xs):
        return False
    if any(x is None for x in xs):
        return None
    return True


def k_or(*xs: Tri) -> Tri:
    if any(x is True for x in xs):
        return True
    if any(x is None for x in xs):
        return None
    return False


def ge(x: Optional[float], th: float) -> Tri:
    return None if x is None else x >= th


def lt(x: Optional[float], th: float) -> Tri:
    return None if x is None else x < th


def eq1(x: Optional[int]) -> Tri:
    return None if x is None else x == 1


def code_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# ---------- AI コードと学習者コードの対応付け ----------
@dataclass
class Mapping:
    block: CodeBlock
    kind: str                 # "function" | "region"
    ai_tokens: list[Token]
    tgt_tokens: list[Token]
    ai_src: str
    tgt_src: str
    tgt_func_names: list[str] = field(default_factory=list)
    overlap: float = 0.0


def _span_src(src: str, toks: list[Token]) -> str:
    return src[toks[0].start:toks[-1].end] if toks else ""


def map_block(block: CodeBlock, file_src: str, file_toks: list[Token], min_overlap: float) -> Optional[Mapping]:
    blex = lex(block.code)
    btoks = blex.tokens
    if not btoks:
        return None
    bfuncs = find_functions(btoks)
    ffuncs = find_functions(file_toks)
    common = [name for name in bfuncs if name in ffuncs]
    if common:
        ai_t: list[Token] = []
        tg_t: list[Token] = []
        ai_srcs, tg_srcs = [], []
        for name in common:
            a0, a1 = bfuncs[name]
            f0, f1 = ffuncs[name]
            ai_t += btoks[a0:a1]
            tg_t += file_toks[f0:f1]
            ai_srcs.append(_span_src(block.code, btoks[a0:a1]))
            tg_srcs.append(_span_src(file_src, file_toks[f0:f1]))
        sm = difflib.SequenceMatcher(None, [t.value for t in ai_t], [t.value for t in tg_t], autojunk=False)
        overlap = sum(b.size for b in sm.get_matching_blocks()) / max(1, len(ai_t))
        return Mapping(block, "function", ai_t, tg_t, "\n".join(ai_srcs), "\n".join(tg_srcs), common, overlap)
    # 関数単位で対応しない断片：トークン一致で学習者コード内の範囲を探す
    sm = difflib.SequenceMatcher(None, [t.value for t in btoks], [t.value for t in file_toks], autojunk=False)
    mbs = [b for b in sm.get_matching_blocks() if b.size > 0]
    matched = sum(b.size for b in mbs)
    overlap = matched / len(btoks)
    if overlap < min_overlap or not mbs:
        return None
    j0 = mbs[0].b
    j1 = mbs[-1].b + mbs[-1].size
    tg_t = file_toks[j0:j1]
    return Mapping(block, "region", btoks, tg_t, block.code, _span_src(file_src, tg_t), [], overlap)


# ---------- 特徴量 ----------
def compute_D(ai_vals: list[str], tg_vals: list[str]) -> float:
    sm = difflib.SequenceMatcher(None, ai_vals, tg_vals, autojunk=False)
    dele = rep = ins = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "delete":
            dele += i2 - i1
        elif tag == "replace":
            rep += i2 - i1
            # 置換で増えた分は挿入として数える
            ins += max(0, (j2 - j1) - (i2 - i1))
        elif tag == "insert":
            ins += j2 - j1
    denom = len(ai_vals) + ins
    return (dele + rep + ins) / denom if denom else 0.0


def token_origins(doc: OriginDoc, toks: list[Token]) -> list[str]:
    """各トークンの由来（文字の多数決）。"""
    out = []
    for t in toks:
        labels = doc.origins_for(t.start, t.end)
        if not labels:
            out.append("unknown")
            continue
        counts: dict[str, int] = {}
        for lb in labels:
            counts[lb] = counts.get(lb, 0) + 1
        out.append(max(sorted(counts), key=lambda k: counts[k]))
    return out


@dataclass
class RUOutput:
    label: str
    reason: str
    basis: list[str]
    features: dict[str, Any]
    unknown_reasons: list[str]
    review: list[str]


def compute_ru(oc: OpenCycle, end_doc: OriginDoc, cfg: dict, analyzer: StructureAnalyzer) -> RUOutput:
    th = cfg["thresholds"]
    unk: list[str] = []
    review: list[str] = []
    feats: dict[str, Any] = {}

    # --- T ---
    feats["T_runs"] = len(oc.runs)
    feats["T"] = len({(r.code_hash, r.stdin) for r in oc.runs})  # 同一コード・同一入力の反復は1回
    feats["T_distinct_inputs"] = len({r.stdin for r in oc.runs})

    # --- Q（確認質問）---
    followups = [p for p in oc.prompts if not p.is_new]
    if not followups:
        Q: Optional[int] = 0
    elif any(p.features is not None and p.features.asks_about_answer == 1 for p in followups):
        Q = 1
    elif any(p.features is None for p in followups):
        Q = None
        unk.append("Q:followup_features_unavailable")
    else:
        Q = 0
    feats["Q"] = Q

    # --- E（観察可能な検証系列）---
    E = _compute_E(oc, unk)
    feats["E"] = E

    # --- コード対応付け ---
    blocks: list[CodeBlock] = []
    for rid, text in oc.responses:
        blocks.extend(extract_c_blocks(rid, text))
    feats["n_code_blocks"] = len(blocks)
    D = D_norm = text_sim = C = None
    S: Optional[int] = None
    I: Optional[int] = None
    struct_dist = None
    mapping: Optional[Mapping] = None
    end_src = end_doc.text
    end_lex = lex(end_src)
    if not blocks:
        unk.append("code:no_code_in_response")
    else:
        cands = [m for b in blocks
                 if (m := map_block(b, end_src, end_lex.tokens, th["map_min_overlap"])) is not None]
        if not cands:
            unk.append("code:unmappable")
        else:
            mapping = max(cands, key=lambda m: (m.overlap, len(m.ai_tokens)))
    if mapping is not None:
        feats["mapped_block"] = f"{mapping.block.response_id}:{mapping.block.block_id}"
        feats["mapping_kind"] = mapping.kind
        feats["mapping_overlap"] = round(mapping.overlap, 4)
        ai_vals = [t.value for t in mapping.ai_tokens]
        tg_vals = [t.value for t in mapping.tgt_tokens]
        D = compute_D(ai_vals, tg_vals)
        D_norm = compute_D(normalize_identifiers(mapping.ai_tokens), normalize_identifiers(mapping.tgt_tokens))
        text_sim = difflib.SequenceMatcher(
            None, normalize_identifiers(mapping.ai_tokens), normalize_identifiers(mapping.tgt_tokens),
            autojunk=False).ratio()
        if D >= th["D_active"] and D_norm < th["D_active"]:
            review.append("rename_only_suspected")

        sres = analyzer.compare(mapping.ai_src, mapping.tgt_src)
        if sres is None:
            unk.append("S:not_comparable")
        else:
            struct_dist = sres.distance
            S = 1 if (sres.distance >= th["S_dist"] and sres.structural_change) else 0
            feats["S_dist"] = round(sres.distance, 4)
            feats["S_structural_change"] = sres.structural_change
            feats["S_analyzer"] = sres.analyzer

        # 由来（C と I）
        origins = token_origins(end_doc, mapping.tgt_tokens)
        n = len(origins)
        kinds = [origin_kind(o) for o in origins]
        n_ai = sum(k == "ai" for k in kinds)
        n_typed = sum(k == "typed" for k in kinds)
        n_unknown = sum(k in ("unknown", "other") for k in kinds)
        feats["tgt_tokens"] = n
        feats["origin_counts"] = {k: kinds.count(k) for k in sorted(set(kinds))}
        origin_ok = True
        if n == 0:
            origin_ok = False
            unk.append("origin:empty_target")
        elif oc.gap:
            origin_ok = False
            unk.append("origin:edit_log_gap")
        elif n_unknown / n > th["max_unknown_origin_ratio"]:
            origin_ok = False
            unk.append("origin:too_many_unknown")
        if origin_ok and not oc.edits:
            unk.append("C:no_edit_events")
            C = None
        elif origin_ok:
            C = n_ai / n
        handtyped = (n_typed / n) if origin_ok else None

        # I：新規作成・手入力80%以上・構造類似度0.70未満・実行1回以上
        if mapping.kind == "function":
            start_funcs = find_functions(lex(oc.start_doc.text).tokens)
            new_creation: Tri = all(name not in start_funcs for name in mapping.tgt_func_names)
        elif origin_ok:
            this_cycle = sum(origin_cycle(o) == oc.cycle_id for o in origins)
            new_creation = this_cycle / n >= 0.8
        else:
            new_creation = None
        i_val = k_and(
            new_creation,
            ge(handtyped, th["I_handtyped"]),
            None if struct_dist is None else (1.0 - struct_dist) < th["I_struct_sim_max"],
            len(oc.runs) >= 1,
        )
        I = None if i_val is None else int(i_val)
        if oc.gap and I is not None:
            I = None   # v1.2 §2：記録の欠落があるサイクルの由来系特徴（C・I・E）は Unknown
            unk.append("I:edit_log_gap")
        elif I is None:
            unk.append("I:undetermined")
        feats["handtyped_ratio"] = None if handtyped is None else round(handtyped, 4)
    else:
        # 対応付けできない場合、I は「実行0回」なら確定で0（記録の欠落があれば Unknown）
        I = 0 if len(oc.runs) == 0 and not oc.gap else None
        if oc.gap:
            unk.append("I:edit_log_gap")

    feats.update({
        "D": None if D is None else round(D, 4),
        "D_norm": None if D_norm is None else round(D_norm, 4),
        "S": S,
        "C": None if C is None else round(C, 4),
        "I": I,
        "text_similarity": None if text_sim is None else round(text_sim, 4),
        "adopt_unchanged": oc.adopt_unchanged,
    })

    # --- 決定規則（上から順） ---
    r1 = k_and(eq1(E), k_or(ge(feats["T"], th["T_min"]), eq1(I)))
    if r1 is True:
        return RUOutput("Constructive", "rule1", ["E"] + (["T"] if feats["T"] >= th["T_min"] else []) + (["I"] if I == 1 else []),
                        feats, unk, review)
    if r1 is None:
        return RUOutput("Unknown", "rule1_undetermined", [], feats, unk, review)

    d_hit = ge(D, th["D_active"])
    s_hit = eq1(S)
    q_hit = eq1(Q)
    r2 = k_or(d_hit, s_hit, q_hit)
    if r2 is True:
        basis = [k for k, v in (("D", d_hit), ("S", s_hit), ("Q", q_hit)) if v is True]
        if basis == ["D"] and "rename_only_suspected" in review:
            review.append("active_by_rename_only")
        return RUOutput("Active", "rule2", basis, feats, unk, review)
    if r2 is None:
        return RUOutput("Unknown", "rule2_undetermined", [], feats, unk, review)

    r3 = k_or(k_and(ge(C, th["C_passive"]), lt(D, th["D_active"])), oc.adopt_unchanged > 0)
    if r3 is True:
        return RUOutput("Passive", "rule3", ["C"] if oc.adopt_unchanged == 0 else ["adopt_unchanged"],
                        feats, unk, review)
    return RUOutput("Unknown", "rule4_insufficient_evidence", [], feats, unk, review)


def _compute_E(oc: OpenCycle, unk: list[str]) -> Optional[int]:
    runs = oc.runs
    edits_t = [e.t for e in oc.edits]
    # ② 異なる入力2種類以上でテスト → 修正 → 以前のテストの再実行
    for k, r in enumerate(runs):
        before = runs[:k]
        if len({b.stdin for b in before}) < 2:
            continue
        # before の最後の実行以降、r より前に編集がある
        last_before_t = max(b.t for b in before)
        if not any(last_before_t < t < r.t for t in edits_t):
            continue
        if r.stdin in {b.stdin for b in before}:
            return 1
    # ① 言語化された仮説（h3=1 のプロンプト）→ 対応する変更 → 実行・確認
    hyp_missing = False
    for p in oc.prompts:
        if p.features is None:
            hyp_missing = True
            continue
        if p.features.h3 != 1:
            continue
        later_edits = [t for t in edits_t if t > p.t]
        if later_edits and any(r.t > min(later_edits) for r in runs):
            return 1
    if oc.gap:
        unk.append("E:edit_log_gap")
        return None
    if hyp_missing:
        unk.append("E:prompt_features_unavailable")
        return None
    return 0
