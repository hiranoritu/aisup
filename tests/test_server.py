"""サーバ（イベント保存・LLM 中継・介入フロー・支援UI）の検証。サンドボックスは偽物を使う。

仕様 §10 のうちサーバ側に関わる受け入れ条件：
- P-A は AI へ送信される前に提示され、HS は原文で判定される
- 支援を提示したサイクルは以後の判定窓に含まれない／実施不能はクールダウンを始めない
- 支援UI内の実行（SUPPORT_RUN）は RU から除外される
- AI 回答ペインからのコピー→貼り付けが PASTE(source=ai) として由来付きで記録される
- 同じログの再生で同じ判断が再現され、再生では LLM を呼ばない
"""
import json
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest

from engine.core import replay
from server.app import App, make_server
from server.llm import FuncLLM
from server.session import BadRequest
from server.settings import make_server_config

from server_helpers import (F, PC_INFO, PROG_A, Clock, FakeSandbox, Script, cev, close_cycle, code_block, make_app,
                            paste_from_response, start, to_phase, type_at)

SELF_EXPLAIN = json.dumps({"lines": [4, 5], "question": "4〜5行目の役割を説明してください。",
                           "rubric": ["p が a のアドレスを持つ", "*p で a の値を読む"],
                           "model_answer": "p は a のアドレスを持ち、*p で a の値 3 を表示する。", "hint": "& と * に注目"})


def events_of(app, s, typ=None):
    evs = app.store.events(s.pid)
    return [e for e in evs if typ is None or e.type == typ]


def passive_cycle(s, q, feats, script):
    """AI のコードをそのまま貼って1回実行（RU=Passive）。"""
    script.features[q] = feats
    r = s.submit_prompt(q)
    if s.support is not None and s.support["support_type"] != "clarify_question":
        s.support_end(s.support["opportunity_id"], "skipped")
    paste_from_response(s)
    s.run_code(s.engine.doc.text, "")
    return r


def active_cycle(s, q, feats, script):
    """AI のコードを貼り、手で処理を書き足して1回実行（RU=Active, D>=0.10）。"""
    script.features[q] = feats
    r = s.submit_prompt(q)
    paste_from_response(s)
    type_at(s, "int *p = &a;", "\n    *p = *p + 10;\n    if (*p > 5) {\n        printf(\"big\\n\");\n    }")
    s.run_code(s.engine.doc.text, "")
    return r


# ---------------------------------------------------------------- 保存
def test_event_store_is_append_only(tmp_path):
    app = make_app(tmp_path, Script())
    start(app, "adaptive")
    db = sqlite3.connect(tmp_path / "events.sqlite3")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE events SET type='X'")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM events")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE participants SET grp='uniform'")


def test_session_start_records_versions(tmp_path):
    app = make_app(tmp_path, Script())
    s = start(app, "adaptive")
    ss = events_of(app, s, "SESSION_START")[0].payload
    assert ss["group"] == "adaptive" and ss["rule_version"] == app.engine_cfg["rule_version"]
    assert ss["config_hash"] == s.engine.cfg_hash and ss["server_config_hash"] == app.server_cfg_hash
    ts = events_of(app, s, "TASK_START")
    assert [e.task_id for e in ts] == ["intro_sum", "main_swap"]
    assert ts[1].payload["phase"] == "main" and ts[1].payload["starter_code"] == app.task_by_id["main_swap"].starter_code


def test_times_strictly_increase(tmp_path):
    app = make_app(tmp_path, Script(), clock=lambda: 5.0)   # 時計が止まっていても順序が決まる
    s = start(app, "adaptive")
    ts = [e.t for e in events_of(app, s)]
    assert all(a < b for a, b in zip(ts, ts[1:]))


# ---------------------------------------------------------------- 由来
def test_paste_after_copy_from_ai_is_ai_with_origin(tmp_path):
    script = Script()
    app = make_app(tmp_path, script)
    s = start(app, "adaptive")
    s.submit_prompt("質問")
    paste_from_response(s)
    edit = events_of(app, s, "CODE_EDIT")[-1].payload
    resp = events_of(app, s, "AI_RESPONSE")[-1].payload
    assert edit["kind"] == "paste"
    assert edit["source"] == {"type": "ai", "response_id": resp["response_id"], "block_id": "0"}
    assert s.engine.doc.text == PROG_A + "\n"   # フェンス直前の改行まで含めてコピーされる
    assert set(s.engine.doc.origins) == {f"ai@c1@{resp['response_id']}:0"}


