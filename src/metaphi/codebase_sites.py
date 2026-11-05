from __future__ import annotations

import re

_C = frozenset(["go", "java", "kt", "kts", "scala", "groovy", "js", "jsx", "mjs", "cjs", "ts", "tsx", "c", "h", "cc",
                "cpp", "cxx", "hpp", "hh", "cs", "rs", "dart", "swift", "php", "m", "mm"])
_HASH = frozenset(["py", "rb"])
_CHAR = frozenset(["go", "java", "kt", "kts", "scala", "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "cs", "rs", "swift",
                   "m", "mm"])
_TRIPLE = frozenset(["py", "kt", "kts", "scala", "swift", "groovy", "dart"])
_BACKTICK = frozenset(["go", "js", "jsx", "mjs", "cjs", "ts", "tsx"])
_WORDS = {"py": ("and", "or", "True", "False"), "rb": ("true", "false")}
_C_WORDS = ("true", "false")

OPS = ("cmp", "logic", "arith", "bool")
SWAP = {"==": "!=", "!=": "==", "===": "!==", "!==": "===", "<": "<=", "<=": "<", ">": ">=", ">=": ">",
        "&&": "||", "||": "&&", "and": "or", "or": "and", "+": "-", "-": "+",
        "true": "false", "false": "true", "True": "False", "False": "True"}
_OF = {"==": "cmp", "!=": "cmp", "===": "cmp", "!==": "cmp", "<": "cmp", "<=": "cmp", ">": "cmp", ">=": "cmp",
       "&&": "logic", "||": "logic", "and": "logic", "or": "logic", "+": "arith", "-": "arith",
       "true": "bool", "false": "bool", "True": "bool", "False": "bool"}
_SPACED = frozenset(["<", ">", "+", "-"])
_TOKEN = re.compile(r"===|!==|<=>|==|!=|<<=?|>>>?=?|<=|>=|&&|\|\||->|=>|<-|\+\+|--|\+=|-=|[<>+-]|\b[A-Za-z_]\w*\b")
_OPEN = {"c": re.compile(r"//|/\*|\"\"\"|[\"'`]"), "hash": re.compile(r"#|\"\"\"|'''|[\"']")}


def family(ext: str) -> str | None:
    return "c" if ext in _C else "hash" if ext in _HASH else None


def _blank(text: str, ext: str) -> str:
    fam = family(ext)
    out = list(text)
    opens, n, i = _OPEN[fam], len(text), 0

    def clear(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "
    while True:
        m = opens.search(text, i)
        if not m:
            break
        s, tok = m.start(), m.group()
        if tok in ("//", "#"):
            e = text.find("\n", s)
            e = n if e < 0 else e
        elif tok == "/*":
            e = text.find("*/", s + 2)
            e = n if e < 0 else e + 2
        elif tok in ('"""', "'''"):
            if ext not in _TRIPLE:
                e = _closed(text, s, '"', False)
            else:
                e = text.find(tok, s + 3)
                e = n if e < 0 else e + 3
        elif tok == "`":
            if ext not in _BACKTICK:
                i = s + 1
                continue
            e = _closed(text, s, "`", True)
        elif tok == "'" and ext in _CHAR:
            e = _closed(text, s, "'", False)
            if e - s > 12 or text[e - 1:e] != "'":
                i = s + 1
                continue
        else:
            e = _closed(text, s, tok, False)
        clear(s, e)
        i = max(e, s + 1)
    return "".join(out)


def _closed(text: str, s: int, q: str, multiline: bool) -> int:
    k, n = s + 1, len(text)
    while k < n:
        c = text[k]
        if c == "\\" and q != "`":
            k += 2
            continue
        if c == q:
            return k + 1
        if c == "\n" and not multiline:
            return k
        k += 1
    return n


def sites(text: str, ext: str) -> list[tuple[str, int, int]]:
    fam = family(ext)
    if fam is None:
        return []
    words = _WORDS.get(ext, _C_WORDS)
    plain = _blank(text, ext)
    found = []
    for m in _TOKEN.finditer(plain):
        tok, s, e = m.group(), m.start(), m.end()
        op = _OF.get(tok)
        if op is None or (tok[0].isalpha() and tok not in words):
            continue
        if tok in _SPACED and not (s > 0 and plain[s - 1] in " \t" and e < len(plain) and plain[e] in " \t"):
            continue
        found.append((op, s, e))
    return found


def counts(text: str, ext: str) -> dict[str, int] | None:
    if family(ext) is None:
        return None
    got = dict.fromkeys(OPS, 0)
    for op, _s, _e in sites(text, ext):
        got[op] += 1
    return got


def swapped(text: str, ext: str, op: str, nth: int) -> str | None:
    at = [x for x in sites(text, ext) if x[0] == op]
    if not 0 <= nth < len(at):
        return None
    _op, s, e = at[nth]
    return text[:s] + SWAP[text[s:e]] + text[e:]


__all__ = ["OPS", "SWAP", "counts", "family", "sites", "swapped"]
