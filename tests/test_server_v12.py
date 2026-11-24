"""仕様 v1.2 の受け入れ条件（§10）のうちサーバに関わるもの。

- 到着順が入れ替わっても client_seq 順に処理して同じ判定。重複は二重に数えない
- 連番の欠落が埋まらないサイクルで C・I・E が Unknown
- 外部からの貼り付けは記録され、AI 回答ペインからの貼り付けは通る（遮断そのものは画面側）
- 開き直す・サーバ再起動のあとも同じフェーズ・同じエディタ内容・同じ判定状態から再開
- フェーズの時間上限で自動的に次へ。テスト中は AI チャットが使えない
"""
import json

import pytest

from engine.core import replay
from server.allocation import make_table
from server.app import App
from server.llm import FuncLLM
from server.session import BadRequest, NotSynced, OtherPage

from server_helpers import (F, PC_INFO, PROG_A, Clock, FakeSandbox, Script, cev, code_block, make_app, start,
                            to_phase)


def events_of(app, s, typ=None):
    return [e for e in app.store.events(s.pid) if typ is None or e.type == typ]


def raw(s, seq, typ, payload, page=None, ct=None):
    return {"client_seq": seq, "type": typ, "payload": payload, "client_t": ct if ct is not None else seq * 100,
            "page_id": page or s.page_id}


def paste_items(s, start_seq):
    m = [x for x in s.state()["chat"] if x["role"] == "assistant"][-1]
    seg = next(x for x in m["segments"] if x["type"] == "code")
    return [raw(s, start_seq, "COPY_FROM_AI", {"response_id": m["response_id"], "block_id": seg["block_id"], "text": seg["code"]}),
            raw(s, start_seq + 1, "CODE_EDIT", {"offset": 0, "delete_len": len(s.engine.doc.text),
                                                "insert_text": seg["code"], "kind": "paste"})]


# ---------------------------------------------------------------- 順序・重複・欠落
def _cycles_with_batches(tmp_path, reorder: bool, dup: bool):
    script = Script()
    app = make_app(tmp_path, script)
    s = start(app, "adaptive")
    for q in ("q1", "q2", "q3"):
        script.features[q] = F(h2=1)
        s.check_synced(s.next_seq - 1)
        s.submit_prompt(q)
        base = s.next_seq
        items = paste_items(s, base) + [raw(s, base + 2, "CODE_EDIT", {"offset": 0, "delete_len": 0,
                                                                       "insert_text": "// x\n", "kind": "typed"})]
        batches = [items[:1], items[1:2], items[2:]]
        if reorder:
            batches = batches[::-1]      # 後のまとまりが先に届く
        for b in batches:
            s.client_events(b, s.page_id)
        if dup:
            s.client_events(items, s.page_id)   # 全部をもう一度（再送）
        s.client_events(items, s.page_id)       # 欠けた分の再送（画面は acked より後を送り直す）
        s.run_code(s.engine.doc.text, "")
    return app, s


def test_reordered_and_duplicated_arrival_gives_same_judgment(tmp_path):
    _, a = _cycles_with_batches(tmp_path / "a", reorder=False, dup=False)
    app_b, b = _cycles_with_batches(tmp_path / "b", reorder=True, dup=True)
    assert [c.state for c in a.engine.cycles] == [c.state for c in b.engine.cycles]
    assert [d.to_dict()["reason"] for d in a.engine.decisions] == [d.to_dict()["reason"] for d in b.engine.decisions]
    assert a.engine.doc.text == b.engine.doc.text
    seqs = [e.payload["client_seq"] for e in events_of(app_b, b) if e.event_id.startswith("c")]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))    # 連番順・重複なし
    assert not events_of(app_b, b, "CLIENT_GAP")


def test_gap_waits_then_records_client_gap_and_unknown(tmp_path):
    clock = Clock()
    script = Script()
    app = make_app(tmp_path, script, clock=clock)
    s = start(app, "adaptive")
    script.features["q1"] = F(h2=1)
    s.submit_prompt("q1")
    base = s.next_seq
    items = paste_items(s, base)
    r = s.client_events([items[0]] + [raw(s, base + 3, "CODE_SNAPSHOT", {"text": "x"})], s.page_id)
    assert r["acked"] == base                       # base+1, base+2 が欠けているので base+3 は保留
    assert not events_of(app, s, "CLIENT_GAP")
    clock.t += 31
    r = s.client_events([raw(s, base + 3, "CODE_SNAPSHOT", {"text": PROG_A})], s.page_id)
    gap = events_of(app, s, "CLIENT_GAP")[-1].payload
    assert (gap["from_seq"], gap["to_seq"]) == (base + 1, base + 2) and r["acked"] == base + 3
    s.run_code(s.engine.doc.text, "")
    script.features["q2"] = F(h2=1)
    s.submit_prompt("q2")
    c = s.engine.cycles[-1]
    assert c.features["C"] is None and c.features["I"] is None and c.features["E"] is None
    assert c.ru == "Unknown" and "client_gap" in c.features["gap_reasons"]
    # 遅れて届いた欠落分は重複として捨てる（判定を後から変えない）
    s.client_events(items, s.page_id)
    assert not [e for e in events_of(app, s) if e.event_id == f"c{base + 1}"]