def test_paste_not_matching_copy_is_external(tmp_path):
    app = make_app(tmp_path, Script())
    s = start(app, "adaptive")
    s.submit_prompt("質問")
    cev(s, [{"type": "COPY_FROM_AI", "payload": {"response_id": "r1", "block_id": "0", "text": "int a;"}},
                     {"type": "CODE_EDIT", "payload": {"offset": 0, "delete_len": 0, "insert_text": "int b;",
                                                       "kind": "paste",
                                                       "source": {"type": "ai", "response_id": "r1"}}}])
    edit = events_of(app, s, "CODE_EDIT")[-1].payload
    assert edit["source"] == {"type": "external"}   # クライアントの申告は信用しない


def test_paste_matches_with_crlf_difference(tmp_path):
    app = make_app(tmp_path, Script())
    s = start(app, "adaptive")
    s.submit_prompt("質問")
    rid = events_of(app, s, "AI_RESPONSE")[-1].payload["response_id"]
    cev(s, [{"type": "COPY_FROM_AI", "payload": {"response_id": rid, "block_id": None, "text": "a;\r\nb;\r\n"}},
                     {"type": "CODE_EDIT", "payload": {"offset": 0, "delete_len": 0, "insert_text": "a;\nb;\n",
                                                       "kind": "paste"}}])
    assert events_of(app, s, "CODE_EDIT")[-1].payload["source"]["type"] == "ai"


def test_client_cannot_send_server_event_types(tmp_path):
    app = make_app(tmp_path, Script())
    s = start(app, "adaptive")
    with pytest.raises(BadRequest):
        cev(s, [{"type": "SUPPORT_SHOWN", "payload": {}}])
    with pytest.raises(BadRequest):
        cev(s, [{"type": "RUN", "payload": {}}])


# ---------------------------------------------------------------- 特徴抽出
def test_feature_llm_failure_is_unknown_not_passive(tmp_path):
    script = Script()
    script.fail_roles = {"features"}
    app = make_app(tmp_path, script)
    s = start(app, "adaptive")
    s.submit_prompt("質問1")
    pf = events_of(app, s, "PROMPT_FEATURES")[-1].payload
    assert pf["ok"] is False and pf["error"].startswith("llm_failed")
    assert events_of(app, s, "AI_RESPONSE")   # 特徴抽出に失敗しても AI には送る
    close_cycle(s)
    c = s.engine.cycles[-1]
    assert c.hs is None and c.state == "Pending"


def test_llm_calls_are_logged_with_prompt_version(tmp_path):
    app = make_app(tmp_path, Script())
    s = start(app, "adaptive")
    s.submit_prompt("質問")
    calls = events_of(app, s, "LLM_CALL")
    assert [c.payload["purpose"] for c in calls] == ["features", "chat"]
    assert calls[0].payload["prompt_version"] == app.prompts["features"].version
    assert calls[0].payload["messages"] and calls[1].payload["text"] == code_block(PROG_A)
    pf = events_of(app, s, "PROMPT_FEATURES")[-1].payload
    assert pf["llm_call_id"] == calls[0].event_id


