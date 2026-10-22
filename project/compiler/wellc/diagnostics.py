"""Structured diagnostics for ``wellc check``.

The lexer/parser embed exact positions in their error messages
(``FILE:LINE:COL: message``), but semantic errors raised by the checker and
unresolved imports carry no position at all.  This module turns those errors
into JSON-serialisable diagnostics and maps the checker messages back onto
source ranges on a best-effort basis so editors/CI get useful squiggles.

Range convention: 1-based ``line``/``col``; the range starts at ``col`` and
ends at ``endCol`` (exclusive) on ``endLine``.
"""
import re

from .lexer import LexError

# FILE:LINE:COL: message -- the file part is greedy so Windows drive letters
# ("E:\\...") and colons inside paths survive.
_POS_RE = re.compile(r"^(?P<file>.*):(?P<line>\d+):(?P<col>\d+):\s?(?P<msg>.*)$", re.DOTALL)

# Identifiers quoted with single quotes in checker messages, e.g.
# "entry 'MissingController' is not a controller".
_QUOTED_RE = re.compile(r"'([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)'")

# Higher-confidence patterns get a lower tier (searched first).
_SPECIFIC_PATTERNS = [
    # "... has no field foo", "has no function foo", ...
    re.compile(r"has no (?:field|function|method|member)\s+([A-Za-z_]\w*)"),
    # "expected an event type (e.g. on(UserLoginEvent, Handler))"
    re.compile(r"\(e\.g\.\s*([A-Za-z_]\w*)\s*\("),
    # "Model.get: Foo is not a struct/event type"
    re.compile(r":\s*([A-Za-z_]\w*)\s+is\b"),
]

# Generic "<cue word> <identifier>" patterns, e.g. "unknown field foo on Bar".
_CUE_RE = re.compile(
    r"\b(?:identifier|variable|field|function|method|type|name|namespace|"
    r"controller|handler|enum|member|static|entry|var|statement|expression|kind)\s+"
    r"([A-Za-z_]\w*)"
)

# ALL-CAPS words in messages are usually Well identifiers/constants
# ("ALL is only allowed in on/once/off channel").
_ALLCAPS_RE = re.compile(r"\b([A-Z][A-Z0-9_]{1,})\b")

# "foo() expects 3 arguments", "foo(Handler) expects a handler", "foo: ..."
_LEADING_CALL_RE = re.compile(r"^([A-Za-z_]\w*)\s*(?:\(|expects\b)")
_LEADING_NAME_RE = re.compile(r"^([A-Za-z_]\w*):\s")
_TRAILING_NAME_RE = re.compile(r":\s*([A-Za-z_]\w*)\s*$")
# "U.f expects 1 arguments", "Color.fromInt: ...", "Json.stringify: ..."
_QUALIFIED_RE = re.compile(r"^([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b")

# Builtin call helpers whose name is also an English word; when a message
# starts with "on: ..." the raiser is talking about the on(...) call site.
_HELPER_CALLS = {"on", "once", "off", "emit", "start", "print", "stop"}

# English words that can follow a cue word ("type of var x") or start a
# sentence.  They are not identifiers worth locating.  Kept deliberately
# small: real identifier names such as "value" or "name" must not be lost.
_STOPWORDS = {
    "a", "an", "the", "of", "is", "are", "was", "were", "be", "been", "in",
    "on", "at", "to", "for", "from", "by", "with", "as", "into", "or", "and",
    "not", "no", "has", "have", "must", "cannot", "can", "takes", "expects",
    "requires", "allowed", "supported", "only", "some", "any", "all", "each",
    "this", "that", "it", "its", "new", "while", "if", "else", "true",
    "false", "null", "void",
}


def make(file: str, line: int, col: int, end_line: int, end_col: int,
         severity: str, message: str, code: str) -> dict:
    """Build one diagnostic object (the JSON schema used by ``wellc check``)."""
    return {
        "file": file,
        "line": max(1, int(line)),
        "col": max(1, int(col)),
        "endLine": max(1, int(end_line)),
        "endCol": max(1, int(end_col)),
        "severity": severity,
        "message": message,
        "code": code,
    }


def from_parse_error(exc: Exception, fallback_file: str) -> dict:
    """Turn a LexError/ParseError into a positioned diagnostic.

    Lex/parse messages are ``FILE:LINE:COL: message``; if the message was
    produced with a different filename than expected we still trust the
    embedded one (it is the file that actually failed).
    """
    text = str(exc)
    m = _POS_RE.match(text)
    code = "lex" if isinstance(exc, LexError) else "parse"
    if not m:
        return make(fallback_file, 1, 1, 1, 2, "error", text, code)
    file = m.group("file") or fallback_file
    line = int(m.group("line"))
    col = int(m.group("col"))
    msg = m.group("msg")
    return make(file, line, col, line, col + _error_token_len(msg), "error", msg, code)


def from_check_error(exc: Exception, file: str, src: str, tokens=None) -> dict:
    """Turn a CheckError (``FILE: message``, no position) into a diagnostic."""
    text = str(exc)
    prefix = file + ": "
    msg = text[len(prefix):] if text.startswith(prefix) else text
    return locate_semantic(file, src, msg, tokens)


def _error_token_len(msg: str) -> int:
    """Length of the offending token mentioned in "..., got 'x'" (min 1)."""
    m = re.search(r"got '([^']*)'", msg)
    if m:
        return max(1, len(m.group(1)))
    return 1


