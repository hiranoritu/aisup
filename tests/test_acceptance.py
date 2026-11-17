"""仕様 §10 の受け入れ条件。"""
from engine import Engine, make_config, replay
from engine.events import ordered

from builder import PROG_A, PROG_B, F, Log, code_block


def dec_for(eng, n):
    """n 番目（1始まり）の新規質問に対する判断。"""
    return eng.decisions[n - 1]


# §10-1: A-P が2回続いたら、3回目の質問で自己説明（回答後）
def test_two_AP_cycles_select_self_explain_on_third_question():
    log = Log()
    log.task("t1")
    log.cycle_passive_adopt(F(h2=1), PROG_A)
    log.cycle_passive_adopt(F(h2=1), PROG_B)
    log.ask("3回目", F(h2=1))
    eng = replay(log.events)
    assert [c.state for c in eng.cycles[:2]] == ["A-P", "A-P"]
    d = dec_for(eng, 3)
    assert d.eligible and not d.cooldown
    assert d.support_type == "self_explain"
    assert d.timing == "post_response"
    assert d.window_cycle_ids == ["c1", "c2"]


# §10-2: 完了サイクルが1回だけなら介入しない
def test_single_completed_cycle_no_intervention():
    log = Log()
    log.task("t1")
    log.cycle_passive_adopt(F(h2=1))
    log.ask("2回目", F(h2=1))
    eng = replay(log.events)
    assert dec_for(eng, 1).support_type is None and dec_for(eng, 1).reason == "insufficient_cycles(0)"
    assert dec_for(eng, 2).support_type is None and dec_for(eng, 2).reason == "insufficient_cycles(1)"


# §10-3: RU が Unknown のサイクルを Passive として扱わない
def test_unknown_ru_is_not_passive():
    log = Log()
    log.task("t1")
    for _ in range(2):
        p = log.ask("質問", F(h2=1))
        log.respond("ポインタとはアドレスを保持する変数です。", p)  # コードなし
        log.run("")
    log.ask("3回目", F(h2=1))
    eng = replay(log.events)
    for c in eng.cycles[:2]:
        assert c.ru == "Unknown"
        assert c.state == "Pending"
        assert "code:no_code_in_response" in c.unknown_reasons
    d = dec_for(eng, 3)
    assert d.support_type is None and d.reason == "hold_pending"


def test_no_operation_after_answer_is_not_passive():
    """回答を読んだだけで何もしない（無操作）サイクルは Passive にしない。"""
    log = Log()
    log.task("t1")
    p = log.ask("質問", F(h2=1))
    log.respond(code_block(PROG_A), p)
    log.ask("次", F(h2=1))
    eng = replay(log.events)
    assert eng.cycles[0].ru == "Unknown"


# §10-6: 判定根拠が CSV/JSON に出力できる
def test_export(tmp_path):
    from engine.export import export
    log = Log()
    log.task("t1")
    log.cycle_passive_adopt(F(h2=1))
    log.cycle_passive_adopt(F(h2=1), PROG_B)
    log.ask("3", F(h2=1))
    eng = replay(log.events)
    paths = export(eng, str(tmp_path))
    text = open(paths["cycles_csv"], encoding="utf-8").read()
    assert "A-P" in text and "rule3" in text
    dtext = open(paths["decisions_csv"], encoding="utf-8").read()
    assert "self_explain" in dtext


# §10-7: 同じログ・同じ規則バージョンで同じ判断が再現される（LLM を呼ばない）
def test_replay_is_deterministic_and_matches_incremental():
    log = Log()
    log.task("t1")
    log.cycle_passive_adopt(F(h2=1))
    log.cycle_active_modify(F())
    log.cycle_passive_adopt(F(h3=1), PROG_B)
    log.ask("4", F(h2=1))
    a = replay(log.events)
    b = replay(log.events)
    assert [d.to_dict() for d in a.decisions] == [d.to_dict() for d in b.decisions]
    assert [c.to_dict() for c in a.cycles] == [c.to_dict() for c in b.cycles]
    # 本番（1件ずつ受信して即判断）と同じ判断になる
    live = Engine()
    live_decisions = [d for ev in ordered(log.events) if (d := live.ingest(ev)) is not None]
    assert [d.to_dict() for d in live_decisions] == [d.to_dict() for d in a.decisions]


# v1.1: P-A が2回続いたら、質問の具体化を「送信前」に出す。HS は原文で判定
def test_PA_clarify_question_is_pre_send():
    log = Log()
    log.task("t1")
    log.cycle_active_modify(F())
    log.cycle_active_modify(F(), PROG_A)
    log.ask("3", F())
    eng = replay(log.events)
    assert [c.state for c in eng.cycles[:2]] == ["P-A", "P-A"]
    d = dec_for(eng, 3)
    assert d.support_type == "clarify_question"
    assert d.timing == "pre_send"


def test_PA_disabled_by_config():
    log = Log()
    log.task("t1")
    log.cycle_active_modify(F())
    log.cycle_active_modify(F())
    log.ask("3", F())
    eng = replay(log.events, make_config({"policy": {"enable_PA": False}}))
    assert dec_for(eng, 3).support_type is None


