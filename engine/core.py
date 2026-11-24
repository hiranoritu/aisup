"""判定エンジン本体。

イベントを1件ずつ ingest() に渡すと、サイクル分割・特徴量計算・状態判定・介入判断を行う。
本番（サーバが受信したイベントを順に渡す）と再生（ログを先頭から渡す）は同じコードを通る（仕様 §10）。
エンジンは LLM・サンドボックス・時計に触れない。入力はイベントだけ。

介入判断は PROMPT_FEATURES（送信された質問の LLM 特徴）を受けた時点で行う。
サーバ側の流れ：
  PROMPT_DRAFT_SUBMIT を保存 → LLM で特徴抽出 → PROMPT_FEATURES を保存して ingest
  → 返った Decision に従い、pre_send なら AI 送信前に支援、post_response なら AI 回答表示後に支援。
"""
from __future__ import annotations

from typing import Iterable, Optional

from .config import config_hash, make_config
from .document import OriginDoc, edit_origin
from .events import Event, ordered
from .hs import classify_hs, parse_prompt_features
from .models import CycleResult, Decision, EditRec, OpenCycle, PromptRec, RunRec
from .policy import PRE_SEND, choose
from .ru import code_hash, compute_ru
from .structure import get_analyzer


class Engine:
    def __init__(self, cfg: Optional[dict] = None, analyzer=None) -> None:
        self.cfg = cfg or make_config()
        self.cfg_hash = config_hash(self.cfg)
        self.rule_version = self.cfg["rule_version"]
        self.analyzer = analyzer or get_analyzer(self.cfg["structure_analyzer"])

        self.participant_id: Optional[str] = None
        self.group: Optional[str] = None
        self.task_id: Optional[str] = None
        self.phase: Optional[str] = None
        self.doc = OriginDoc()

        self.cycles: list[CycleResult] = []
        self.open: Optional[OpenCycle] = None
        self.decisions: list[Decision] = []
        self.anomalies: list[dict] = []

        self._drafts: dict[str, Event] = {}
        self._cycle_seq = 0
        self._opp_seq = 0
        self._support_active = False
        self._cooldown_pending = False
        self._last_intervened_idx = -1   # self.cycles 内の添字（§13-2 案B 用）
        # v1.2：離脱・復帰直後の打鍵・エディタ内コピー
        self._away: Optional[tuple[str, float]] = None      # (page_id, 離脱開始の client_t[ms])
        self._burst: Optional[dict] = None                  # 復帰直後の打鍵の集計中
        self._clip: Optional[tuple[str, list[str]]] = None  # エディタ内でコピーした (文字列, 由来ラベル列)

    # ------------------------------------------------------------ 公開API
    def ingest(self, ev: Event) -> Optional[Decision]:
        if self.participant_id is None:
            self.participant_id = ev.participant_id
        h = getattr(self, "_on_" + ev.type.lower(), None)
        in_support = ev.in_support or self._support_active
        if self.open is not None and ev.type not in ("SESSION_START", "PROMPT_DRAFT_SUBMIT", "PROMPT_FEATURES"):
            self.open.event_ids.append(ev.event_id)
            if not in_support and ev.type in ("CODE_EDIT", "RUN", "COMPILE", "PROMPT_DRAFT_SUBMIT"):
                if self.open.last_activity_t and ev.t - self.open.last_activity_t >= self.cfg["thresholds"]["idle_seconds"]:
                    self.open.idle_gaps += 1   # 暫定クローズ候補として記録のみ（Passive の根拠にしない）
                self.open.last_activity_t = ev.t
        if h is None:
            return None
        return h(ev, in_support)

    @property
    def clipboard_text(self) -> Optional[str]:
        """エディタ内で最後にコピーされた文字列（サーバが貼り付けの由来を決めるのに使う）。"""
        return None if self._clip is None else self._clip[0]

    def finish(self, t: Optional[float] = None) -> None:
        """ログ末尾で開いているサイクルを閉じる（再生時）。"""
        if self.open is not None:
            self._close(t if t is not None else self.open.last_activity_t or self.open.start_t, "log_end")

    # ------------------------------------------------------------ ハンドラ
    def _on_session_start(self, ev: Event, _s: bool):
        self.group = ev.payload.get("group")
        if self.group not in ("adaptive", "uniform"):
            self._anomaly(ev, "invalid_group")
        return None

    def _on_task_start(self, ev: Event, _s: bool):
        if self.open is not None:
            self._close(ev.t, "task_start")
        self.task_id = ev.task_id
        self.phase = ev.payload.get("phase", "main")
        self.doc = OriginDoc(ev.payload.get("starter_code", ""), "starter")
        return None

    def _on_task_end(self, ev: Event, _s: bool):
        if self.open is not None:
            self._close(ev.t, "task_end")
        return None

    def _on_prompt_draft_submit(self, ev: Event, _s: bool):
        self._drafts[ev.event_id] = ev
        return None

    def _on_prompt_features(self, ev: Event, _s: bool) -> Optional[Decision]:
        pid = ev.payload.get("prompt_event_id")
        draft = self._drafts.pop(pid, None)
        if draft is None:
            self._anomaly(ev, "features_without_draft")
            return None
        if ev.payload.get("ok", False):
            feats, err = parse_prompt_features(ev.payload.get("features"))
        else:
            feats, err = None, ev.payload.get("error", "llm_failed")
        th = self.cfg["thresholds"]
        review: list[str] = []
        # 新規質問か確認質問か（§13-1 案A）
        if self.open is None or self.open.task_id != draft.task_id:
            is_new = True
        elif feats is None:
            is_new = False
            review.append("followup_undetermined:features_unavailable")
        elif feats.is_followup == 1:
            is_new = False
        elif feats.followup_confidence < th["followup_confidence_min"]:
            is_new = False
            review.append("followup_low_confidence")
        else:
            is_new = True
        rec = PromptRec(draft.event_id, draft.t, draft.payload.get("text", ""), feats, err, is_new, review)

        if not is_new:
            assert self.open is not None
            self.open.prompts.append(rec)
            self.open.review.extend(review)
            self.open.event_ids.extend([draft.event_id, ev.event_id])
            return None

        if self.open is not None:
            self._close(ev.t, "next_question")
        self._open_cycle(draft, rec)
        assert self.open is not None
        self.open.event_ids.append(ev.event_id)
        return self._decide(draft, ev.t)

    def _on_prompt_sent(self, ev: Event, _s: bool):
        if self.open is not None and ev.payload.get("revised"):
            self.open.revised_prompt = ev.payload.get("text")
        return None

    def _on_ai_response(self, ev: Event, in_support: bool):
        if self.open is None:
            self._anomaly(ev, "response_without_cycle")
            return None
        if not in_support:
            self.open.responses.append((str(ev.payload.get("response_id", ev.event_id)), ev.payload.get("text", "")))
        return None

    def _on_code_edit(self, ev: Event, in_support: bool):
        cid = self.open.cycle_id if self.open else ""
        origin: "str | list[str]" = edit_origin(ev.payload, cid)
        src = ev.payload.get("source") or {}
        ins = ev.payload.get("insert_text", "")
        if ev.payload.get("kind") == "paste" and src.get("type") == "editor":
            # エディタ内の貼り付け：コピー元の文字の由来を引き継ぐ（v1.2）
            origin = list(self._clip[1]) if self._clip is not None and self._clip[0] == ins else f"other@{cid or 'none'}"
        if ev.payload.get("kind") == "typed" and ins:
            self._count_burst(ev, len(ins))
        res = self.doc.apply(int(ev.payload.get("offset", -1)), int(ev.payload.get("delete_len", 0)),
                             ev.payload.get("insert_text", ""), origin)
        if not res.ok:
            self._anomaly(ev, "edit_failed:" + res.reason)
            if self.open:
                self.open.gap = True
                self.open.gap_reasons.append("edit_failed")
            return None
        if self.open is not None:
            if in_support:
                self.open.review.append("edit_during_support")
            else:
                self.open.edits.append(EditRec(ev.t, ev.payload.get("kind", "other"), origin))
        return None

    def _on_code_snapshot(self, ev: Event, _s: bool):
        text = ev.payload.get("text")
        if text is None:
            return None
        if text != self.doc.text:
            self._anomaly(ev, "snapshot_mismatch")
            self.doc.resync(text)
            if self.open is not None:
                self.open.gap = True
                self.open.gap_reasons.append("snapshot_mismatch")
        return None

    # ------------------------------------------------------------ v1.2 のイベント
    def _on_copy_in_editor(self, ev: Event, _s: bool):
        try:
            off, n = int(ev.payload["offset"]), int(ev.payload["length"])
        except (KeyError, TypeError, ValueError):
            return None
        if 0 <= off and off + n <= len(self.doc.chars):
            self._clip = (self.doc.text[off:off + n], self.doc.origins_for(off, off + n))
        return None

    def _on_paste_blocked(self, ev: Event, _s: bool):
        if self.open is not None:
            self.open.blocked_pastes += 1
        return None

    def _on_focus_change(self, ev: Event, _s: bool):
        """離脱（タブ非表示またはフォーカス喪失）と復帰。長さは端末の単調時刻 client_t[ms] で測る。"""
        p = ev.payload
        page, ct = p.get("page_id"), p.get("client_t")
        if not isinstance(ct, (int, float)):
            return None
        if p.get("away"):
            if self._away is None:
                self._away = (page, float(ct))
            return None
        if self._away is None:
            return None
        start_page, start_ct = self._away
        self._away = None
        if self.open is None:
            return None
        self.open.away_count += 1
        if start_page != page:
            self.open.away_unknown += 1
            return None
        dur = max(0.0, (float(ct) - start_ct) / 1000.0)
        self.open.away_s += dur
        if dur >= self.cfg["thresholds"]["contam_min_away_s"]:
            self._burst = {"page": page, "start": float(ct), "chars": 0}
        return None

    def _count_burst(self, ev: Event, n: int) -> None:
        b = self._burst
        ct = ev.payload.get("client_t")
        if b is None or not isinstance(ct, (int, float)) or ev.payload.get("page_id") != b["page"]:
            return
        if float(ct) - b["start"] > self.cfg["thresholds"]["contam_burst_window_s"] * 1000.0:
            self._burst = None
            return
        b["chars"] += n
        if self.open is not None:
            self.open.burst_typed_max = max(self.open.burst_typed_max, b["chars"])

    def _on_client_gap(self, ev: Event, _s: bool):
        """連番の欠落（v1.2 §2）。そのサイクルの由来系特徴（C・I・E）は Unknown にする。"""
        if self.open is not None:
            self.open.gap = True
            self.open.gap_reasons.append("client_gap")
        self._anomaly(ev, "client_gap")
        return None

    def _on_session_resume(self, ev: Event, _s: bool):
        if self.open is not None:
            self.open.interruptions += 1
            try:
                self.open.interruption_s += float(ev.payload.get("gap_s") or 0.0)
            except (TypeError, ValueError):
                pass
        self._away = None
        self._burst = None
        text = ev.payload.get("text")
        if isinstance(text, str) and text != self.doc.text:
            self._anomaly(ev, "resume_text_mismatch")
            self.doc.resync(text)
            if self.open is not None:
                self.open.gap = True
                self.open.gap_reasons.append("resume_text_mismatch")
        return None

    def _on_phase_end(self, ev: Event, _s: bool):
        if self.open is not None:
            self._close(ev.t, "phase_end")
        return None

    def _on_run(self, ev: Event, in_support: bool):
        if self.open is None or in_support:
            return None
        self.open.runs.append(RunRec(ev.t, str(ev.payload.get("stdin", "")), code_hash(self.doc.text),
                                     ev.payload.get("exit_code")))
        return None

    def _on_adopt_unchanged(self, ev: Event, in_support: bool):
        if self.open is not None and not in_support:
            self.open.adopt_unchanged += 1
        return None

    def _on_support_shown(self, ev: Event, _s: bool):
        self._support_active = True
        self._cooldown_pending = True
        if self.open is not None:
            self.open.intervened = True
            self.open.support_types.append(ev.payload.get("support_type", ""))
        return None

    def _on_support_end(self, ev: Event, _s: bool):
        self._support_active = False
        return None

    # SUPPORT_RUN / SUPPORT_ANSWER / SUPPORT_HINT / SUPPORT_INFEASIBLE / COMPILE / COPY_FROM_AI は
    # 状態に影響しない（ログとして保存されるのみ）。

    # ------------------------------------------------------------ サイクル
    def _open_cycle(self, draft: Event, rec: PromptRec) -> None:
        self._cycle_seq += 1
        self.open = OpenCycle(
            cycle_id=f"c{self._cycle_seq}", task_id=draft.task_id, phase=self.phase or "",
            start_t=draft.t, start_doc=self.doc.copy(), prompts=[rec], last_activity_t=draft.t,
            review=list(rec.review),
        )
        self.open.event_ids.append(draft.event_id)

    def _close(self, t: float, reason: str) -> None:
        oc = self.open
        assert oc is not None
        first = oc.prompts[0]
        hs = classify_hs(first.features, first.parse_error)
        ru = compute_ru(oc, self.doc, self.cfg, self.analyzer)
        if hs.label is None or ru.label == "Unknown":
            state = "Pending"
        else:
            state = f"{hs.label[0]}-{ru.label[0]}"
        review = list(oc.review) + list(ru.review)
        if hs.conflict:
            review.append("hs_conflict_h1_with_h3h4")
        off_task = None if first.features is None else bool(first.features.off_task)
        unknown = list(ru.unknown_reasons)
        if hs.label is None:
            unknown.insert(0, "HS:" + hs.reason)
        th = self.cfg["thresholds"]
        aux = {
            "blocked_pastes": oc.blocked_pastes, "away_count": oc.away_count, "away_s": round(oc.away_s, 1),
            "away_unknown": oc.away_unknown, "burst_typed_max": oc.burst_typed_max,
            "interruptions": oc.interruptions, "interruption_s": round(oc.interruption_s, 1),
            "gap_reasons": list(oc.gap_reasons),
        }
        aux["contamination_suspected"] = (oc.blocked_pastes >= th["contam_blocked_pastes_min"]
                                          or oc.burst_typed_max >= th["contam_burst_chars"])
        ru.features.update(aux)
        res = CycleResult(
            cycle_id=oc.cycle_id, task_id=oc.task_id, phase=oc.phase, start_t=oc.start_t, end_t=t,
            close_reason=reason, prompt_event_ids=[p.event_id for p in oc.prompts],
            hs=hs.label, hs_reason=hs.reason, hs_conflict=hs.conflict,
            hs_features=None if first.features is None else {
                k: getattr(first.features, k) for k in ("h1", "h2", "h3", "h4", "off_task")},
            ru=ru.label, ru_reason=ru.reason, ru_basis=ru.basis, features=ru.features,
            unknown_reasons=unknown, state=state, intervened=oc.intervened,
            support_types=oc.support_types, off_task=off_task, idle_gaps=oc.idle_gaps,
            review=review, event_ids=oc.event_ids, rule_version=self.rule_version, config_hash=self.cfg_hash,
        )
        self.cycles.append(res)
        if oc.intervened:
            self._last_intervened_idx = len(self.cycles) - 1
        self.open = None

    # ------------------------------------------------------------ 介入判断
    def _scope_cycles(self) -> list[CycleResult]:
        scope = self.cfg["policy"]["window_scope"]
        key = (lambda c: c.phase == self.phase) if scope == "phase" else (lambda c: c.task_id == self.task_id)
        return [c for c in self.cycles if key(c)]

    def _window(self) -> tuple[list[CycleResult], dict[str, list[CycleResult]]]:
        scoped = self._scope_cycles()
        if self.cfg["policy"]["window_rule"] == "after_last_intervention" and self._last_intervened_idx >= 0:
            last = self.cycles[self._last_intervened_idx]
            if last in scoped:
                scoped = scoped[scoped.index(last) + 1:]
        usable = [c for c in scoped if not c.intervened]
        history = {c.cycle_id: [h for h in scoped if h.end_t <= c.start_t] for c in usable}
        return usable[-2:], history

    def _decide(self, draft: Event, t: float) -> Decision:
        self._opp_seq += 1
        oc = self.open
        assert oc is not None
        group = self.group or "unknown"
        enabled = self.phase in self.cfg["policy"]["support_enabled_phases"]
        window, history = self._window()
        base = dict(
            opportunity_id=f"o{self._opp_seq}", prompt_event_id=draft.event_id, cycle_id=oc.cycle_id, t=t,
            group=group, phase_name=self.phase or "", window_cycle_ids=[c.cycle_id for c in window],
            window_states=[c.state for c in window], rule_version=self.rule_version, config_hash=self.cfg_hash,
        )
        if not enabled:
            d = Decision(eligible=False, cooldown=False, support_type=None, timing=None,
                         reason="support_disabled_phase", candidate_cells=[], **base)
        elif group not in ("adaptive", "uniform"):
            d = Decision(eligible=False, cooldown=False, support_type=None, timing=None,
                         reason="group_unset", candidate_cells=[], **base)
        elif len(window) < 2:
            d = Decision(eligible=False, cooldown=False, support_type=None, timing=None,
                         reason=f"insufficient_cycles({len(window)})", candidate_cells=[], **base)
        elif self._cooldown_pending:
            self._cooldown_pending = False
            d = Decision(eligible=True, cooldown=True, support_type=None, timing=None,
                         reason="cooldown", candidate_cells=[], **base)
        else:
            support, why, cells = choose(group, window, history, self.cfg)
            timing = None if support is None else ("pre_send" if support in PRE_SEND else "post_response")
            d = Decision(eligible=True, cooldown=False, support_type=support, timing=timing,
                         reason=why, candidate_cells=cells, **base)
        self.decisions.append(d)
        return d

    def _anomaly(self, ev: Event, kind: str) -> None:
        self.anomalies.append({"event_id": ev.event_id, "type": ev.type, "t": ev.t, "anomaly": kind})


def replay(events: Iterable[Event], cfg: Optional[dict] = None, analyzer=None) -> Engine:
    eng = Engine(cfg, analyzer)
    last_t = None
    for ev in ordered(events):
        eng.ingest(ev)
        last_t = ev.t
    eng.finish(last_t)
    return eng
