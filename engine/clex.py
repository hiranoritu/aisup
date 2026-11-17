"""C言語の字句解析（トークン列）。

D・C・I・text_similarity のトークン数計算に使う。構文木（S）は structure.py。
未完成・不正なコードでも例外を出さず、errors に記録して最後まで走査する（仕様 §10）。
コメントはトークンに含めない。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

KEYWORDS = frozenset(
    "auto break case char const continue default do double else enum extern float for goto "
    "if inline int long register restrict return short signed sizeof static struct switch "
    "typedef union unsigned void volatile while _Bool _Complex _Imaginary bool".split()
)
TYPE_WORDS = frozenset(
    "char double float int long short signed unsigned void _Bool bool const volatile static "
    "extern register auto restrict inline size_t".split()
)

_OPS = sorted(
    "... >>= <<= -> ++ -- << >> <= >= == != && || += -= *= /= %= &= |= ^= ## "
    "+ - * / % < > = ! ~ & | ^ ? : ; , . ( ) [ ] { } #".split(),
    key=len, reverse=True,
)

_MASTER = re.compile(
    r"(?P<ws>\s+)"
    r"|(?P<lcomment>//[^\n]*)"
    r"|(?P<bcomment>/\*.*?(?:\*/|\Z))"
    r"|(?P<string>\"(?:\\.|[^\"\\\n])*\"?)"
    r"|(?P<char>'(?:\\.|[^'\\\n])*'?)"
    r"|(?P<number>(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)[uUlLfF]*)"
    r"|(?P<ident>[A-Za-z_]\w*)"
    r"|(?P<op>" + "|".join(re.escape(o) for o in _OPS) + r")"
    r"|(?P<other>.)",
    re.S,
)


_STRING_DONE = re.compile(r"\"(?:\\.|[^\"\\\n])*\"")
_CHAR_DONE = re.compile(r"'(?:\\.|[^'\\\n])*'")


@dataclass(frozen=True)
class Token:
    kind: str   # keyword|ident|number|string|char|op|preproc|other
    value: str
    start: int  # 文字オフセット（含む）
    end: int    # 文字オフセット（含まない）


@dataclass
class LexResult:
    tokens: list[Token]
    errors: list[str] = field(default_factory=list)

    @property
    def values(self) -> list[str]:
        return [t.value for t in self.tokens]


def _at_line_start(src: str, pos: int) -> bool:
    i = pos - 1
    while i >= 0 and src[i] in " \t":
        i -= 1
    return i < 0 or src[i] == "\n"


def lex(src: str) -> LexResult:
    tokens: list[Token] = []
    errors: list[str] = []
    pos, n = 0, len(src)
    while pos < n:
        if src[pos] == "#" and _at_line_start(src, pos):
            # プリプロセッサ行（行継続 \ を含む）は1トークン
            end = pos
            while end < n:
                nl = src.find("\n", end)
                if nl == -1:
                    end = n
                    break
                if src[nl - 1] == "\\":
                    end = nl + 1
                    continue
                end = nl
                break
            text = " ".join(src[pos:end].split())
            tokens.append(Token("preproc", text, pos, end))
            pos = end
            continue
        m = _MASTER.match(src, pos)
        if m is None:  # 到達しない想定（other が任意1文字に一致）
            errors.append(f"unlexable@{pos}")
            pos += 1
            continue
        kind = m.lastgroup
        val = m.group()
        if kind in ("ws", "lcomment"):
            pass
        elif kind == "bcomment":
            if not val.endswith("*/") or len(val) < 4:
                errors.append(f"unterminated_comment@{pos}")
        elif kind == "string":
            if not _STRING_DONE.fullmatch(val):
                errors.append(f"unterminated_string@{pos}")
            tokens.append(Token("string", val, pos, m.end()))
        elif kind == "char":
            if not _CHAR_DONE.fullmatch(val):
                errors.append(f"unterminated_char@{pos}")
            tokens.append(Token("char", val, pos, m.end()))
        elif kind == "ident":
            tokens.append(Token("keyword" if val in KEYWORDS else "ident", val, pos, m.end()))
        elif kind == "other":
            errors.append(f"unexpected_char@{pos}")
            tokens.append(Token("other", val, pos, m.end()))
        else:
            tokens.append(Token(kind, val, pos, m.end()))
        pos = m.end()
    return LexResult(tokens, errors)


def normalize_identifiers(tokens: list[Token]) -> list[str]:
    """識別子を ID に置き換えた値列（機械的な改名の検出用）。"""
    return ["ID" if t.kind == "ident" else t.value for t in tokens]


def brackets_balanced(tokens: list[Token]) -> bool:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    for t in tokens:
        if t.kind != "op":
            continue
        if t.value in "([{":
            stack.append(t.value)
        elif t.value in pairs:
            if not stack or stack[-1] != pairs[t.value]:
                return False
            stack.pop()
    return not stack


def find_functions(tokens: list[Token]) -> dict[str, tuple[int, int]]:
    """トップレベルの関数定義を {名前: (開始トークン添字, 終了トークン添字・含まない)} で返す。

    `ident ( ... ) {` の形を深さ0で探す近似。宣言子の型部分は直前の
    `;` `}` プリプロセッサ行の直後から含める。
    """
    funcs: dict[str, tuple[int, int]] = {}
    depth = 0
    i, n = 0, len(tokens)
    stmt_start = 0
    while i < n:
        t = tokens[i]
        if t.kind == "op" and t.value == "{" and depth == 0:
            # 直前が ) なら関数本体の可能性
            j = i - 1
            if j >= 0 and tokens[j].value == ")":
                # 対応する ( を探す
                d = 0
                k = j
                while k >= 0:
                    if tokens[k].value == ")":
                        d += 1
                    elif tokens[k].value == "(":
                        d -= 1
                        if d == 0:
                            break
                    k -= 1
                if k > 0 and tokens[k - 1].kind == "ident":
                    name = tokens[k - 1].value
                    # 本体の終わり
                    d = 0
                    m = i
                    while m < n:
                        if tokens[m].value == "{":
                            d += 1
                        elif tokens[m].value == "}":
                            d -= 1
                            if d == 0:
                                break
                        m += 1
                    end = min(m + 1, n)
                    funcs.setdefault(name, (stmt_start, end))
                    i = end
                    stmt_start = end
                    continue
            depth += 1
        elif t.kind == "op" and t.value == "{":
            depth += 1
        elif t.kind == "op" and t.value == "}":
            depth = max(0, depth - 1)
            if depth == 0:
                stmt_start = i + 1
        elif depth == 0 and (t.kind == "preproc" or (t.kind == "op" and t.value == ";")):
            stmt_start = i + 1
        i += 1
    return funcs
