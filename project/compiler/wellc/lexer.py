"""Well lexer: .we -> tokens."""
from dataclasses import dataclass

KEYWORDS = {
    "module", "import", "entry", "const", "enum", "struct", "event",
    "controller", "handler", "func", "extends",
    "var", "new", "if", "else", "while",
    "for", "return", "break", "continue", "true", "false", "null",
    "void", "bool", "int", "long", "float", "double", "String",
}

MULTI_OPS = ["==", "!=", "<=", ">=", "&&", "||", "??", "+=", "-=", "->"]
SINGLE_OPS = "{}()[];,:.?+-*/%<>=!&|^~"


@dataclass
class Token:
    kind: str      # id / int / float / str / op / kw / eof
    value: str
    line: int
    col: int

    def __repr__(self):
        return f"Token({self.kind},{self.value!r},L{self.line})"


class LexError(Exception):
    pass


def lex(src: str, filename: str = "<src>") -> list[Token]:
    toks: list[Token] = []
    i, line, col = 0, 1, 1
    n = len(src)

    def err(msg):
        raise LexError(f"{filename}:{line}:{col}: {msg}")

    while i < n:
        c = src[i]
        if c in " \t\r\n":
            if c == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1
            continue
        if src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
            continue
        if src.startswith("/*", i):
            i += 2
            col += 2
            while i < n and not src.startswith("*/", i):
                if src[i] == "\n":
                    line += 1
                    col = 1
                else:
                    col += 1
                i += 1
            if i >= n:
                err("unterminated block comment")
            i += 2
            col += 2
            continue
        if c.isalpha() or c == "_":
            start = i
            while i < n and (src[i].isalnum() or src[i] == "_"):
                i += 1
            text = src[start:i]
            kind = "kw" if text in KEYWORDS else "id"
            toks.append(Token(kind, text, line, col))
            col += i - start
            continue
        if c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            start = i
            is_float = False
            if src.startswith(("0x", "0X"), i):
                i += 2
                while i < n and src[i] in "0123456789abcdefABCDEF":
                    i += 1
            else:
                while i < n and src[i].isdigit():
                    i += 1
                if i < n and src[i] == ".":
                    is_float = True
                    i += 1
                    while i < n and src[i].isdigit():
                        i += 1
                if i < n and src[i] in "eE":
                    is_float = True
                    i += 1
                    if i < n and src[i] in "+-":
                        i += 1
                    while i < n and src[i].isdigit():
                        i += 1
            text = src[start:i]
            toks.append(Token("float" if is_float else "int", text, line, col))
            col += i - start
            continue
        if c == '"':
            i += 1
            col += 1
            buf = []
            while i < n and src[i] != '"':
                if src[i] == "\\":
                    i += 1
                    col += 1
                    if i >= n:
                        err("unterminated string")
                    esc = src[i]
                    mapping = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "0": "\0"}
                    buf.append(mapping.get(esc, esc))
                else:
                    buf.append(src[i])
                i += 1
                col += 1
            if i >= n:
                err("unterminated string")
            i += 1
            col += 1
            toks.append(Token("str", "".join(buf), line, col))
            continue
        matched = False
        for op in MULTI_OPS:
            if src.startswith(op, i):
                toks.append(Token("op", op, line, col))
                i += len(op)
                col += len(op)
                matched = True
                break
        if matched:
            continue
        if c in SINGLE_OPS:
            toks.append(Token("op", c, line, col))
            i += 1
            col += 1
            continue
        err(f"unexpected character {c!r}")
    toks.append(Token("eof", "", line, col))
    return toks
