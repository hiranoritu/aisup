"""特徴量・規則の単体テスト。"""
from engine import replay
from engine.clex import brackets_balanced, find_functions, lex
from engine.hs import classify_hs, parse_prompt_features
from engine.ru import compute_D
from engine.structure import LexerApproxAnalyzer

from builder import PROG_A, PROG_B, F, Log, code_block


# ---------- 字句解析：壊れたコードで止まらない（§10-4） ----------
BROKEN = [
    "int main( {",
    'printf("unterminated',
    "/* never closed",
    "int *p = &a; p++; *(p + 1) = 3; int **pp = &p; **pp = 4;",
    "char c = '\\",
    "#define MAX(a,b) \\\n ((a)>(b)?(a):(b))\nint x = MAX(1,2);",
    "int a[3] = {1,2,3}; int *q = a + 2; q->x;",
    "@@@ $$$ ```",
    "",
]


def test_lexer_never_raises():
    for src in BROKEN:
        res = lex(src)
        assert isinstance(res.tokens, list)


def test_lexer_reports_errors():
    assert lex('printf("abc').errors
    assert lex("/* abc").errors
    assert not lex(PROG_A).errors


def test_structure_none_on_syntax_error():
    an = LexerApproxAnalyzer()
    assert an.compare("int main( {", PROG_A) is None
    assert an.compare(PROG_A, PROG_A).distance == 0.0


def test_structure_detects_pointer_change():
    an = LexerApproxAnalyzer()
    before = "void f(int x) { int y = x; }"
    after = "void f(int *x) { int y = *x; }"
    r = an.compare(before, after)
    assert r is not None and r.structural_change and r.distance > 0


def test_rename_is_not_structural():
    an = LexerApproxAnalyzer()
    r = an.compare("int f(int a){ return a + 1; }", "int f(int b){ return b + 1; }")
    assert r.distance == 0.0


def test_find_functions():
    funcs = find_functions(lex(PROG_B).tokens)
    assert set(funcs) == {"swap", "main"}
    assert brackets_balanced(lex(PROG_B).tokens)


# ---------- D ----------
def test_D_values():
    a = lex("int x = 1;").values
    assert compute_D(a, a) == 0.0
    b = lex("int x = 1; x = x + 2;").values
    assert compute_D(a, b) > 0.10


def test_rename_only_flagged():
    log = Log()
    log.task("t1")
    p = log.ask("q", F(h2=1))
    r = log.respond(code_block(PROG_A), p)
    log.paste_replace_all(PROG_A, r)
    renamed = PROG_A.replace("int a = 3", "int value = 3").replace("&a", "&value").replace("*p", "*ptr").replace("int *p", "int *ptr").replace("int *ptrtr", "int *ptr")
    log.type_replace_all(renamed)
    log.run("")
    log.ask("next", F(h2=1))
    c = replay(log.events).cycles[0]
    assert c.features["D"] >= 0.10
    assert c.features["D_norm"] < 0.10
    assert "rename_only_suspected" in c.review


# ---------- HS ----------
def test_hs_rules():
    def hs(**kw):
        f, err = parse_prompt_features(F(**kw))
        return classify_hs(f, err)
    assert hs().label == "Passive"
    assert hs(h2=1).label == "Active"
    assert hs(h2=1, h3=1).label == "Constructive"
    assert hs(h4=1).label == "Constructive"
    r = hs(h1=1, h3=1)
    assert r.label == "Constructive" and r.conflict


def test_hs_parse_failure_is_unknown_not_passive():
    f, err = parse_prompt_features("{not json")
    assert f is None
    assert classify_hs(f, err).label is None
    bad = F(h2=1)
    bad["spans"] = {}
    f, err = parse_prompt_features(bad)
    assert f is None and err == "missing_span_h2"


def test_llm_failure_gives_pending():
    log = Log()
    log.task("t1")
    p = log.ask("q", None, ok=False)
    r = log.respond(code_block(PROG_A), p)
    log.paste_replace_all(PROG_A, r)
    log.run("")
    log.ask("next", F(h2=1))
    c = replay(log.events).cycles[0]
    assert c.hs is None and c.ru == "Passive" and c.state == "Pending"


# ---------- 確認質問とQ ----------
def test_followup_merges_into_cycle_and_sets_Q():
    log = Log()
    log.task("t1")
    p = log.ask("q", F(h2=1))
    r = log.respond(code_block(PROG_A), p)
    log.paste_replace_all(PROG_A, r)
    p2 = log.ask("この *p は何を指していますか？", F(followup=1, asks=1))
    log.respond("a を指しています。", p2)
    log.ask("別の質問", F(h2=1))
    eng = replay(log.events)
    assert len(eng.decisions) == 2          # 確認質問は介入機会にならない
    c = eng.cycles[0]
    assert len(c.prompt_event_ids) == 2
    assert c.features["Q"] == 1
    assert c.ru == "Active" and "Q" in c.ru_basis


def test_low_confidence_new_question_treated_as_followup():
    log = Log()
    log.task("t1")
    log.ask("q", F(h2=1))
    log.ask("これは？", F(followup=0, conf=0.4))
    eng = replay(log.events)
    assert len(eng.decisions) == 1
    assert "followup_low_confidence" in eng.cycles[0].review
    assert len(eng.cycles[0].prompt_event_ids) == 2


