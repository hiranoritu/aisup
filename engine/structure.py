"""S 特徴：正規化構造差分（仕様 §4）。

`1 - 2L/(len_before+len_after)`（L は正規化構造系列の LCS 長）と、
差分に if/for/while/call/ポインタ関連の構造要素が含まれるかを返す。
比較不能（構文エラー等）なら None。

- TreeSitterAnalyzer : 本番用。Tree-sitter C の CST から系列を作る（要 tree_sitter, tree_sitter_c）。
- LexerApproxAnalyzer: 開発用の近似。トークン列から系列を作る。仕様の S ではないので本実験では使わない。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from .clex import TYPE_WORDS, brackets_balanced, lex


@dataclass(frozen=True)
class StructureResult:
    distance: float           # 1 - 2L/(la+lb)
    structural_change: bool   # 差分に構造要素を含むか
    len_before: int
    len_after: int
    analyzer: str


class StructureAnalyzer(Protocol):
    name: str

    def sequence(self, src: str) -> Optional[list[str]]: ...
    def compare(self, before_src: str, after_src: str) -> Optional[StructureResult]: ...


# 差分に含まれたら「構造変更あり」とみなす要素（両アナライザ共通の語彙に正規化済み）
STRUCT_ELEMENTS = frozenset({
    "if", "else", "for", "while", "do", "switch", "case", "return", "goto",
    "CALL", "PTR_DEREF", "ADDR_OF", "ARROW", "SUBSCRIPT", "PTR_DECL",
})


def lcs_diff(a: list[str], b: list[str]) -> tuple[int, list[str]]:
    """LCS 長と、LCS に含まれない要素（両側）のリスト。"""
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return 0, list(a) + list(b)
    # DP 表（n*m はAIコード片×対象範囲なので数万〜数十万セル程度）
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        ai = a[i]
        row, nxt = dp[i], dp[i + 1]
        for j in range(m - 1, -1, -1):
            if ai == b[j]:
                row[j] = nxt[j + 1] + 1
            else:
                row[j] = nxt[j] if nxt[j] >= row[j + 1] else row[j + 1]
    unmatched: list[str] = []
    i = j = 0
    while i < n and j < m:
        if a[i] == b[j]:
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            unmatched.append(a[i])
            i += 1
        else:
            unmatched.append(b[j])
            j += 1
    unmatched.extend(a[i:])
    unmatched.extend(b[j:])
    return dp[0][0], unmatched


def _compare_sequences(sa, sb, name: str) -> Optional[StructureResult]:
    if sa is None or sb is None:
        return None
    la, lb = len(sa), len(sb)
    if la + lb == 0:
        return StructureResult(0.0, False, 0, 0, name)
    L, unmatched = lcs_diff(sa, sb)
    dist = 1.0 - 2.0 * L / (la + lb)
    change = any(x in STRUCT_ELEMENTS for x in unmatched)
    return StructureResult(dist, change, la, lb, name)


class LexerApproxAnalyzer:
    """開発用近似。`*` `&` は前後の文脈から単項/二項を推定する。"""

    name = "lexer_approx"
    version_label = "lexer_approx"

    def sequence(self, src: str) -> Optional[list[str]]:
        res = lex(src)
        if res.errors or not brackets_balanced(res.tokens):
            return None
        toks = res.tokens
        seq: list[str] = []
        for i, t in enumerate(toks):
            prev = toks[i - 1] if i > 0 else None
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            unary_ctx = prev is None or (prev.kind == "op" and prev.value not in (")", "]")) or (
                prev.kind == "keyword" and prev.value not in TYPE_WORDS)
            after_type = prev is not None and (prev.value in TYPE_WORDS or prev.value == "*")
            if t.kind == "ident":
                seq.append("CALL" if nxt is not None and nxt.value == "(" else "ID")
            elif t.kind == "keyword":
                seq.append("TYPE" if t.value in TYPE_WORDS else t.value)
            elif t.kind == "number":
                seq.append("NUM")
            elif t.kind == "string":
                seq.append("STR")
            elif t.kind == "char":
                seq.append("CHR")
            elif t.kind == "preproc":
                seq.append("PP")
            elif t.value == "*":
                if after_type:
                    seq.append("PTR_DECL")
                elif unary_ctx:
                    seq.append("PTR_DEREF")
                else:
                    seq.append("*")
            elif t.value == "&":
                seq.append("ADDR_OF" if unary_ctx else "&")
            elif t.value == "->":
                seq.append("ARROW")
            elif t.value == "[":
                seq.append("SUBSCRIPT")
            elif t.value in (";", ",", "]"):
                continue  # 区切りは構造系列から除く
            else:
                seq.append(t.value)
        return seq

    def compare(self, before_src: str, after_src: str) -> Optional[StructureResult]:
        return _compare_sequences(self.sequence(before_src), self.sequence(after_src), self.name)


class TreeSitterAnalyzer:
    """本番用。Tree-sitter C の named node の前順走査系列。

    識別子名・コメント・位置情報を除き、ノード型のみを並べる。has_error なら None。
    検証は tests/test_structure_ts.py（版は requirements.txt で固定）。
    """

    name = "tree_sitter_c"
    _MAP = {
        "call_expression": "CALL",
        "pointer_expression": None,  # 演算子で分岐（* → PTR_DEREF, & → ADDR_OF）
        "pointer_declarator": "PTR_DECL",
        # v1.2-dev.2：ポインタ型へのキャスト・sizeof(int *) もポインタ関連の構造とする
        "abstract_pointer_declarator": "PTR_DECL",
        "subscript_expression": "SUBSCRIPT",
        "if_statement": "if",
        "else_clause": "else",
        "for_statement": "for",
        "while_statement": "while",
        "do_statement": "do",
        "switch_statement": "switch",
        "case_statement": "case",
        "return_statement": "return",
        "goto_statement": "goto",
    }
    _DROP = {"comment"}
    # v1.2-dev.2：リテラルの中身（string_content・escape_sequence・character）は比較しない。子をたどらずこの記号だけ置く
    _LEAF = {"string_literal": "STR", "char_literal": "CHR", "system_lib_string": "STR",
             "raw_string_literal": "STR"}
    _IDENT = {"identifier", "field_identifier", "type_identifier", "statement_identifier"}

    def __init__(self, expected_versions: Optional[dict] = None) -> None:
        import tree_sitter_c as tsc  # noqa: WPS433
        from tree_sitter import Language, Parser
        self.versions = installed_versions()
        if expected_versions:
            # 設定に記録した版と違えば止める（文法の版でノード型が変わり得るため。黙って別の版で判定しない）
            diff = {k: (v, self.versions.get(k)) for k, v in expected_versions.items() if self.versions.get(k) != v}
            if diff:
                raise RuntimeError(f"tree-sitter version mismatch (expected, installed): {diff}")
        self.version_label = "tree_sitter_c " + " ".join(f"{k}={v}" for k, v in sorted(self.versions.items()))
        self._parser = Parser(Language(tsc.language()))

    def sequence(self, src: str) -> Optional[list[str]]:
        tree = self._parser.parse(src.encode("utf-8"))
        root = tree.root_node
        if root.has_error:
            return None
        seq: list[str] = []
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type in self._DROP:
                continue
            if node.is_named and node is not root:
                if node.type in self._LEAF:
                    seq.append(self._LEAF[node.type])
                    continue
                if node.type in self._IDENT:
                    seq.append("ID")
                elif node.type == "pointer_expression":
                    op = node.child_by_field_name("operator")
                    seq.append("ADDR_OF" if op is not None and op.type == "&" else "PTR_DEREF")
                elif node.type == "field_expression":
                    op = node.child_by_field_name("operator")
                    seq.append("ARROW" if op is not None and op.type == "->" else "field")
                else:
                    mapped = self._MAP.get(node.type, node.type)
                    seq.append(mapped)
            stack.extend(reversed(node.children))
        return seq

    def compare(self, before_src: str, after_src: str) -> Optional[StructureResult]:
        return _compare_sequences(self.sequence(before_src), self.sequence(after_src), self.version_label)


def installed_versions() -> dict:
    """Tree-sitter 本体と C 文法の版（パッケージの版）。"""
    from importlib.metadata import PackageNotFoundError, version
    out = {}
    for pkg in ("tree-sitter", "tree-sitter-c"):
        try:
            out[pkg] = version(pkg)
        except PackageNotFoundError:
            out[pkg] = None
    return out


def get_analyzer(name: str, expected_versions: Optional[dict] = None) -> StructureAnalyzer:
    if name == "tree_sitter_c":
        return TreeSitterAnalyzer(expected_versions)  # 未導入なら ImportError をそのまま出す（黙って近似に落とさない）
    if name == "lexer_approx":
        return LexerApproxAnalyzer()
    raise ValueError(f"unknown structure analyzer: {name}")
