"""判定エンジンの v1.2 対応（仕様 v1.2 §2, §10, §15.2）。"""
from engine import make_config, replay

from builder import PROG_A, F, Log, code_block


def edit(log, offset, delete_len, text, kind="typed", ct=None, page="p1", source=None):
    p = {"offset": offset, "delete_len": delete_len, "insert_text": text, "kind": kind,
         "client_t": ct, "page_id": page}
    if source:
        p["source"] = source
    log.add("CODE_EDIT", p, dt=0.1)
    log.doc = log.doc[:offset] + text + log.doc[offset + delete_len:]


def focus(log, away, ct, page="p1"):
    log.add("FOCUS_CHANGE", {"away": away, "page_id": page, "client_t": ct}, dt=0.1)


def cycle_with(log, mid, feats=None):
    """質問→AIコードを貼る→mid(log)→実行。"""
    p = log.ask("q", feats or F(h2=1))
    r = log.respond(code_block(PROG_A), p)
    log.paste_replace_all(PROG_A, r)
    mid(log)
    log.run("")


def test_version_bumped():
    assert make_config()["rule_version"] == "v1.2-dev.2"


def test_client_gap_makes_C_I_E_unknown_not_passive():
    log = Log()
    log.task("t1")
    cycle_with(log, lambda lg: lg.add("CLIENT_GAP", {"from_seq": 5, "to_seq": 7}))
    log.ask("next", F(h2=1))
    eng = replay(log.events)
    c = eng.cycles[0]
    assert c.features["C"] is None and c.features["I"] is None and c.features["E"] is None
    assert c.ru == "Unknown" and c.state == "Pending"
    assert "client_gap" in c.features["gap_reasons"]
    assert any(r.startswith("E:") for r in c.unknown_reasons) and "I:edit_log_gap" in c.unknown_reasons


def test_without_gap_same_cycle_is_passive():
    log = Log()
    log.task("t1")
    cycle_with(log, lambda lg: None)
    log.ask("next", F(h2=1))
    c = replay(log.events).cycles[0]
    assert c.ru == "Passive" and c.features["contamination_suspected"] is False


def test_burst_typing_after_long_away_sets_flag():
    def mid(lg):
        focus(lg, True, 10_000)
        focus(lg, False, 40_000)                    # 30 秒離脱
        for k in range(4):                          # 戻ってから 60 秒以内に 160 文字
            edit(lg, len(lg.doc), 0, "x" * 40, ct=41_000 + k * 1000)
    log = Log()
    log.task("t1")
    cycle_with(log, mid)
    log.ask("next", F(h2=1))
    f = replay(log.events).cycles[0].features
    assert f["away_count"] == 1 and f["away_s"] == 30.0
    assert f["burst_typed_max"] == 160 and f["contamination_suspected"] is True


def test_short_away_or_late_typing_is_not_burst():
    def mid(lg):
        focus(lg, True, 10_000)
        focus(lg, False, 15_000)                    # 5 秒だけ（閾値 10 秒未満）
        edit(lg, len(lg.doc), 0, "x" * 200, ct=16_000)
        focus(lg, True, 20_000)
        focus(lg, False, 50_000)                    # 30 秒離脱
        edit(lg, len(lg.doc), 0, "y" * 200, ct=50_000 + 61_000)   # 戻って 61 秒後
    log = Log()
    log.task("t1")
    cycle_with(log, mid)
    log.ask("next", F(h2=1))
    f = replay(log.events).cycles[0].features
    assert f["away_count"] == 2 and f["burst_typed_max"] == 0 and not f["contamination_suspected"]


def test_away_across_reload_counts_with_unknown_length():
    def mid(lg):
        focus(lg, True, 10_000, page="p1")
        focus(lg, False, 2_000, page="p2")          # 読み直した別ページで復帰
    log = Log()
    log.task("t1")
    cycle_with(log, mid)
    log.ask("next", F(h2=1))
    f = replay(log.events).cycles[0].features
    assert f["away_count"] == 1 and f["away_unknown"] == 1 and f["away_s"] == 0.0