# ---------------------------------------------------------------- 介入フロー
def test_PA_pre_send_before_ai(tmp_path):
    script = Script()
    app = make_app(tmp_path, script)
    s = start(app, "adaptive")
    active_cycle(s, "q1", F(), script)       # HS=Passive（原文）, RU=Active → P-A 候補
    active_cycle(s, "q2", F(), script)
    script.features["q3"] = F()
    n_resp = len(events_of(app, s, "AI_RESPONSE"))
    r = s.submit_prompt("q3")
    assert r["stage"] == "pre_send_support"
    sup = r["state"]["support"]
    assert sup["support_type"] == "clarify_question" and sup["public"]["original_text"] == "q3"
    assert len(events_of(app, s, "AI_RESPONSE")) == n_resp           # まだ AI に送っていない
    assert events_of(app, s, "PROMPT_SENT")[-1].payload["prompt_event_id"] != events_of(app, s, "PROMPT_DRAFT_SUBMIT")[-1].event_id
    r = s.clarify_submit(sup["opportunity_id"], 0, "swap の引数の書き方が知りたい", "appended")
    assert r["stage"] == "done"
    sent = events_of(app, s, "PROMPT_SENT")[-1].payload
    assert sent["revised"] and sent["text"] == "q3\nswap の引数の書き方が知りたい" and sent["original_text"] == "q3"
    types = [e.type for e in events_of(app, s)]
    i_shown, i_end, i_sent, i_resp = (types.index("SUPPORT_SHOWN"), types.index("SUPPORT_END"),
                                      len(types) - 1 - types[::-1].index("PROMPT_SENT"),
                                      len(types) - 1 - types[::-1].index("AI_RESPONSE"))
    assert i_shown < i_end < i_sent < i_resp
    # HS は原文（PROMPT_DRAFT_SUBMIT の文面）で判定：q3 の特徴は h2=0 → Passive
    close_cycle(s)
    assert s.engine.cycles[-1].hs == "Passive" and s.engine.cycles[-1].intervened


def test_AP_self_explain_post_response_and_grading(tmp_path):
    script = Script()
    script.support = lambda sysmsg, msgs: SELF_EXPLAIN
    script.grade = lambda u: json.dumps({"criteria": [{"index": 0, "met": 1, "evidence": "アドレス"},
                                                      {"index": 1, "met": 1, "evidence": "値"}], "feedback": ""})
    app = make_app(tmp_path, script)
    s = start(app, "adaptive")
    passive_cycle(s, "q1", F(h2=1), script)
    passive_cycle(s, "q2", F(h2=1), script)
    script.features["q3"] = F(h2=1)
    r = s.submit_prompt("q3")
    assert r["stage"] == "post_response_support"
    sup = r["state"]["support"]
    assert sup["support_type"] == "self_explain" and sup["timing"] == "post_response"
    assert sup["public"]["highlight_lines"] == [4, 5]
    assert "rubric" not in json.dumps(sup["public"]) and "model_answer" not in json.dumps(sup)   # 正答は送らない
    types = [e.type for e in events_of(app, s)]
    assert types.index("SUPPORT_SHOWN") > len(types) - 1 - types[::-1].index("AI_RESPONSE")   # 回答の後
    # 支援中はコピー禁止（試みは記録）
    cev(s, [{"type": "COPY_FROM_AI", "payload": {"response_id": "x", "block_id": "0", "text": "int"}}])
    assert events_of(app, s, "COPY_FROM_AI")[-1].payload["blocked"] is True
    # 実行も不可
    with pytest.raises(BadRequest):
        s.run_code("int main(void){}", "")
    res = s.support_answer(sup["opportunity_id"], "p は a のアドレスを持っていて *p で値を読む")
    assert res["done"] and res["result"]["correct"] is True and res["result"]["unmet"] == []
    s.support_end(sup["opportunity_id"], "completed")
    assert s.support is None
    end = events_of(app, s, "SUPPORT_END")[-1]
    assert end.payload["reason"] == "completed"
    # 次の適格機会はクールダウン
    passive_cycle(s, "q4", F(h2=1), script)
    assert s.engine.decisions[-1].reason == "cooldown"
    # 介入したサイクルは窓から外れる
    script.features["q5"] = F(h2=1)
    s.submit_prompt("q5")
    assert "c3" not in s.engine.decisions[-1].window_cycle_ids