def test_actions_require_synced_events(tmp_path):
    app = make_app(tmp_path, Script())
    s = start(app, "adaptive")
    with pytest.raises(NotSynced) as ei:
        s.check_synced(s.next_seq)              # まだ届いていない連番まで付けた
    assert ei.value.acked == s.next_seq - 1
    s.check_synced(s.next_seq - 1)
    with pytest.raises(OtherPage):
        s.check_synced(None, "another-page")


# ---------------------------------------------------------------- 貼り付け
def test_paste_blocked_records_length_only_and_flags_cycle(tmp_path):
    script = Script()
    app = make_app(tmp_path, script)
    s = start(app, "adaptive")
    script.features["q1"] = F(h2=1)
    s.submit_prompt("q1")
    cev(s, [{"type": "PASTE_BLOCKED", "payload": {"length": 120, "via": "paste", "text": "秘密のコード"}}])
    pb = events_of(app, s, "PASTE_BLOCKED")[-1].payload
    assert pb["length"] == 120 and "text" not in pb           # 内容は保存しない
    # 画面の遮断をすり抜けた外部貼り付けは external として印を付ける
    cev(s, [{"type": "CODE_EDIT", "payload": {"offset": 0, "delete_len": 0, "insert_text": "int z;",
                                              "kind": "paste", "source": {"type": "ai"}}}])
    e = events_of(app, s, "CODE_EDIT")[-1].payload
    assert e["source"] == {"type": "external"} and e["unblocked_external"] and e["claimed_source"] == "ai"
    script.features["q2"] = F(h2=1)
    s.submit_prompt("q2")
    f = s.engine.cycles[-1].features
    assert f["blocked_pastes"] == 1 and f["contamination_suspected"] is True


def test_editor_internal_paste_is_accepted(tmp_path):
    script = Script()
    app = make_app(tmp_path, script)
    s = start(app, "adaptive")
    s.submit_prompt("q1")
    base = s.next_seq
    s.client_events(paste_items(s, base), s.page_id)
    line = "    return 0;\n"
    off = s.engine.doc.text.index(line)
    cev(s, [{"type": "COPY_IN_EDITOR", "payload": {"offset": off, "length": len(line), "cut": False}},
            {"type": "CODE_EDIT", "payload": {"offset": off, "delete_len": 0, "insert_text": line, "kind": "paste"}}])
    e = events_of(app, s, "CODE_EDIT")[-1].payload
    assert e["source"] == {"type": "editor"} and "unblocked_external" not in e
    assert s.engine.doc.origins[off].startswith("ai@")        # 元の文字の由来を引き継ぐ


# ---------------------------------------------------------------- 再開・再起動
def test_reopen_and_restart_resume_same_phase_code_and_judgment(tmp_path):
    clock = Clock()
    script = Script()
    app = make_app(tmp_path, script, clock=clock)
    s = start(app, "adaptive")
    for q in ("q1", "q2"):
        script.features[q] = F(h2=1)
        s.submit_prompt(q)
        s.client_events(paste_items(s, s.next_seq), s.page_id)
        s.run_code(s.engine.doc.text, "")
    before = s.state()
    decisions = [d.to_dict() for d in s.engine.decisions]
    clock.t += 2000                                            # 30 分を超える中断
    r = s.hello("page2", PC_INFO)["state"]
    res = events_of(app, s, "SESSION_RESUME")[-1].payload
    assert res["over_limit"] is True and res["gap_s"] >= 2000 and res["text"] == before["code"]
    assert r["phase"]["id"] == "main" and r["code"] == before["code"]
    with pytest.raises(OtherPage):
        s.client_events([], "page1")                            # 古いタブからは受け付けない
    app.store.close()
    app2 = App(app.server_cfg, str(tmp_path), llm=FuncLLM(lambda *a: (_ for _ in ()).throw(AssertionError("no llm"))),
               sandbox=FakeSandbox(), admin_token="t", clock=clock)
    s2 = app2.session(s.pid)
    st = s2.state()
    assert st["phase"]["id"] == "main" and st["code"] == before["code"] and st["chat"] == before["chat"]
    assert st["next_seq"] == s.next_seq and s2.page_id == "page2"
    assert [d.to_dict() for d in s2.engine.decisions] == decisions
    assert [d.to_dict() for d in replay(app2.store.events(s.pid), app2.engine_cfg).decisions] == decisions


