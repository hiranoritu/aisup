"""S 特徴の本番アナライザ（Tree-sitter C）の検証（仕様 §4 の S）。

tree_sitter / tree_sitter_c が無ければ import の時点で失敗させる（skip にしない）。
本実験は Tree-sitter 版で判定するので、未導入のまま緑になってはいけない。
"""
import pytest

from engine import make_config, replay
from engine.config import DEFAULT_CONFIG
from engine.structure import (STRUCT_ELEMENTS, LexerApproxAnalyzer, TreeSitterAnalyzer,
                              get_analyzer, lcs_diff)

from builder import PROG_A, PROG_B, F, Log
from test_features import BROKEN


@pytest.fixture(scope="module")
def ts():
    return TreeSitterAnalyzer()


def S_of(r, cfg=DEFAULT_CONFIG):
    """compute_ru と同じ S の決め方（閾値は config から読む）。"""
    if r is None:
        return None
    return int(r.distance >= cfg["thresholds"]["S_dist"] and r.structural_change)


# ---------- 設定・生成 ----------
def test_default_config_uses_tree_sitter():
    assert DEFAULT_CONFIG["structure_analyzer"] == "tree_sitter_c"
    an = get_analyzer("tree_sitter_c")
    assert isinstance(an, TreeSitterAnalyzer) and an.name == "tree_sitter_c"


def test_engine_records_tree_sitter_as_S_analyzer():
    log = Log()
    log.task("t1")
    log.cycle_active_modify(F(h2=1))
    log.ask("次", F(h2=1))
    eng = replay(log.events)
    c = eng.cycles[0]
    assert c.features["S_analyzer"] == "tree_sitter_c"
    assert c.features["S"] == 1  # if とポインタ操作を書き足している


# ---------- 比較不能（構文エラー）→ None ----------
@pytest.mark.parametrize("src", BROKEN)
def test_never_raises_on_broken_code(ts, src):
    ts.sequence(src)  # 例外を出さないこと（None か list）
    ts.compare(src, PROG_A)


@pytest.mark.parametrize("src", [
    "int main( {",
    "int x = 1",              # セミコロン欠落（MISSING ノード）
    "if (x) { y = 1;",        # 閉じ括弧欠落
    'printf("abc',
    "/* never closed",
    "@@@ $$$",
])
def test_syntax_error_is_not_comparable(ts, src):
    assert ts.sequence(src) is None
    assert ts.compare(src, PROG_A) is None
    assert ts.compare(PROG_A, src) is None


# ---------- 正規化：識別子名・コメント・位置情報を除く ----------
def test_identical_is_zero(ts):
    for prog in (PROG_A, PROG_B):
        r = ts.compare(prog, prog)
        assert r.distance == 0.0 and not r.structural_change and r.analyzer == "tree_sitter_c"


def test_rename_is_not_structural(ts):
    r = ts.compare("int f(int a){ return a + 1; }", "int g(int value){ return value + 1; }")
    assert r.distance == 0.0 and not r.structural_change


def test_comments_are_dropped(ts):
    a = "int f(int a){ return a + 1; }"
    b = "// 足し算\nint f(int a){ /* 1 を足す */ return a + 1; } // end"
    assert ts.sequence(a) == ts.sequence(b)


def test_whitespace_and_position_are_dropped(ts):
    a = "int t = *x; *x = *y; *y = t;"
    b = "\n\n    int t = *x;\n\t*x   =   *y;\n*y=t;\n"
    assert ts.sequence(a) == ts.sequence(b)


def test_non_ascii_source(ts):
    """UTF-8 の多バイト文字（日本語の文字列・コメント）で壊れない。"""
    a = 'int main(void){ printf("こんにちは\\n"); return 0; }'
    b = 'int main(void){ /* 挨拶 */ printf("さようなら\\n"); return 0; }'
    r = ts.compare(a, b)
    assert r is not None and r.distance == 0.0


# ---------- 構造要素の語彙（両アナライザ共通） ----------
def test_pointer_labels(ts):
    seq = ts.sequence("void f(int *x) { int y = *x; int *p = &y; }")
    assert seq.count("PTR_DECL") == 2
    assert "PTR_DEREF" in seq and "ADDR_OF" in seq
    assert "pointer_expression" not in seq and "pointer_declarator" not in seq