def test_wrong_answer_hint_retry_then_done(tmp_path):
    script = Script()
    script.support = lambda sysmsg, msgs: SELF_EXPLAIN
    script.grade = lambda u: json.dumps({"criteria": [{"index": 0, "met": 0, "evidence": ""},
                                                      {"index": 1, "met": 1, "evidence": "値"}], "feedback": "アドレスは？"})
    app = make_app(tmp_path, script)
    s = start(app, "adaptive")
    passive_cycle(s, "q1", F(h2=1), script)
    passive_cycle(s, "q2", F(h2=1), script)
    script.features["q3"] = F(h2=1)
    opp = s.submit_prompt("q3")["state"]["support"]["opportunity_id"]
    with pytest.raises(BadRequest):
        s.support_hint(opp)                    # 1回目の回答前はヒントなし
    r1 = s.support_answer(opp, "値を読む")
    assert not r1["done"] and r1["result"]["unmet"] == ["p が a のアドレスを持つ"]
    with pytest.raises(BadRequest):
        s.support_end(opp, "completed")        # 終わっていない
    assert s.support_hint(opp)["hint"] == "& と * に注目"
    with pytest.raises(BadRequest):
        s.support_hint(opp)                    # ヒントは1回
    r2 = s.support_answer(opp, "値を読む2")
    assert r2["done"] and r2["result"]["correct"] is False
    with pytest.raises(BadRequest):
        s.support_answer(opp, "3回目")
    s.support_end(opp, "completed")


def test_generation_failure_is_infeasible_without_cooldown(tmp_path):
    script = Script()
    script.support = lambda sysmsg, msgs: "これはJSONではない"
    app = make_app(tmp_path, script)
    s = start(app, "adaptive")
    passive_cycle(s, "q1", F(h2=1), script)
    passive_cycle(s, "q2", F(h2=1), script)
    script.features["q3"] = F(h2=1)
    r = s.submit_prompt("q3")
    assert r["stage"] == "done" and r["state"]["support"] is None
    inf = events_of(app, s, "SUPPORT_INFEASIBLE")[-1].payload
    assert inf["reason"] == "json_parse_error" and len(inf["attempts"]) == 2   # 1回だけ再生成
    assert sum(1 for c in script.calls if c == "support") == 2
    assert not events_of(app, s, "SUPPORT_SHOWN")
    paste_from_response(s)
    s.run_code(s.engine.doc.text, "")
    script.features["q4"] = F(h2=1)
    s.submit_prompt("q4")
    d = s.engine.decisions[-1]
    assert d.reason != "cooldown" and d.support_type == "self_explain"   # 実施不能はクールダウンを始めない
    close_cycle(s)
    assert not s.engine.cycles[-2].intervened


@pytest.mark.parametrize("group", ["adaptive", "uniform"])
def test_no_code_in_response_is_infeasible_in_both_groups(tmp_path, group):
    script = Script()
    app = make_app(tmp_path, script)
    s = start(app, group)
    passive_cycle(s, "q1", F(h2=1), script)
    passive_cycle(s, "q2", F(h2=1), script)
    script.chat = lambda q: "ポインタとはアドレスを保持する変数です。"
    script.features["q3"] = F(h2=1)
    s.submit_prompt("q3")
    inf = events_of(app, s, "SUPPORT_INFEASIBLE")[-1].payload
    assert inf["reason"] == "no_code_in_response"
    assert inf["support_type"] == ("self_explain" if group == "adaptive" else "trace_predict")


def test_uniform_trace_predict_answer_from_execution(tmp_path):
    script = Script()
    script.support = lambda sysmsg, msgs: json.dumps({
        "program": PROG_A, "stdin": "", "question": "何が出力されますか", "expected_output": "999",
        "distractors": ["0", "3", "&a", "999"], "hint": "p は何を指す？"})
    sb = FakeSandbox({"": "3\n"})
    app = make_app(tmp_path, script, sandbox=sb)
    s = start(app, "uniform")
    passive_cycle(s, "q1", F(h2=1), script)
    passive_cycle(s, "q2", F(h2=1), script)
    script.features["q3"] = F(h3=1)
    r = s.submit_prompt("q3")
    sup = r["state"]["support"]
    assert sup["support_type"] == "trace_predict"
    choices = sup["public"]["choices"]
    assert "3" in choices and choices.count("3") == 1          # 実行結果と同じ誤答は除く
    shown = events_of(app, s, "SUPPORT_SHOWN")[-1].payload
    assert shown["key"]["actual_output"] == "3\n"
    assert shown["generation_attempts"][0]["verification"]["llm_expected_matches_actual"] is False
    assert sb.calls[-1] == (PROG_A, ["", "", ""])               # 同一入力で3回実行
    res = s.support_answer(sup["opportunity_id"], choices.index("3"))
    assert res["result"]["correct"] and res["result"]["actual_output"] == "3\n"