def test_paste_blocked_flag_only_does_not_change_decision():
    def build(policy):
        log = Log()
        log.task("t1")
        for _ in range(2):
            cycle_with(log, lambda lg: lg.add("PASTE_BLOCKED", {"length": 300, "via": "paste"}))
        log.ask("3", F(h2=1))
        return replay(log.events, make_config({"policy": {"contamination_policy": policy}}))
    a = build("flag_only")
    assert all(c.features["contamination_suspected"] for c in a.cycles[:2])
    assert a.decisions[2].support_type == "self_explain"          # 案A：介入判断に使わない
    b = build("hold")
    assert b.decisions[2].support_type is None and b.decisions[2].reason == "hold_contamination"


def test_editor_internal_paste_inherits_origin():
    def mid(lg):
        # AI 由来の printf 行をエディタ内でコピーして末尾に貼る
        line = '    printf("%d\\n", *p);\n'
        off = lg.doc.index(line)
        lg.add("COPY_IN_EDITOR", {"offset": off, "length": len(line), "cut": False})
        edit(lg, len(lg.doc) - 1, 0, line, kind="paste", source={"type": "editor"})
    log = Log()
    log.task("t1")
    cycle_with(log, mid)
    eng = replay(log.events)
    tail = eng.doc.origins[-30:]
    assert all(o.startswith("ai@") for o in tail), tail[:3]


def test_editor_paste_not_matching_clip_is_other():
    def mid(lg):
        lg.add("COPY_IN_EDITOR", {"offset": 0, "length": 5, "cut": False})
        edit(lg, 0, 0, "zzz", kind="paste", source={"type": "editor"})
    log = Log()
    log.task("t1")
    cycle_with(log, mid)
    assert replay(log.events).doc.origins[0].startswith("other@")


def test_session_resume_records_interruption_and_resyncs():
    def mid(lg):
        lg.add("SESSION_RESUME", {"gap_s": 600, "text": lg.doc, "page_id": "p2"})
        lg.add("SESSION_RESUME", {"gap_s": 100, "text": lg.doc + "// 違う", "page_id": "p3"})
    log = Log()
    log.task("t1")
    cycle_with(log, mid)
    log.ask("next", F(h2=1))
    eng = replay(log.events)
    f = eng.cycles[0].features
    assert f["interruptions"] == 2 and f["interruption_s"] == 700.0
    assert "resume_text_mismatch" in f["gap_reasons"] and eng.doc.text.endswith("// 違う")
    assert eng.cycles[0].ru == "Unknown"      # 無操作・欠落を Passive にしない


def test_phase_end_closes_open_cycle():
    log = Log()
    log.task("t1")
    cycle_with(log, lambda lg: None)
    log.add("PHASE_END", {"phase": "main", "reason": "timeout"})
    eng = replay(log.events)
    assert eng.open is None and eng.cycles[0].close_reason == "phase_end"


def _E_after_repaste(rule):
    """エディタに既に PROG_A → h3=1 の質問 → 同じコードを全選択して貼り直す → 実行。"""
    log = Log()
    log.task("t1", starter=PROG_A)
    p = log.ask("x が入れ替わらないのは swap に値を渡しているからだと思う", F(h3=1))
    r = log.respond(code_block(PROG_A), p)
    log.paste_replace_all(PROG_A, r)        # 中身が同じ貼り直し
    log.run("")
    log.ask("next", F(h2=1))
    return replay(log.events, make_config({"E_edit_rule": rule})).cycles[0].features["E"]


def test_E_ignores_repaste_of_same_code():
    assert _E_after_repaste("content_changed") == 0
    assert _E_after_repaste("any") == 1             # v1.2-dev.1 の規則


def test_E_counts_real_change_after_hypothesis():
    log = Log()
    log.task("t1")
    p = log.ask("仮説", F(h3=1))
    r = log.respond(code_block(PROG_A), p)
    log.paste_replace_all(PROG_A, r)
    log.type_at("return 0;", "/* 変更 */ ", after=False)
    log.run("")
    log.ask("next", F(h2=1))
    assert replay(log.events).cycles[0].features["E"] == 1