# --------------------------------------------------------------------------
# semantic errors
# --------------------------------------------------------------------------

def _extract_candidates(msg: str) -> list[tuple[int, str, bool]]:
    """Return ``(tier, identifier, call_only)`` candidates from a message.

    Lower tiers are more likely to be the identifier the report is about.
    """
    found: list[tuple[int, str, bool]] = []
    seen: set[str] = set()

    def add(tier: int, name: str, call_only: bool = False):
        # Quoted names and "(e.g. foo(...))" examples are high confidence and
        # may legitimately be English words that are also API identifiers
        # (on/once/off/emit/...), so only generic candidates are filtered.
        if not name or name in seen:
            return
        if tier > 0 and name in _STOPWORDS and not call_only:
            return
        seen.add(name)
        found.append((tier, name, call_only))

    for m in _QUOTED_RE.finditer(msg):
        add(0, m.group(1))

    for rx in _SPECIFIC_PATTERNS:
        for m in rx.finditer(msg):
            call_only = "e.g." in m.group(0)
            add(1, m.group(1), call_only)

    for m in _CUE_RE.finditer(msg):
        add(2, m.group(1))

    for m in _ALLCAPS_RE.finditer(msg):
        add(2, m.group(1))

    m = _LEADING_CALL_RE.match(msg)
    if m:
        if m.group(1) in _HELPER_CALLS:
            add(1, m.group(1), call_only=True)
        else:
            add(3, m.group(1))
    m = _LEADING_NAME_RE.match(msg)
    if m:
        if m.group(1) in _HELPER_CALLS:
            add(1, m.group(1), call_only=True)
        else:
            add(3, m.group(1))
    m = _QUALIFIED_RE.match(msg)
    if m:
        add(3, m.group(2), call_only=True)
    m = _TRAILING_NAME_RE.search(msg)
    if m:
        add(4, m.group(1))

    found.sort(key=lambda c: c[0])
    return found


def _token_index(src: str, file: str, tokens=None):
    """Map identifier value -> [(line, col)] and call name -> [(line, col)].

    Comments and string literals never contribute (they are not id/kw
    tokens), which is exactly the "skip comments and strings" requirement.
    """
    if tokens is None:
        from .lexer import lex
        try:
            tokens = lex(src, file)
        except LexError:
            return {}, {}
    idents: dict[str, list[tuple[int, int]]] = {}
    calls: dict[str, list[tuple[int, int]]] = {}
    for i, t in enumerate(tokens):
        if t.kind not in ("id", "kw"):
            continue
        idents.setdefault(t.value, []).append((t.line, t.col))
        if i + 1 < len(tokens):
            nxt = tokens[i + 1]
            if nxt.kind == "op" and nxt.value == "(":
                calls.setdefault(t.value, []).append((t.line, t.col))
    return idents, calls


def locate_semantic(file: str, src: str, msg: str, tokens=None) -> dict:
    """Best-effort range for a checker message that has no position."""
    idents, calls = _token_index(src, file, tokens)
    duplicate = "duplicate" in msg.lower()

    for _tier, name, call_only in _extract_candidates(msg):
        if call_only:
            positions = calls.get(name) or []
            if not positions:
                positions = idents.get(name) or []
        else:
            positions = idents.get(name) or []
            if not positions and "." in name:
                # e.g. 'A.B' -> try the final segment as a plain identifier.
                positions = idents.get(name.rsplit(".", 1)[-1]) or []
        if not positions:
            continue
        if duplicate and len(positions) > 1:
            # The duplicate is normally the second occurrence of the name.
            line, col = positions[1]
        else:
            line, col = positions[0]
        end_col = col + max(1, len(name.rsplit(".", 1)[-1]))
        return make(file, line, col, line, end_col, "error", msg, "semantic")

    # No identifier could be mapped: point at the start of the file.
    return make(file, 1, 1, 1, 2, "error", msg, "semantic")


# --------------------------------------------------------------------------
# unresolved imports
# --------------------------------------------------------------------------

def locate_import(file: str, src: str, spec: str, tokens=None) -> dict:
    """Warning range for an unresolved import.

    ``ImportDecl`` has no position, so walk the token stream for the
    ``import`` keyword and compare the following string / dotted path with
    the spec.  The returned range covers the ``import`` keyword.
    """
    message = f"unresolved import '{spec}'"
    if tokens is None:
        from .lexer import lex
        try:
            tokens = lex(src, file)
        except LexError:
            tokens = None
    if tokens is not None:
        n = len(tokens)
        i = 0
        while i < n:
            t = tokens[i]
            if t.kind == "kw" and t.value == "import":
                j = i + 1
                got = None
                if j < n and tokens[j].kind == "str":
                    got = tokens[j].value
                else:
                    parts: list[str] = []
                    while j < n and tokens[j].kind != "eof" and not (
                        tokens[j].kind == "op" and tokens[j].value == ";"
                    ):
                        tok = tokens[j]
                        if tok.kind in ("id", "kw"):
                            parts.append(tok.value)
                        elif tok.kind == "op" and tok.value == ".":
                            parts.append(".")
                        j += 1
                    got = "".join(parts)
                if got == spec:
                    return make(file, t.line, t.col, t.line, t.col + len("import"),
                                "warning", message, "import")
            i += 1
    return make(file, 1, 1, 1, 2, "warning", message, "import")