def test_support_run_is_in_support_and_excluded_from_T(tmp_path):
    script = Script()
    script.features = {}
    # C-P 候補を作る：h3=1（Constructive）で貼り付けのみ → C-P
    script.support = lambda sysmsg, msgs: json.dumps({
        "task": "rotate を作る", "starter_code": PROG_A, "reference_solution": PROG_A, "tests": ["1 2 3\n"],
        "hint": "h"}) if "条件変更" in sysmsg else json.dumps({
        "program": PROG_A, "stdin": "", "question": "出力は？", "distractors": ["1", "2"], "hint": "h"})
    app = make_app(tmp_path, script, sandbox=FakeSandbox({"": "3\n"}))
    s = start(app, "adaptive")
    passive_cycle(s, "q1", F(h3=1), script)
    passive_cycle(s, "q2", F(h3=1), script)
    script.features["q3"] = F(h3=1)
    sup = s.submit_prompt("q3")["state"]["support"]
    assert sup["support_type"] == "predict_verify"
    with pytest.raises(BadRequest):
        s.support_run(sup["opportunity_id"], PROG_A, "")      # 選択式では実行できない
    s.support_end(sup["opportunity_id"], "skipped")
    paste_from_response(s)
    s.run_code(s.engine.doc.text, "")
    close_cycle(s)
    c3 = s.engine.cycles[-1]
    assert c3.intervened and c3.features["T_runs"] == 1


def test_apply_variation_support_run_and_grading(tmp_path):
    script = Script()
    script.support = lambda sysmsg, msgs: json.dumps({
        "task": "合計も表示する", "starter_code": PROG_A, "reference_solution": PROG_A + "\n", "tests": ["1\n", "2\n"],
        "hint": "h"})
    sb = FakeSandbox({"1\n": "A\n", "2\n": "B\n"})
    app = make_app(tmp_path, script, sandbox=sb)
    s = start(app, "adaptive")
    # C-A 候補：HS=Constructive（h4）, RU=Active（D）, E=0（h3 の仮説なし・入力1種類）
    script.features.clear()
    for q in ("a1", "a2"):
        script.features[q] = F(h4=1)
        s.submit_prompt(q)
        paste_from_response(s)
        type_at(s, "int *p = &a;", "\n    *p = *p + 10;\n    if (*p > 5) {\n        printf(\"big\\n\");\n    }")
    script.features["a3"] = F(h4=1)
    r = s.submit_prompt("a3")
    sup = r["state"]["support"]
    assert sup and sup["support_type"] == "apply_variation", s.engine.decisions[-1]
    assert sup["public"]["tests"] == ["1\n", "2\n"]
    run = s.support_run(sup["opportunity_id"], "int main(void){return 0;}", "1\n")
    assert run["ok"]
    sr = events_of(app, s, "SUPPORT_RUN")[-1]
    assert sr.in_support is True
    res = s.support_answer(sup["opportunity_id"], "int main(void){return 0;}")
    assert res["result"]["correct"] is True        # 偽サンドボックスは入力ごとに同じ出力を返す


def test_support_timeout_closes(tmp_path):
    script = Script()
    script.support = lambda sysmsg, msgs: SELF_EXPLAIN
    clock = Clock()
    app = make_app(tmp_path, script, clock=clock)
    s = start(app, "adaptive")
    passive_cycle(s, "q1", F(h2=1), script)
    passive_cycle(s, "q2", F(h2=1), script)
    script.features["q3"] = F(h2=1)
    opp = s.submit_prompt("q3")["state"]["support"]["opportunity_id"]
    clock.t += app.server_cfg["support"]["max_duration_s"] + 1
    with pytest.raises(BadRequest):
        s.support_answer(opp, "説明")
    assert events_of(app, s, "SUPPORT_END")[-1].payload["reason"] == "timeout" and s.support is None


def test_skip_respects_setting(tmp_path):
    script = Script()
    script.support = lambda sysmsg, msgs: SELF_EXPLAIN
    app = make_app(tmp_path, script, server_overrides={"support": {"allow_skip": False}})
    s = start(app, "adaptive")
    passive_cycle(s, "q1", F(h2=1), script)
    passive_cycle(s, "q2", F(h2=1), script)
    script.features["q3"] = F(h2=1)
    opp = s.submit_prompt("q3")["state"]["support"]["opportunity_id"]
    with pytest.raises(BadRequest):
        s.support_end(opp, "skipped")