# ---------------------------------------------------------------- 時間上限・テスト中の AI
def test_phase_timeout_auto_advances_and_test_has_no_ai(tmp_path):
    clock = Clock()
    app = make_app(tmp_path, Script(), clock=clock)
    s = start(app, "adaptive", phase="intro")
    s.submit_prompt("q1")
    s.tick()
    assert s.phase()["id"] == "intro"
    clock.t += 601
    s.tick()
    assert s.phase()["id"] == "pretest"
    ends = events_of(app, s, "PHASE_END")
    assert ends[-1].payload == {"phase": "intro", "reason": "timeout"}
    assert events_of(app, s, "TASK_END")[-1].payload["reason"] == "timeout"
    with pytest.raises(BadRequest):
        s.submit_prompt("テスト中の質問")                         # テスト中は AI チャットが使えない
    with pytest.raises(BadRequest):
        s.run_code("int main(void){return 0;}", "")
    assert "chat" not in s.state() and "instrument" in s.state()
    clock.t += 601
    s.tick()
    assert s.phase()["id"] == "main" and s.task.task_id == "main_swap"


def test_session_expires_after_24h(tmp_path):
    clock = Clock()
    app = make_app(tmp_path, Script(), clock=clock)
    s = start(app, "adaptive")
    clock.t += 86400
    s.tick()
    assert s.status == "expired" and s.state()["status"] == "expired"
    assert events_of(app, s, "PHASE_END")[-1].payload["reason"] == "expired"
    with pytest.raises(BadRequest):
        s.submit_prompt("q")


# ---------------------------------------------------------------- 課題の提出
def test_task_gated_by_passing_submission(tmp_path):
    sb = FakeSandbox()
    app = make_app(tmp_path, Script(), sandbox=sb)
    s = start(app, "adaptive")
    with pytest.raises(BadRequest):
        s.task_next()                                            # 合格前は進めない
    r = s.task_submit("SYNTAX_ERROR")
    assert r["passed"] is False and not s.task_passed
    r = s.task_submit(app.task_by_id["main_swap"].starter_code)
    assert r["passed"] is True and r["state"]["busy"] is False
    sub = r["state"]["last_submit"]
    hidden = [x for x in sub["results"] if not x["public"]]
    assert hidden and all(set(x) == {"public", "pass"} for x in hidden)   # 非公開の入力・出力は見せない
    full = events_of(app, s, "TASK_SUBMIT")[-1].payload
    assert len(full["full"]) == len(app.judge_inputs(app.task_by_id["main_swap"]))
    s.task_next()
    assert s.task.task_id == "main_minmax"
    s.task_submit(s.engine.doc.text)
    s.task_next()
    assert s.phase()["id"] == "post"                             # 最後の課題に合格したら次のフェーズ


# ---------------------------------------------------------------- 同意・環境確認・テスト・停止
def test_consent_decline_withdraws(tmp_path):
    app = make_app(tmp_path, Script())
    p = app.create_participant(None, "adaptive")
    s = app.session(p["participant_id"])
    s.hello("pg", PC_INFO)
    assert s.state()["consent"]["text"].startswith("<!--")
    s.consent(False)
    assert s.status == "withdrawn"
    assert events_of(app, s, "CONSENT")[-1].payload["agree"] is False


def test_envcheck_blocks_mobile_and_warns_narrow(tmp_path):
    app = make_app(tmp_path, Script())
    s = start(app, "adaptive", phase="envcheck")
    r = s.env_check({**PC_INFO, "mobile": True})
    assert not r["ok"] and r["blocking"] == ["mobile"] and s.phase()["id"] == "envcheck"
    r = s.env_check({**PC_INFO, "viewport_w": 800, "junk": "x" * 10})
    assert r["ok"] and r["warnings"] == ["narrow_screen"] and s.phase()["id"] == "intro"
    assert "junk" not in events_of(app, s, "ENV_CHECK")[-1].payload


def test_answers_validated_and_required(tmp_path):
    app = make_app(tmp_path, Script())
    s = start(app, "adaptive", phase="questionnaire")
    with pytest.raises(BadRequest):
        s.save_answers({"q_mental": 9}, final=False)
    with pytest.raises(BadRequest):
        s.save_answers({"nope": 1}, final=False)
    s.save_answers({"q_mental": 4, "q_comment": "楽しかった"}, final=False)
    s.save_answers({"q_mental": 4}, final=False)                 # 同じ答えは二重に記録しない
    assert len(events_of(app, s, "QUESTIONNAIRE_ANSWER")) == 2
    with pytest.raises(BadRequest):
        s.save_answers({}, final=True)                           # 必須が未回答
    s.save_answers({"q_frustration": 1, "q_support_burden": 1, "q_support_need": 4, "q_external_ai": 1}, final=True)
    assert s.status == "done"
    items = s.state()  # 終了後も状態は取れる
    assert items["status"] == "done"


