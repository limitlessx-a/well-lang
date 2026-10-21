"""wellc driver: parse -> check -> C -> native."""
import os
import shutil
import subprocess
import sys

from .lexer import lex, LexError
from .parser import Parser, ParseError
from .checker import Checker, CheckError
from .codegen import CodeGen
from .ast_nodes import Program

HERE = os.path.dirname(os.path.abspath(__file__))
COMPILER_DIR = os.path.dirname(HERE)
RUNTIME_DIR = os.path.join(COMPILER_DIR, "runtime")
BUILD_DIR = os.path.join(COMPILER_DIR, "build")
REPO_ROOT = os.path.dirname(COMPILER_DIR)
RUNTIME_SOURCES = [
    "core/wellrt.c",
    "core/well_module.c",
    "core/well_thread.c",
    "core/well_model.c",
    "core/well_cache.c",
    "core/well_store.c",
    "core/well_containers.c",
    "core/well_event.c",
    "core/well_timer.c",
    "core/well_serialize.c",
    "core/well_json.c",
    "core/well_file.c",
]

MODULE_SOURCES = {
    "http": "modules/http/well_http.c",
    "ws": "modules/ws/well_ws.c",
    "mysql": "modules/mysql/well_mysql.c",
}
CORE_SOURCES = list(RUNTIME_SOURCES)
RUNTIME_SOURCES += list(MODULE_SOURCES.values())


class BuildError(Exception):
    pass


def find_cc() -> str:
    env = os.environ.get("WELL_CC")
    if env:
        return env
    for name in ("tcc", "gcc", "clang"):
        p = shutil.which(name)
        if p:
            return p
    # bundled TinyCC fallback (this workspace)
    p = os.path.join(REPO_ROOT, "tool_ai", "tools", "tcc", "tcc", "tcc.exe")
    if os.path.isfile(p):
        return p
    raise BuildError("no C compiler found; set WELL_CC or install tcc/gcc/clang")


def _parse_file(path: str) -> Program:
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    toks = lex(src, path)
    return Parser(toks, path).parse_program()


def _resolve_import(spec: str, base_dir: str) -> str | None:
    candidates = []
    if spec.startswith(".") or "/" in spec or "\\" in spec or spec.endswith(".we"):
        candidates.append(os.path.join(base_dir, spec))
        if not spec.endswith(".we"):
            candidates.append(os.path.join(base_dir, spec + ".we"))
    else:
        rel = spec.replace(".", os.sep)
        candidates.append(os.path.join(base_dir, rel + ".we"))
        candidates.append(os.path.join(base_dir, rel, "index.we"))
        candidates.append(os.path.join(base_dir, os.path.basename(rel) + ".we"))
    for c in candidates:
        if os.path.isfile(c):
            return os.path.abspath(c)
    return None


def load_program(path: str) -> Program:
    """Parse a .we file and merge imported local modules (declarations only)."""
    seen: set[str] = set()
    order: list[str] = []

    def visit(p: str):
        p = os.path.abspath(p)
        if p in seen:
            return
        seen.add(p)
        order.append(p)
        prog = _parse_file(p)
        base = os.path.dirname(p)
        for imp in prog.imports:
            ip = _resolve_import(imp.path, base)
            if ip and ip not in seen:
                visit(ip)

    visit(path)
    merged: list = []
    names: set[tuple[str, str]] = set()
    for p in order:
        prog = _parse_file(p)
        for d in prog.decls:
            key = (type(d).__name__, getattr(d, "name", ""))
            if key in names:
                continue
            names.add(key)
            merged.append(d)
        if os.path.abspath(p) == os.path.abspath(path):
            main = prog
    main.decls = merged
    return main


def compile_source(src: str, filename: str = "<src>") -> str:
    try:
        toks = lex(src, filename)
        prog = Parser(toks, filename).parse_program()
        prog = Checker(prog, filename).run()
        return CodeGen(prog).gen()
    except (LexError, ParseError, CheckError) as e:
        raise BuildError(str(e))


def build(path: str, out: str | None = None, keep_c: bool = False) -> tuple[str, str]:
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise BuildError(f"file not found: {path}")
    os.makedirs(BUILD_DIR, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0]
    c_path = os.path.join(BUILD_DIR, stem + ".c")
    try:
        prog = load_program(path)
        prog = Checker(prog, path).run()
        code = CodeGen(prog).gen()
    except (LexError, ParseError, CheckError) as e:
        raise BuildError(str(e))
    with open(c_path, "w", encoding="utf-8") as f:
        f.write(code)
    if out is None:
        out = os.path.join(BUILD_DIR, stem + (".exe" if os.name == "nt" else ""))
    out = os.path.abspath(out)
    cc = find_cc()
    cmd = [cc, "-I", os.path.join(RUNTIME_DIR, "core"),
           "-I", os.path.join(RUNTIME_DIR, "modules", "http"),
           "-I", os.path.join(RUNTIME_DIR, "modules", "ws"),
           "-I", os.path.join(RUNTIME_DIR, "modules", "mysql"),
           "-o", out, c_path]
    if os.name != "nt":
        cmd.append("-pthread")
    sources = list(CORE_SOURCES)
    if prog.uses_http:
        sources.append(MODULE_SOURCES["http"])
    if prog.uses_ws:
        sources.append(MODULE_SOURCES["ws"])
    if prog.uses_mysql:
        sources.append(MODULE_SOURCES["mysql"])
    cmd += [os.path.join(RUNTIME_DIR, name) for name in sources]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise BuildError("C compile failed:\n" + (r.stdout or "") + (r.stderr or ""))
    if not keep_c:
        pass  # keep generated C for debugging; harmless
    return c_path, out


def run(path: str, out: str | None = None) -> int:
    _, exe = build(path, out)
    r = subprocess.run([exe])
    return r.returncode


def emit_c(path: str, out: str | None = None) -> str:
    path = os.path.abspath(path)
    try:
        prog = load_program(path)
        prog = Checker(prog, path).run()
        code = CodeGen(prog).gen()
    except (LexError, ParseError, CheckError) as e:
        raise BuildError(str(e))
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(code)
    return code
