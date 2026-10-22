"""wellc driver: parse -> check -> C -> native."""
import os
import shutil
import shlex
import subprocess
import sys

from .lexer import lex, LexError
from .parser import Parser, ParseError
from .checker import Checker, CheckError
from .codegen import CodeGen
from .ast_nodes import Program
from .diagnostics import from_check_error, from_parse_error, locate_import

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


class CheckIOError(Exception):
    """The check subcommand could not read a source file (exit code 2)."""


def find_cc() -> str:
    env = os.environ.get("WELL_CC")
    if env:
        return env
    # Prefer the repo-local TinyCC (fast compile, default flags) so that a
    # gcc/clang installed on PATH does not silently become the default and slow
    # down every build/test. Use WELL_CC=gcc/clang explicitly for optimized builds.
    p = os.path.join(REPO_ROOT, "tool_ai", "tools", "tcc", "tcc", "tcc.exe")
    if os.path.isfile(p):
        return p
    # POSIX: TinyCC 0.9.27 has no __atomic_*/__sync_* builtins, which the
    # multithreaded runtime needs, so prefer gcc/clang there. TCC stays the
    # default on Windows (where the runtime uses Interlocked instead).
    names = ("gcc", "clang", "tcc") if os.name != "nt" else ("tcc", "gcc", "clang")
    for name in names:
        p = shutil.which(name)
        if p:
            return p
    raise BuildError("no C compiler found; set WELL_CC or install tcc/gcc/clang")


def cc_flags() -> list[str]:
    """Extra C compiler flags for the selected toolchain.

    WELL_CFLAGS always wins (an explicitly empty value disables the default).
    When WELL_CC/autodetection resolves to gcc or clang we default to -O2;
    TinyCC keeps its fast-compile/default-flags behaviour.
    """
    if "WELL_CFLAGS" in os.environ:
        raw = os.environ["WELL_CFLAGS"]
        parts = shlex.split(raw, posix=(os.name != "nt"))
        # Windows shlex(posix=False) keeps surrounding quotes; strip matched ones.
        return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in ("\"", "'") else p
                for p in parts]
    cc = os.path.basename(find_cc()).lower()
    if cc.startswith(("gcc", "clang")):
        return ["-O2"]
    return []


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


_NAMED_DECL_KINDS = {
    "StructDecl": "type",
    "ControllerDecl": "controller",
    "HandlerDecl": "handler",
    "FuncDecl": "function",
    "NamespaceDecl": "namespace",
    "StaticDecl": "static",
    "EnumDecl": "enum",
    "ConstDecl": "const",
}


def _check_duplicate_decls(prog: Program, path: str) -> None:
    """Duplicate declarations inside one file are an error (imports are merged
    separately and are deduplicated by name)."""
    seen: dict[str, str] = {}
    for d in prog.decls:
        label = _NAMED_DECL_KINDS.get(type(d).__name__)
        name = getattr(d, "name", None)
        if not label or not name:
            continue
        if name in seen:
            raise CheckError(f"{path}: duplicate declaration '{name}' (already defined as a {seen[name]})")
        seen[name] = label


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
        _check_duplicate_decls(prog, p)
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
           "-I", os.path.join(RUNTIME_DIR, "modules", "mysql")]
    cmd += cc_flags()
    cmd += ["-o", out, c_path]
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


# --------------------------------------------------------------------------
# check: parse + semantic analysis only (no C compiler, no build/ output)
# --------------------------------------------------------------------------

def _read_source(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise CheckIOError(f"cannot read {path}: {e}") from e


def _scan_for_check(path: str, cache: dict, order: list, diags: list) -> None:
    """Parse ``path`` and every locally resolvable import, collecting errors.

    ``cache`` maps abspath -> {src, prog, toks, broken}.  ``order`` is the
    discovery (pre-order DFS) order and is also used to rank diagnostics.
    A parse/duplicate-declaration error marks the file ``broken`` so the
    merged checker is skipped (load_program would raise the same error).
    """
    ap = os.path.abspath(path)
    if ap in cache:
        return
    entry = {"src": None, "prog": None, "toks": None, "broken": True}
    cache[ap] = entry
    order.append(ap)
    src = _read_source(ap)
    entry["src"] = src
    try:
        toks = lex(src, ap)
        prog = Parser(toks, ap).parse_program()
    except (LexError, ParseError) as e:
        diags.append(from_parse_error(e, ap))
        return
    entry["prog"] = prog
    entry["toks"] = toks
    try:
        _check_duplicate_decls(prog, ap)
    except CheckError as e:
        diags.append(from_check_error(e, ap, src, toks))
        return
    entry["broken"] = False
    base = os.path.dirname(ap)
    for imp in prog.imports:
        ip = _resolve_import(imp.path, base)
        if ip and ip not in cache:
            _scan_for_check(ip, cache, order, diags)


def _dedupe_diags(diags: list) -> list:
    out = []
    seen = set()
    for d in diags:
        key = (d["file"], d["line"], d["col"], d["endLine"], d["endCol"],
               d["severity"], d["message"], d["code"])
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def check_file(path: str) -> tuple[dict, int]:
    """Check a .we file and return ``(result, exit_code)``.

    ``result`` is JSON-serialisable: ``{"ok": bool, "file": str,
    "diagnostics": [...]}``.  Exit code is 0 when there is no error (warnings
    are fine), 1 when at least one error is reported.  Library files without
    an ``entry`` are valid and report ``ok: true``.
    """
    path = os.path.abspath(path)
    diags: list = []
    cache: dict = {}
    order: list = []
    _scan_for_check(path, cache, order, diags)

    # Unresolved imports are warnings: the program still parses, it just
    # cannot be merged completely, so report them even when other errors exist.
    for p in order:
        entry = cache[p]
        prog = entry["prog"]
        if prog is None:
            continue
        for imp in prog.imports:
            if _resolve_import(imp.path, os.path.dirname(p)) is None:
                diags.append(locate_import(p, entry["src"], imp.path, entry["toks"]))

    broken = any(entry["broken"] for entry in cache.values())
    if not broken:
        try:
            prog = load_program(path)
            Checker(prog, path).run()
        except (LexError, ParseError) as e:  # pragma: no cover - already parsed above
            diags.append(from_parse_error(e, path))
        except CheckError as e:
            diags.append(from_check_error(e, path, cache[path]["src"], cache[path]["toks"]))

    diags = _dedupe_diags(diags)
    rank = {p: i for i, p in enumerate(order)}
    diags.sort(key=lambda d: (rank.get(d["file"], len(order)), d["line"], d["col"],
                              0 if d["severity"] == "error" else 1))
    ok = not any(d["severity"] == "error" for d in diags)
    return {"ok": ok, "file": path, "diagnostics": diags}, 0 if ok else 1