def test_force_next_phase_aborts_support_and_session_completes(tmp_path):
    script = Script()
    script.support = lambda sysmsg, msgs: SELF_EXPLAIN
    app = make_app(tmp_path, script)
    s = start(app, "adaptive")
    passive_cycle(s, "q1", F(h2=1), script)
    passive_cycle(s, "q2", F(h2=1), script)
    script.features["q3"] = F(h2=1)
    s.submit_prompt("q3")
    assert s.force_next_phase() == "post"
    end = events_of(app, s, "SUPPORT_END")[-1].payload
    assert end["reason"] == "aborted" and end["by"] == "phase_admin"
    assert events_of(app, s, "TASK_END")[-1].payload["reason"] == "admin"
    assert s.task.task_id == "post_reverse"
    to_phase(s, "questionnaire")
    s.save_answers({"q_mental": 3, "q_frustration": 2, "q_support_burden": 1, "q_support_need": 4,
                    "q_external_ai": 0}, final=True)
    st = s.state()
    assert st["status"] == "done" and st["phase"]["id"] == "questionnaire" and st["completion_message"]
    assert events_of(app, s, "SESSION_END")[-1].payload["reason"] == "completed"


def test_post_phase_has_no_support(tmp_path):
    script = Script()
    script.support = lambda sysmsg, msgs: SELF_EXPLAIN
    app = make_app(tmp_path, script)
    s = start(app, "adaptive", phase="post")
    for q in ("p1", "p2", "p3"):
        passive_cycle(s, q, F(h2=1), script)
    assert all(d.reason == "support_disabled_phase" for d in s.engine.decisions)
    assert not events_of(app, s, "SUPPORT_SHOWN")


# ---------------------------------------------------------------- 再起動・再生
def test_restart_restores_state_and_replay_matches_without_llm(tmp_path):
    script = Script()
    script.support = lambda sysmsg, msgs: SELF_EXPLAIN
    app = make_app(tmp_path, script)
    s = start(app, "adaptive")
    passive_cycle(s, "q1", F(h2=1), script)
    passive_cycle(s, "q2", F(h2=1), script)
    script.features["q3"] = F(h2=1)
    s.submit_prompt("q3")            # 支援表示中のまま再起動
    before = s.state()
    decisions = [d.to_dict() for d in s.engine.decisions]
    app.store.close()

    def no_llm(role, messages):
        raise AssertionError("LLM must not be called on restore/replay")
    cfg = app.server_cfg
    app2 = App(cfg, str(tmp_path), llm=FuncLLM(no_llm), sandbox=FakeSandbox(), admin_token="t")
    s2 = app2.session(s.pid)
    after = s2.state()
    for k in ("task", "code", "chat", "version"):
        assert after[k] == before[k], k
    assert after["support"]["opportunity_id"] == before["support"]["opportunity_id"]
    assert [d.to_dict() for d in s2.engine.decisions] == decisions
    # 保存した DECISION（送信時点の判断）とログ再生の判断が一致
    logged = [e.payload for e in app2.store.events(s.pid) if e.type == "DECISION"]
    rep = replay(app2.store.events(s.pid), app2.engine_cfg)
    assert [d.to_dict() for d in rep.decisions] == logged
    # 復元後もそのまま続けられる
    s2.support_end(after["support"]["opportunity_id"], "skipped")
    assert s2.state()["support"] is None


def test_export_zip_contains_logs_and_judgments(tmp_path):
    import io
    import zipfile
    script = Script()
    app = make_app(tmp_path, script)
    s = start(app, "uniform")
    passive_cycle(s, "q1", F(h2=1), script)
    z = zipfile.ZipFile(io.BytesIO(app.export_zip()))
    names = z.namelist()
    assert f"events/{s.pid}_events.jsonl" in names
    assert f"judgments/{s.pid}_cycles.csv" in names and f"judgments/{s.pid}_decisions.csv" in names
    man = json.loads(z.read("manifest.json"))
    assert "token" not in json.dumps(man["participants"])
    assert man["prompts"]["features"]["version"] == app.prompts["features"].version