def test_arrow_vs_dot(ts):
    dot, arrow = ts.sequence("p.x = 1;"), ts.sequence("p->x = 1;")
    assert "ARROW" in arrow and "ARROW" not in dot
    r = ts.compare("p.x = 1;", "p->x = 1;")
    assert r.structural_change


def test_identifiers_collapse_to_ID(ts):
    seq = ts.sequence("struct node { int v; }; typedef int myint; void f(struct node *n){ n->v = 1; L: goto L; }")
    assert {"identifier", "field_identifier", "type_identifier", "statement_identifier"}.isdisjoint(seq)
    assert "ID" in seq and "goto" in seq


def test_control_flow_labels(ts):
    src = """
    int f(int n) {
        int s = 0;
        for (int i = 0; i < n; i++) { if (i % 2) s += i; else s -= i; }
        while (n > 0) n--;
        do { n++; } while (n < 3);
        switch (n) { case 1: return 1; default: break; }
        return g(s);
    }"""
    seq = ts.sequence(src)
    for lab in ("for", "if", "else", "while", "do", "switch", "case", "return", "CALL"):
        assert lab in seq, lab
    # 写像先はすべて STRUCT_ELEMENTS に含まれる
    mapped = {v for v in TreeSitterAnalyzer._MAP.values() if v is not None} | {"PTR_DEREF", "ADDR_OF", "ARROW"}
    assert mapped <= STRUCT_ELEMENTS


# ---------- 距離の式と S の判定 ----------
def test_distance_formula(ts):
    a = "int x = 1;"
    b = "int x = 1; if (x) { x = 2; }"
    sa, sb = ts.sequence(a), ts.sequence(b)
    L, _ = lcs_diff(sa, sb)
    r = ts.compare(a, b)
    assert (r.len_before, r.len_after) == (len(sa), len(sb))
    assert r.distance == pytest.approx(1 - 2 * L / (len(sa) + len(sb)))
    assert 0.0 <= r.distance <= 1.0


def test_distance_symmetric(ts):
    a, b = "x = a[i];", "x = *(a + i);"
    assert ts.compare(a, b).distance == pytest.approx(ts.compare(b, a).distance)
    assert ts.compare(a, b).structural_change == ts.compare(b, a).structural_change


def test_literal_only_change_is_not_structural(ts):
    r = ts.compare("int x = 1; return x;", "int x = 42; return x;")
    assert r.distance == 0.0 and not r.structural_change


# 両アナライザで S（0/1/None）が一致すべき代表例。現行の受け入れテストでも全サイクル一致を確認済み。
PAIRS = [
    ("void f(int x) { int y = x; }", "void f(int *x) { int y = *x; }", 0),   # 構造変更ありだが距離 < 0.10
    ("int x = 1;", "int x = 1; if (x) { x = 2; }", 1),
    ("for (i=0;i<n;i++) s+=a[i];", "while (i<n) { s+=a[i]; i++; }", 1),
    ("x = a[i];", "x = *(a + i);", 1),
    ("p.x = 1;", "p->x = 1;", 1),
    ("int f(int a){ return a + 1; }", "int f(int b){ return b + 1; }", 0),
    ("int main( {", PROG_A, None),
    (PROG_A, PROG_B, 1),
]


@pytest.mark.parametrize("before,after,expected", PAIRS)
def test_S_matches_expected_and_lexer_approx(ts, before, after, expected):
    assert S_of(ts.compare(before, after)) == expected
    assert S_of(LexerApproxAnalyzer().compare(before, after)) == expected


def test_S_threshold_comes_from_config(ts):
    """閾値は config から読む（ここでは値を変えた設定で境界の動きだけ確認する）。"""
    r = ts.compare("void f(int x) { int y = x; }", "void f(int *x) { int y = *x; }")
    assert S_of(r) == 0
    assert S_of(r, make_config({"thresholds": {"S_dist": 0.05}})) == 1


def test_deterministic(ts):
    """同じ入力からは同じ系列（再生可能性）。別インスタンスでも同じ。"""
    other = TreeSitterAnalyzer()
    for src in (PROG_A, PROG_B):
        assert ts.sequence(src) == ts.sequence(src) == other.sequence(src)


def test_deeply_nested_does_not_recurse(ts):
    src = "int f(int x){ return " + "(" * 500 + "x" + ")" * 500 + "; }"
    seq = ts.sequence(src)
    assert seq is not None and seq.count("parenthesized_expression") == 500