# ---------- C・由来追跡 ----------
def test_C_from_ai_paste():
    log = Log()
    log.task("t1")
    p = log.ask("q", F(h2=1))
    r = log.respond(code_block(PROG_A), p)
    log.paste_replace_all(PROG_A, r)
    log.ask("next", F(h2=1))
    c = replay(log.events).cycles[0]
    assert c.features["C"] == 1.0
    assert c.ru == "Passive"


def test_retyped_ai_code_is_not_counted_as_C():
    """AIコードを見ながら打ち直した場合、C は AI 由来にならない（text_similarity は高い）。"""
    log = Log()
    log.task("t1")
    p = log.ask("q", F(h2=1))
    log.respond(code_block(PROG_A), p)
    log.type_replace_all(PROG_A)
    log.ask("next", F(h2=1))
    c = replay(log.events).cycles[0]
    assert c.features["C"] == 0.0
    assert c.features["text_similarity"] == 1.0
    assert c.ru == "Unknown"   # D<0.1, S=0, C<0.8 → Passive の根拠なし


def test_snapshot_mismatch_makes_origin_unknown():
    log = Log()
    log.task("t1")
    p = log.ask("q", F(h2=1))
    r = log.respond(code_block(PROG_A), p)
    log.paste_replace_all(PROG_A, r)
    log.snapshot(PROG_A + "\n// 取りこぼした編集\nint g;")
    log.ask("next", F(h2=1))
    eng = replay(log.events)
    c = eng.cycles[0]
    assert c.features["C"] is None
    assert "origin:edit_log_gap" in c.unknown_reasons
    assert any(a["anomaly"] == "snapshot_mismatch" for a in eng.anomalies)


# ---------- E・T と Constructive ----------
def test_E_pattern2_constructive():
    log = Log()
    log.task("t1")
    p = log.ask("自分は swap が値渡しだから動かないと思う", F(h2=1, h3=1))
    r = log.respond(code_block(PROG_B), p)
    log.paste_replace_all(PROG_B, r)
    log.run("1")
    log.run("2")
    log.type_at("swap(&a, &b);", "\n    swap(&a, &b);")
    log.run("1")
    log.ask("next", F(h2=1))
    c = replay(log.events).cycles[0]
    assert c.features["E"] == 1
    assert c.features["T"] >= 2
    assert c.state == "C-C"


def test_repeated_identical_runs_count_once():
    log = Log()
    log.task("t1")
    p = log.ask("q", F(h2=1))
    r = log.respond(code_block(PROG_A), p)
    log.paste_replace_all(PROG_A, r)
    for _ in range(5):
        log.run("")
    log.ask("next", F(h2=1))
    c = replay(log.events).cycles[0]
    assert c.features["T_runs"] == 5 and c.features["T"] == 1


def test_E_pattern1_hypothesis_edit_run():
    log = Log()
    log.task("t1")
    p = log.ask("q", F(h2=1))
    r = log.respond(code_block(PROG_A), p)
    log.paste_replace_all(PROG_A, r)
    log.ask("*p は a のアドレスではなく値だと思うので確かめたい", F(h3=1, followup=1))
    log.type_at("printf(\"%d\\n\", *p);", "\n    printf(\"%p\\n\", (void*)p);")
    log.run("")
    log.ask("next", F(h2=1))
    c = replay(log.events).cycles[0]
    assert c.features["E"] == 1


def test_I_self_written_function():
    """AIの例とは構造の違う関数を自分で書いて実行 → I=1。"""
    log = Log()
    log.task("t1", starter="#include <stdio.h>\n")
    p = log.ask("q", F(h2=1))
    log.respond(code_block("void inc(int *x) {\n    *x = *x + 1;\n}"), p)
    own = ("#include <stdio.h>\nvoid inc(int *x) {\n    int i;\n    for (i = 0; i < 3; i++) {\n"
           "        if (x[i] > 0) {\n            x[i]++;\n        }\n    }\n    printf(\"%d\\n\", x[0]);\n}\n")
    log.type_replace_all(own)
    log.run("")
    log.ask("next", F(h2=1))
    c = replay(log.events).cycles[0]
    assert c.features["I"] == 1


def test_off_task_holds_adaptive():
    log = Log()
    log.task("t1")
    log.cycle_passive_adopt(F(h2=1, off=1))
    log.cycle_passive_adopt(F(h2=1), PROG_B)
    log.ask("3", F(h2=1))
    d = replay(log.events).decisions[2]
    assert d.support_type is None and d.reason == "hold_off_task"


def test_edit_out_of_range_marks_gap():
    log = Log()
    log.task("t1")
    p = log.ask("q", F(h2=1))
    log.respond(code_block(PROG_A), p)
    log.add("CODE_EDIT", {"offset": 999, "delete_len": 0, "insert_text": "x", "kind": "typed"})
    log.ask("next", F(h2=1))
    eng = replay(log.events)
    assert any(a["anomaly"].startswith("edit_failed") for a in eng.anomalies)