# ---------------------------------------------------------------- HTTP
@pytest.fixture
def http_app(tmp_path):
    script = Script()
    app = make_app(tmp_path, script)
    srv = make_server(app, "127.0.0.1", 0)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield app, f"http://127.0.0.1:{srv.server_address[1]}", script
    srv.shutdown()
    srv.server_close()


def _req(url, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_http_auth_and_flow(http_app):
    app, base, script = http_app
    assert _req(base + "/api/state")[0] == 403
    assert _req(base + "/api/admin/overview")[0] == 403
    assert _req(base + "/api/admin/overview", headers={"X-Admin-Token": "wrong"})[0] == 403
    st, body = _req(base + "/api/admin/participants", {"group": "adaptive"}, {"X-Admin-Token": "admintoken"})
    assert st == 200
    p = json.loads(body)
    assert p["group"] == "adaptive" and p["assignment"] == "manual"
    tok = {"X-Token": p["token"]}
    st, body = _req(base + "/api/state", headers=tok)
    assert st == 200 and json.loads(body)["status"] == "not_started"
    st, body = _req(base + "/api/hello", {"page_id": "pg", "info": PC_INFO}, tok)
    assert json.loads(body)["state"]["phase"]["id"] == "consent"
    _req(base + "/api/consent", {"agree": True}, tok)
    st, body = _req(base + "/api/envcheck", {"info": PC_INFO}, tok)
    assert json.loads(body)["state"]["phase"]["id"] == "intro"
    st, body = _req(base + "/api/prompt", {"text": "質問です", "after_seq": 0, "page_id": "pg"}, tok)
    assert st == 200
    state = json.loads(body)["state"]
    assert state["busy"] is False and state["chat"][-1]["segments"][1]["type"] == "code"
    assert "OPENAI" not in body.decode() and "key" not in state
    st, body = _req(base + "/api/events", {"page_id": "pg", "events": [
        {"client_seq": 1, "type": "CODE_EDIT", "payload": {"offset": 99999, "delete_len": 0, "insert_text": "x", "kind": "typed"}}]}, tok)
    assert st == 200 and json.loads(body)["acked"] == 1   # 範囲外の編集は異常として記録されるが画面は止めない
    st, body = _req(base + "/api/run", {"code": "int main(void){return 0;}", "stdin": "", "after_seq": 2, "page_id": "pg"}, tok)
    assert st == 409 and json.loads(body) == {"error": "not_synced", "acked": 1}
    st, body = _req(base + "/api/run", {"code": "int main(void){return 0;}", "stdin": "", "after_seq": 1, "page_id": "pg"}, tok)
    assert st == 200
    st, body = _req(base + "/api/events", {"page_id": "other", "events": []}, tok)
    assert st == 409 and json.loads(body)["error"] == "other_page"
    assert app.session(p["participant_id"]).engine.anomalies
    assert _req(base + "/api/events", {"events": "x"}, tok)[0] == 400
    st, body = _req(base + "/api/admin/export", headers={"X-Admin-Token": "admintoken"})
    assert st == 200 and body[:2] == b"PK"


def test_http_static_and_traversal(http_app):
    _, base, _ = http_app
    st, body = _req(base + "/")
    assert st == 200 and b"<textarea id=\"code\"" in body
    assert _req(base + "/admin")[0] == 200
    assert _req(base + "/app.js")[0] == 200
    assert _req(base + "/../server/app.py")[0] == 404
    assert _req(base + "/%2e%2e/server/app.py")[0] == 404


# ---------------------------------------------------------------- 教材
def test_material_view_is_logged_with_version(tmp_path):
    app = make_app(tmp_path, Script())
    s = start(app, "adaptive")
    ids = [m["id"] for m in s.state()["materials"]]
    assert ids == ["howto", "pointer_basics", "strings_pointers", "errors"]
    cev(s, [{"type": "MATERIAL_VIEW", "payload": {"material_id": "pointer_basics", "action": "open"}},
                     {"type": "MATERIAL_VIEW", "payload": {"material_id": "pointer_basics", "action": "close"}}])
    ev = events_of(app, s, "MATERIAL_VIEW")
    assert [e.payload["action"] for e in ev] == ["open", "close"]
    assert ev[0].payload["material_version"] == app.material_by_id["pointer_basics"]["version"]
    # 不正な内容は拒否せず記録だけして先へ進む（拒否すると端末の送信待ちが詰まるため）
    r = cev(s, [{"type": "MATERIAL_VIEW", "payload": {"material_id": "nope", "action": "open"}}])
    assert r["acked"] == s.next_seq - 1
    assert events_of(app, s, "CLIENT_INFO")[-1].payload["reason"] == "bad_payload"
    # 教材の閲覧はサイクルの判定に影響しない
    assert s.engine.anomalies == []


def test_materials_filtered_by_phase(tmp_path):
    app = make_app(tmp_path, Script(), server_overrides={"materials": [
        {"id": "howto", "title": "使い方", "file": "materials/howto.md"},
        {"id": "pb", "title": "ポインタ", "file": "materials/pointer_basics.md", "phases": ["main"]}]})
    s = start(app, "adaptive", phase="intro")
    assert [m["id"] for m in s.state()["materials"]] == ["howto"]
    to_phase(s, "main")
    assert [m["id"] for m in s.state()["materials"]] == ["howto", "pb"]


def test_http_material_endpoint(http_app):
    app, base, _ = http_app
    p = app.create_participant(None, "adaptive")
    assert _req(base + "/api/material?id=howto")[0] == 403
    st, body = _req(base + "/api/material?id=pointer_basics", headers={"X-Token": p["token"]})
    m = json.loads(body)
    assert st == 200 and m["title"] == "ポインタの基本" and "```c" in m["content"]
    assert _req(base + "/api/material?id=x", headers={"X-Token": p["token"]})[0] == 400
    assert _req(base + "/materials/howto.md")[0] == 200
    assert _req(base + "/materials/../server/app.py")[0] == 404
    assert _req(base + "/highlight.js")[0] == 200 and _req(base + "/materials.js")[0] == 200


# ---------------------------------------------------------------- OpenAI クライアント（偽の API サーバで検証）
@pytest.fixture
def fake_openai():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
            if body["model"] == "bad-model":
                out, code = {"error": {"message": "model not found"}}, 404
            else:
                out, code = {"model": body["model"] + "-2026-11-17", "usage": {"total_tokens": 3},
                             "choices": [{"message": {"content": '{"ok": 1}'}}]}, 200
            data = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1", seen
    srv.shutdown()
    srv.server_close()


def test_openai_client_request_and_response(fake_openai):
    from server.llm import OpenAIClient
    base, seen = fake_openai
    roles = {"features": {"model": "gpt-6.1-terra", "params": {"reasoning_effort": "none"}, "timeout_s": 5,
                          "response_format": {"type": "json_object"}},
             "chat": {"model": "gpt-6.2-sol", "params": {"reasoning_effort": "low"}, "timeout_s": 5},
             "grade": {"model": "bad-model", "params": {}, "timeout_s": 5}}
    c = OpenAIClient("sk-test", roles, base)
    r = c.complete("features", [{"role": "user", "content": "q"}], json_mode=True)
    assert r.ok and r.text == '{"ok": 1}' and r.model == "gpt-6.1-terra-2026-11-17"   # 実際に応答したモデル名を記録
    req = seen[-1]
    assert req["path"] == "/v1/chat/completions" and req["auth"] == "Bearer sk-test"
    assert req["body"]["reasoning_effort"] == "none" and req["body"]["response_format"] == {"type": "json_object"}
    assert "temperature" not in req["body"]
    c.complete("chat", [{"role": "user", "content": "q"}])
    assert "response_format" not in seen[-1]["body"]
    r = c.complete("grade", [{"role": "user", "content": "q"}], json_mode=True)
    assert not r.ok and r.error.startswith("http_404") and "model not found" in r.error


def test_committed_server_config_is_valid(monkeypatch):
    from server.settings import abspath, load_server_config, validate_server_config
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = load_server_config(abspath("server_config.json"))
    assert validate_server_config(cfg) == []
    assert all(cfg["llm"][r]["model"] for r in ("chat", "features", "support", "grade"))
    assert "OPENAI_API_KEY" not in json.dumps(cfg) and "sk-" not in json.dumps(cfg)