def test_instrument_answer_keys_are_not_sent(tmp_path):
    app = make_app(tmp_path, Script())
    s = start(app, "adaptive", phase="pretest")
    items = s.state()["instrument"]["items"]
    assert items and all("answer" not in it for it in items)


def test_admin_stop(tmp_path):
    app = make_app(tmp_path, Script())
    s = start(app, "adaptive")
    s.stop("緊急")
    assert s.status == "stopped" and events_of(app, s, "PARTICIPANT_STOPPED")[-1].payload["reason"] == "緊急"
    with pytest.raises(BadRequest):
        s.submit_prompt("q")


# ---------------------------------------------------------------- 割付・上限
def test_block_randomization_table_and_issue(tmp_path):
    rows = make_table(3, seed=7)
    assert [r[0] for r in rows] == list(range(1, 13))
    for b in range(1, 4):
        groups = [g for _, blk, g in rows if blk == b]
        assert sorted(groups) == ["adaptive", "adaptive", "uniform", "uniform"]
    assert make_table(3, seed=7) == rows                         # 同じ種から同じ表
    path = tmp_path / "alloc.csv"
    path.write_text("slot,block,group\n" + "".join(f"{s},{b},{g}\n" for s, b, g in rows[:4]), encoding="utf-8")
    app = make_app(tmp_path / "d", Script(), server_overrides={"allocation_file": str(path)})
    issued = [app.create_participant(None, "auto") for _ in range(4)]
    assert [p["group"] for p in issued] == [g for _, _, g in rows[:4]] and [p["slot"] for p in issued] == [1, 2, 3, 4]
    ss = events_of(app, app.session(issued[0]["participant_id"]), "SESSION_START")[0].payload
    assert ss["assignment"] == "block_randomized" and ss["allocation_slot"] == 1 and ss["allocation_file_hash"]
    with pytest.raises(BadRequest):
        app.create_participant(None, "auto")                     # 使い切った


def test_llm_limit_notifies_then_blocks(tmp_path):
    app = make_app(tmp_path, Script(), server_overrides={"llm_limits": {
        "notify": {"max_questions": 2, "max_cost_usd": 99}, "block": {"max_questions": 3, "max_cost_usd": 99},
        "block_message": "上限に達しました（テスト）"}})
    s = start(app, "adaptive")
    s.submit_prompt("q1")
    s.submit_prompt("q2")
    lim = events_of(app, s, "LIMIT_REACHED")
    assert [(e.payload["action"], e.payload["limit"]) for e in lim] == [("notify", "questions")]
    assert any("上限" in a["message"] for a in app.alerts)
    assert s.state()["ai_blocked"] is False                 # 通知だけでは止めない
    s.submit_prompt("q3")
    lim = events_of(app, s, "LIMIT_REACHED")
    assert [(e.payload["action"], e.payload["limit"]) for e in lim] == [("notify", "questions"), ("block", "questions")]
    st = s.state()
    assert st["ai_blocked"] is True and st["ai_blocked_message"] == "上限に達しました（テスト）"
    with pytest.raises(BadRequest):
        s.submit_prompt("q4")
    # 再生しても止まったまま
    app2 = make_app(tmp_path, Script(), server_overrides={"llm_limits": app.server_cfg["llm_limits"]})
    assert app2.session(s.pid).state()["ai_blocked"] is True


def test_default_llm_limits():
    from server.settings import DEFAULT_SERVER_CONFIG as D
    assert D["llm_limits"]["notify"] == {"max_questions": 80, "max_cost_usd": 2.0}
    assert D["llm_limits"]["block"] == {"max_questions": 240, "max_cost_usd": 6.0}


def test_llm_cost_from_usage(tmp_path):
    app = make_app(tmp_path, Script())
    assert app.llm_cost("gpt-6.2-sol", {"prompt_tokens": 1_000_000, "completion_tokens": 100_000}) == pytest.approx(3.0)
    assert app.llm_cost("gpt-6.1-terra-2026-11-01", {"prompt_tokens": 1_000_000}) == pytest.approx(1.0)
    assert app.llm_cost("unknown", {"prompt_tokens": 5}) == 0.0


def test_session_end_makes_backup(tmp_path):
    app = make_app(tmp_path, Script())
    s = start(app, "adaptive", phase="questionnaire")
    s.save_answers({"q_mental": 1, "q_frustration": 1, "q_support_burden": 1, "q_support_need": 1,
                    "q_external_ai": 0}, final=True)
    import os
    backups = os.listdir(tmp_path / "backups")
    assert any(f.endswith(f"end_{s.pid}.sqlite3") for f in backups)