def test_hs_uses_original_prompt_not_revised():
    log = Log()
    log.task("t1")
    p = log.add("PROMPT_DRAFT_SUBMIT", {"text": "教えて"})
    log.add("PROMPT_FEATURES", {"prompt_event_id": p, "ok": True, "features": F()})
    log.add("SUPPORT_SHOWN", {"opportunity_id": "o1", "support_type": "clarify_question"})
    log.add("SUPPORT_END", {"opportunity_id": "o1", "reason": "completed"})
    log.add("PROMPT_SENT", {"prompt_event_id": p, "text": "教えて。scanfでエラーが出る理由を知りたい", "revised": True})
    eng = replay(log.events)
    assert eng.cycles[0].hs == "Passive"


# v1.1: 支援UI内の実行は T/E に含めない
def test_support_runs_excluded_from_T():
    log = Log(group="uniform")
    log.task("t1")
    log.cycle_passive_adopt(F(h2=1))
    log.cycle_passive_adopt(F(h2=1), PROG_B)
    p = log.ask("3", F(h2=1))
    r = log.respond(code_block(PROG_A), p)
    log.support("trace_predict", "o3", runs_inside=3)
    log.paste_replace_all(PROG_A, r)
    log.run("")
    log.ask("4", F(h2=1))
    eng = replay(log.events)
    c3 = eng.cycles[2]
    assert c3.intervened
    assert c3.features["T_runs"] == 1


# v1.1: 支援を出したサイクルは判定窓から除外。直後の適格機会はクールダウン
def test_intervened_cycle_excluded_and_cooldown():
    log = Log()
    log.task("t1")
    log.cycle_passive_adopt(F(h2=1))             # c1 A-P
    log.cycle_passive_adopt(F(h2=1), PROG_B)     # c2 A-P
    p = log.ask("3", F(h2=1))                    # o3 → self_explain
    r = log.respond(code_block(PROG_A), p)
    log.support("self_explain", "o3")
    log.paste_replace_all(PROG_A, r)
    log.run("")                                  # c3 A-P（介入あり）
    log.cycle_passive_adopt(F(h2=1), PROG_B)     # o4 → cooldown, c4 A-P
    log.ask("5", F(h2=1))                        # o5
    eng = replay(log.events)
    assert dec_for(eng, 3).support_type == "self_explain"
    d4 = dec_for(eng, 4)
    assert d4.cooldown and d4.support_type is None
    assert d4.window_cycle_ids == ["c1", "c2"]
    d5 = dec_for(eng, 5)
    assert "c3" not in d5.window_cycle_ids
    assert d5.window_cycle_ids == ["c2", "c4"]
    assert d5.support_type == "self_explain"


def test_window_rule_after_last_intervention():
    log = Log()
    log.task("t1")
    log.cycle_passive_adopt(F(h2=1))
    log.cycle_passive_adopt(F(h2=1), PROG_B)
    p = log.ask("3", F(h2=1))
    r = log.respond(code_block(PROG_A), p)
    log.support("self_explain", "o3")
    log.paste_replace_all(PROG_A, r)
    log.run("")
    log.cycle_passive_adopt(F(h2=1), PROG_B)
    log.ask("5", F(h2=1))
    eng = replay(log.events, make_config({"policy": {"window_rule": "after_last_intervention"}}))
    d5 = dec_for(eng, 5)
    assert d5.window_cycle_ids == ["c4"]
    assert d5.support_type is None and d5.reason.startswith("insufficient_cycles")


def test_infeasible_support_does_not_start_cooldown():
    log = Log()
    log.task("t1")
    log.cycle_passive_adopt(F(h2=1))
    log.cycle_passive_adopt(F(h2=1), PROG_B)
    p = log.ask("3", F(h2=1))
    r = log.respond(code_block(PROG_A), p)
    log.infeasible("o3")
    log.paste_replace_all(PROG_A, r)
    log.run("")
    log.ask("4", F(h2=1))
    eng = replay(log.events)
    d4 = dec_for(eng, 4)
    assert not d4.cooldown
    assert d4.support_type == "self_explain"
    assert d4.window_cycle_ids == ["c2", "c3"]


# 一律群：状態に関係なく Trace-and-Predict。共通の適格性・クールダウンは同じ
def test_uniform_group_ignores_state():
    log = Log(group="uniform")
    log.task("t1")
    for _ in range(2):
        p = log.ask("質問", F(h2=1))
        log.respond("説明のみ", p)              # Pending になるサイクル
    p = log.ask("3", F(h2=1))
    log.respond(code_block(PROG_A), p)
    log.support("trace_predict", "o3")
    log.ask("4", F(h2=1))
    log.ask("5", F(h2=1))
    eng = replay(log.events)
    assert [c.state for c in eng.cycles[:2]] == ["Pending", "Pending"]
    assert dec_for(eng, 3).support_type == "trace_predict"
    assert dec_for(eng, 4).cooldown
    assert dec_for(eng, 5).support_type == "trace_predict"


def test_no_support_in_post_phase():
    log = Log()
    log.task("t2", phase="post")
    log.cycle_passive_adopt(F(h2=1))
    log.cycle_passive_adopt(F(h2=1), PROG_B)
    log.ask("3", F(h2=1))
    eng = replay(log.events)
    assert dec_for(eng, 3).reason == "support_disabled_phase"
    assert eng.cycles[0].state == "A-P"  # 推定は続ける


def test_intro_cycles_not_counted_in_main_window():
    log = Log()
    log.task("intro", phase="intro")
    log.cycle_passive_adopt(F(h2=1))
    log.cycle_passive_adopt(F(h2=1), PROG_B)
    log.task("t1", phase="main")
    log.ask("main 1", F(h2=1))
    eng = replay(log.events)
    assert eng.decisions[-1].reason == "insufficient_cycles(0)"
