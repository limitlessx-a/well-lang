"""Lightweight type checker + symbol tables for Well MVP."""
from .ast_nodes import *

BUILTIN = {"bool", "int", "long", "float", "double", "String", "Bytes", "void", "null"}
NUMERIC = {"int", "long", "float", "double"}
ROOTS = {"Vo", "Event", "Controller", "Handler"}


class CheckError(Exception):
    def __init__(self, message, errors=None):
        super().__init__(message)
        # ``errors`` carries every collected semantic message (``check --json``
        # reports them all); ``str(exc)`` stays the first one for build paths.
        self.errors: list[str] = list(errors) if errors else [message]


class Scope(dict):
    """Type environment for one lexical scope.

    ``declared`` holds the names declared directly in this scope so that a
    repeated ``var``/parameter in the same scope is reported (inner scopes may
    still shadow outer ones).
    """

    def __init__(self, base=None):
        super().__init__(base or {})
        self.declared: set[str] = set()


class Checker:
    def __init__(self, prog: Program, filename="<src>"):
        self.p = prog
        self.fn = filename
        self.structs = {}     # name -> {"kind", "fields": [FieldDecl], "base", "name"}
        self.enums = {}       # name -> {"type": "String"|"int", "members": [(n,v)]}
        self.consts = {}      # name -> (TypeRef, Expr)
        self.funcs = {}       # name -> FuncDecl
        self.controllers = {}
        self.handlers = {}
        self.handler_event = {}   # handler -> TypeRef
        self.handler_method = {}  # handler -> method name
        self.namespaces = {}      # namespace -> {func name: MethodDecl}
        self.statics = {}         # static name -> {"fields": {name: FieldDecl}, "funcs": {name: MethodDecl}}
        self.current_namespace = None
        self.cur_ret = "void"
        self.loop_depth = 0
        self.break_depth = 0
        self.cur_pos = None   # (file, line, col) of the statement being checked
        self.expr_depth = 0
        self.stmt_depth = 0
        self.ns_pos = {}      # namespace name -> declaration position
        self.static_pos = {}  # static name -> declaration position
        # Set while checking a type-inferred `var x = [...]` initializer.
        self.allow_container_literal = False

    # ---------- public ----------
    def run(self):
        self.collect()
        # Semantic errors are collected per declaration/method so `check`
        # (and editors) see more than the first mistake in a file.  Build/run
        # paths still surface ``str(CheckError)`` = the first message.
        self.errors = []
        self.attempt(self.check_decl_types)
        for name, h in self.controllers.items():
            self.attempt(self._check_controller, h)
        for name, h in self.handlers.items():
            self.attempt(self._check_handler, name, h)
        for name, f in self.funcs.items():
            self.attempt(self.check_method, f, {}, is_func=True)
        for ns, funcs in self.namespaces.items():
            self.attempt(self._check_namespace, ns, funcs)
        for sname, st in self.statics.items():
            self.attempt(self._check_static, sname, st)
        self.attempt(self._check_entry)
        if self.errors:
            raise CheckError(self.errors[0], self.errors)
        # expose tables
        self.p.structs = self.structs
        self.p.events = {k: v for k, v in self.structs.items() if v["kind"] == "event"}
        self.p.enums = self.enums
        self.p.consts = self.consts
        self.p.funcs = self.funcs
        self.p.namespaces = self.namespaces
        self.p.statics = self.statics
        self.p.controllers = self.controllers
        self.p.handlers = self.handlers
        self.p.handler_event = self.handler_event
        self.p.handler_method = self.handler_method
        return self.p

    def attempt(self, fn, *args, **kwargs):
        """Run one semantic check unit, recording (not raising) its errors."""
        try:
            return fn(*args, **kwargs)
        except CheckError as e:
            errs = getattr(e, "errors", None) or [str(e)]
            for msg in errs:
                if msg not in self.errors:
                    self.errors.append(msg)
            return None

    def _check_controller(self, h):
        for m in h.methods:
            if m.name == "run":
                self.check_method(m, {}, owner=None)

    def _check_handler(self, name, h):
        ev = self.handler_event.get(name)
        if ev is None:
            self.cur_pos = self.pos_of(h)
            self.err(f"handler {name} has no onEvent/handle method")
        for m in h.methods:
            if m.name == self.handler_method[name]:
                self.check_method(m, {"oid": "long"}, owner=name, event_type=ev)

    def _check_namespace(self, ns, funcs):
        old = self.current_namespace
        self.current_namespace = ns
        try:
            for fname, f in funcs.items():
                self.check_method(f, {}, owner=None, is_func=True)
        finally:
            self.current_namespace = old

    def _check_static(self, sname, st):
        for fname, f in st["fields"].items():
            self._check_static_field(sname, fname, f)
        old = self.current_namespace
        self.current_namespace = sname
        try:
            for fname, f in st["funcs"].items():
                self.check_method(f, {}, owner=None, is_func=True)
        finally:
            self.current_namespace = old

    def check_default_value(self, label: str, ty: str, d, literal_msg: str):
        """Validate a struct/static field default value.

        Only literals, consts, container literals and ``new Bytes/Array/Map``
        are allowed: codegen materialises the default once and must not embed
        an arbitrary expression (calls, host reads, ``new <struct>``, ...).
        """
        if isinstance(d, ArrayLit):
            kind, _ = self.container_info(ty)
            if kind != "array":
                self.err(f"{label}: array literal default requires Array<T>")
            self.check_container_literal(d, ty, {})
            return
        if isinstance(d, ObjectLit):
            kind, _ = self.container_info(ty)
            if kind != "map":
                self.err(f"{label}: map literal default requires Map<T>")
            self.check_container_literal(d, ty, {})
            return
        if isinstance(d, New):
            dt = self.check_expr(d, {})
            if not (getattr(d, "bytes_new", False) or getattr(d, "container_new", None)):
                self.err(f"{label}: default new is limited to Bytes/Array/Map")
            self.check_assignable(ty, dt)
            return
        if not self._is_default_literal(d):
            self.err(f"{label}: {literal_msg}")
        vt = self.check_expr(d, {})
        self.check_assignable(ty, vt)

    def _is_default_literal(self, e) -> bool:
        if isinstance(e, (IntLit, FloatLit, StrLit, BoolLit, NullLit, Ident)):
            return True
        return isinstance(e, Unary) and e.op in ("-", "+") \
            and isinstance(e.expr, (IntLit, FloatLit))

    def _check_static_field(self, sname, fname, f):
        self.cur_pos = self.pos_of(f) or self.static_pos.get(sname)
        self.check_type_name(f.type.name)
        self.check_static_field_type(sname, fname, f.type.name, set())
        if f.default is None:
            return
        self.check_default_value(
            f"static {sname}.{fname}", f.type.name, f.default,
            "default value must be a literal/const/container literal")

    def _check_entry(self):
        if self.p.entry and self.p.entry not in self.controllers:
            self.err(f"entry '{self.p.entry}' is not a controller")

    def err(self, msg):
        if self.cur_pos is not None:
            f, line, col = self.cur_pos
            raise CheckError(f"{f}:{line}:{col}: {msg}")
        raise CheckError(f"{self.fn}: {msg}")

    def pos_of(self, node):
        """(file, line, col) of a declaration / field / method, or None."""
        if node is None:
            return None
        if isinstance(node, dict):
            line = node.get("line")
            if line is None:
                return None
            return (node.get("file", self.fn), line, node.get("col", 1))
        line = getattr(node, "line", None)
        if line is None:
            return None
        return (getattr(node, "file", self.fn), line, getattr(node, "col", 1))

    # ---------- declarations ----------
    def collect(self):
        # first pass: names
        for d in self.p.decls:
            self.cur_pos = self.pos_of(d)
            if isinstance(d, EnumDecl):
                self.enums[d.name] = d
            elif isinstance(d, StructDecl):
                pass
            elif isinstance(d, ConstDecl):
                pass
            elif isinstance(d, FuncDecl):
                self.funcs[d.name] = d
            elif isinstance(d, ControllerDecl):
                self.controllers[d.name] = d
                seen_m = set()
                runs = 0
                for m in d.methods:
                    if m.name in seen_m:
                        self.err(f"controller {d.name}: duplicate method {m.name}")
                    seen_m.add(m.name)
                    if m.name != "run":
                        self.err(f"controller {d.name}: unsupported method {m.name} "
                                 f"(a controller only supports run())")
                    runs += 1
                if runs == 0:
                    self.err(f"controller {d.name} must define run()")
            elif isinstance(d, HandlerDecl):
                self.handlers[d.name] = d
                seen_m = set()
                event_methods = 0
                for m in d.methods:
                    if m.name in seen_m:
                        self.err(f"handler {d.name}: duplicate method {m.name}")
                    seen_m.add(m.name)
                    if m.name in ("onEvent", "handle", "on", "handler"):
                        event_methods += 1
                    else:
                        self.err(f"handler {d.name}: unsupported method {m.name} "
                                 f"(a handler only supports its event method)")
                if event_methods > 1:
                    self.err(f"handler {d.name} must define exactly one event method, "
                             f"found {event_methods}")
            elif isinstance(d, NamespaceDecl):
                if d.name in self.namespaces:
                    self.err(f"duplicate namespace {d.name}")
                table = {}
                for f in d.funcs:
                    if f.name in table:
                        self.err(f"namespace {d.name}: duplicate function {f.name}")
                    table[f.name] = f
                self.namespaces[d.name] = table
                self.ns_pos[d.name] = self.pos_of(d)
            elif isinstance(d, StaticDecl):
                if d.name in self.statics or d.name in self.namespaces:
                    self.err(f"duplicate type name {d.name}")
                fields = {}
                for f in d.fields:
                    if f.name in fields:
                        self.err(f"static {d.name}: duplicate field {f.name}")
                    fields[f.name] = f
                table = {}
                for f in d.funcs:
                    if f.name in table:
                        self.err(f"static {d.name}: duplicate function {f.name}")
                    table[f.name] = f
                self.statics[d.name] = {"fields": fields, "funcs": table}
                self.static_pos[d.name] = self.pos_of(d)
        # enums: normalize members (auto values, String default = member name)
        for name, d in self.enums.items():
            self.cur_pos = self.pos_of(d)
            kind = None
            for mn, mv in d.members:
                if isinstance(mv, str):
                    if kind == "int":
                        self.err(f"enum {name} mixes string/int values")
                    kind = "String"
                elif isinstance(mv, bool):
                    kind = "int"
                elif mv is not None:
                    kind = "int"
            base = getattr(d, "base", None)
            if base is not None:
                bname = base.name
                if bname not in ("int", "long", "String"):
                    self.err(f"enum {name}: unsupported base type {bname} "
                             f"(only int/long/String enums are supported)")
                if d.members:
                    if bname == "String" and kind != "String":
                        self.err(f"enum {name} is declared String but has int values")
                    if bname in ("int", "long") and kind == "String":
                        self.err(f"enum {name} is declared {bname} but has String values")
            if kind is None:
                kind = "String" if (base is not None and base.name == "String") else "int"
            normalized = []
            auto = 0
            seen_vals = set()
            for mn, mv in d.members:
                if kind == "String":
                    if mv is not None and not isinstance(mv, str):
                        self.err(f"enum {name}.{mn} must be a string")
                    normalized.append((mn, mv if isinstance(mv, str) else mn))
                else:
                    if mv is None:
                        val = auto
                    else:
                        val = int(mv)
                    if val in seen_vals:
                        self.err(f"enum {name}: duplicate value {val} (member {mn})")
                    seen_vals.add(val)
                    normalized.append((mn, val))
                    auto = val + 1
            self.enums[name] = {"type": kind, "members": normalized}
        # structs/events
        for d in self.p.decls:
            if isinstance(d, StructDecl):
                self.cur_pos = self.pos_of(d)
                self.structs[d.name] = {
                    "kind": "event" if d.is_event else "struct",
                    "fields": self.flatten_fields(d.name, d, set()),
                    "base": d.base,
                    "name": d.name,
                    "file": getattr(d, "file", self.fn),
                    "line": getattr(d, "line", None),
                    "col": getattr(d, "col", 1),
                }
        # consts
        for d in self.p.decls:
            if isinstance(d, ConstDecl):
                self.consts[d.name] = d
        # handlers: find event method
        for name, h in self.handlers.items():
            self.cur_pos = self.pos_of(h)
            for m in h.methods:
                if m.name in ("onEvent", "handle", "on", "handler"):
                    if len(m.params) != 1:
                        self.err(f"handler {name}.{m.name} must take exactly one event param")
                    pname, ptype = m.params[0]
                    if ptype.name not in self.structs or self.structs[ptype.name]["kind"] != "event":
                        self.err(f"handler {name}.{m.name} parameter must be a concrete event type, not {ptype.name}")
                    self.handler_event[name] = ptype
                    self.handler_method[name] = m.name
                    break
            if name not in self.handler_event:
                self.err(f"handler {name} must define onEvent(e:SomeEvent)")
        self.cur_pos = None

    def flatten_fields(self, name, d, seen):
        if name in seen:
            self.err(f"cyclic struct inheritance: {name}")
        seen.add(name)
        out = []
        if d.base and d.base not in ROOTS:
            base = None
            for bd in self.p.decls:
                if isinstance(bd, StructDecl) and bd.name == d.base:
                    base = bd
                    break
            if base is None:
                self.err(f"unknown base type {d.base} for {name}")
            out.extend(self.flatten_fields(base.name, base, seen))
        out.extend(d.fields)
        # duplicate field check
        names = set()
        for f in out:
            if f.name in names:
                self.err(f"duplicate field {f.name} in {name}")
            names.add(f.name)
        return out

    # ---------- methods ----------
    def check_method(self, m, outer, owner=None, event_type=None, is_func=False):
        old_pos = self.cur_pos
        self.cur_pos = self.pos_of(m) or old_pos
        try:
            self._check_method(m, outer, owner=owner, event_type=event_type, is_func=is_func)
        finally:
            self.cur_pos = old_pos

    def _check_method(self, m, outer, owner=None, event_type=None, is_func=False):
        env = Scope(outer)
        seen = set()
        for pname, ptype in m.params:
            if pname in seen:
                self.err(f"{m.name}: duplicate parameter {pname}")
            seen.add(pname)
            self.check_type_name(ptype.name)
            env[pname] = ptype.name
            env.declared.add(pname)
        if m.ret:
            self.check_type_name(m.ret.name, allow_void=True)
        ret = m.ret.name if m.ret else "void"
        old_ret, old_loop, old_break = self.cur_ret, self.loop_depth, self.break_depth
        self.cur_ret = ret
        self.loop_depth = 0
        self.break_depth = 0
        try:
            self.check_block(m.body, env)
            if ret != "void" and not self.block_returns(m.body):
                self.err(f"{m.name}: not all paths return a value")
        finally:
            self.cur_ret = old_ret
            self.loop_depth = old_loop
            self.break_depth = old_break

    def check_static_field_type(self, sname, fname, ty, seen):
        """Events (directly or reachable through structs/containers) are not
        allowed in static fields: emit mutates an event's oid after storage."""
        if ty in self.structs:
            if self.structs[ty]["kind"] == "event":
                self.err(f"static {sname}.{fname}: event type {ty} is not allowed in static fields")
            if ty in seen:
                return
            seen.add(ty)
            for sf in self.structs[ty]["fields"]:
                self.check_static_field_type(sname, fname, sf.type.name, seen)
            return
        kind, et = self.container_info(ty)
        if kind:
            self.check_static_field_type(sname, fname, et, seen)

    def check_decl_types(self):
        for name, st in self.structs.items():
            self.cur_pos = self.pos_of(st)
            for f in st["fields"]:
                self.cur_pos = self.pos_of(f) or self.pos_of(st)
                if f.type.name == "void":
                    self.err(f"{name}.{f.name}: a field cannot be void")
                self.check_type_name(f.type.name)
                if f.default is not None:
                    self.check_default_value(
                        f"{name}.{f.name}", f.type.name, f.default,
                        "default value must be a compile-time literal/const/container literal")
        for name, f in self.funcs.items():
            self.cur_pos = self.pos_of(f)
            for pn, pt in f.params:
                self.check_type_name(pt.name)
            if f.ret:
                self.check_type_name(f.ret.name, allow_void=True)
        for name, c in self.controllers.items():
            self.cur_pos = self.pos_of(c)
            for m in c.methods:
                self.cur_pos = self.pos_of(m) or self.pos_of(c)
                for pn, pt in m.params:
                    self.check_type_name(pt.name)
                if m.ret:
                    self.check_type_name(m.ret.name, allow_void=True)
        for name, h in self.handlers.items():
            self.cur_pos = self.pos_of(h)
            for m in h.methods:
                self.cur_pos = self.pos_of(m) or self.pos_of(h)
                for pn, pt in m.params:
                    self.check_type_name(pt.name)
                if m.ret:
                    self.check_type_name(m.ret.name, allow_void=True)
        for ns, funcs in self.namespaces.items():
            self.cur_pos = self.ns_pos.get(ns)
            for fname, f in funcs.items():
                self.cur_pos = self.pos_of(f) or self.ns_pos.get(ns)
                for pn, pt in f.params:
                    self.check_type_name(pt.name)
                if f.ret:
                    self.check_type_name(f.ret.name, allow_void=True)
        for sname, st in self.statics.items():
            self.cur_pos = self.static_pos.get(sname)
            for fname, f in st["funcs"].items():
                self.cur_pos = self.pos_of(f) or self.static_pos.get(sname)
                for pn, pt in f.params:
                    self.check_type_name(pt.name)
                if f.ret:
                    self.check_type_name(f.ret.name, allow_void=True)
        for name, d in self.consts.items():
            self.cur_pos = self.pos_of(d)
            self.check_type_name(d.type.name)
            vt = self.check_expr(d.value, {})
            # ``const`` is emitted as a C macro and expanded at every use
            # site, so its initializer must be a pure compile-time constant
            # (no ``new``/calls/host reads, no String concatenation which
            # would allocate a fresh object per use).
            if not self.is_pure_const(d.value):
                self.err(
                    f"const {name}: initializer must be a compile-time constant "
                    f"(literal, enum member, another const, or arithmetic on them)")
            self.check_assignable(d.type.name, vt)
        self.cur_pos = None

    def is_pure_const(self, e) -> bool:
        """True when ``e`` can be expanded as a C macro with no side effects."""
        if isinstance(e, (IntLit, FloatLit, BoolLit, NullLit)):
            return True
        if isinstance(e, StrLit):
            return True
        if isinstance(e, Ident):
            return e.name in self.consts
        if isinstance(e, Member):
            return getattr(e, "is_enum", False)
        if isinstance(e, Unary):
            return e.op in ("+", "-", "!", "~") and self.is_pure_const(e.expr)
        if isinstance(e, Binary):
            lt = getattr(e.left, "ty", None)
            rt = getattr(e.right, "ty", None)
            if lt == "String" or rt == "String":
                return False
            return self.is_pure_const(e.left) and self.is_pure_const(e.right)
        return False

    def block_returns(self, b: Block) -> bool:
        return bool(b.stmts) and self.stmt_returns(b.stmts[-1])

    def stmt_returns(self, s) -> bool:
        if isinstance(s, Return):
            return True
        if isinstance(s, Block):
            return self.block_returns(s)
        if isinstance(s, If):
            return s.els is not None and self.stmt_returns(s.then) and self.stmt_returns(s.els)
        if isinstance(s, Switch):
            if s.default is None:
                return False
            return all(self.stmt_returns(b) for _, b in s.cases) and self.stmt_returns(s.default)
        if isinstance(s, While):
            # `while (true) {}` never falls through; a reachable break means it can.
            return self.is_infinite_cond(s.cond) and not self.loop_body_breaks(s.body)
        if isinstance(s, For):
            # `for (;;) {}` / `for (; true;) {}` never falls through.
            return self.is_infinite_cond(s.cond) and not self.loop_body_breaks(s.body)
        return False

    def is_const_case(self, e) -> bool:
        if isinstance(e, (IntLit, BoolLit, StrLit)):
            return True
        if isinstance(e, Unary) and e.op == "-" and isinstance(e.expr, IntLit):
            return True
        if isinstance(e, Ident) and e.name in self.consts:
            return True
        if isinstance(e, Member) and getattr(e, "is_enum", False):
            return True
        return False

    def event_fields(self, ev: str) -> dict:
        """Field name -> type for a concrete event type."""
        st = self.structs.get(ev)
        if st is None or st.get("kind") != "event":
            self.err(f"{ev} is not a concrete event type")
        return {f.name: f.type.name for f in st["fields"]}

    def check_block(self, b: Block, env):
        for s in b.stmts:
            self.check_stmt(s, env)

    # ---------- statements ----------
    def check_stmt(self, s, env):
        old = self.cur_pos
        line = getattr(s, "line", None)
        if line is not None:
            self.cur_pos = (getattr(s, "file", self.fn), line, getattr(s, "col", 1))
        self.stmt_depth += 1
        if self.stmt_depth > 150:
            self.err("statement nesting too deep (max 150)")
        try:
            self._check_stmt(s, env)
        finally:
            self.stmt_depth -= 1
            self.cur_pos = old

    def _check_stmt(self, s, env):
        if isinstance(s, VarDecl):
            ty = None
            if s.type:
                ty = s.type.name
                self.check_type_name(ty)
            if ty and isinstance(s.init, (ArrayLit, ObjectLit)):
                self.check_container_literal(s.init, ty, env)
                et = ty
            else:
                if ty is None and isinstance(s.init, (ArrayLit, ObjectLit)):
                    # `var xs = [1, 2];` infers Array<int> from the elements.
                    self.allow_container_literal = True
                    try:
                        et = self.check_expr(s.init, env)
                    finally:
                        self.allow_container_literal = False
                else:
                    et = self.check_expr(s.init, env)
                if ty:
                    # An annotated declaration must agree with its initializer
                    # (the annotation used to be silently discarded).
                    self.check_assignable(ty, et)
            if ty is None:
                ty = et
                if ty in ("null", None):
                    self.err(f"cannot infer type of var {s.name} (add annotation)")
                if ty == "void":
                    self.err(f"cannot use a void expression as the initializer of var {s.name}")
            names = getattr(env, "declared", None)
            if names is not None:
                if s.name in names:
                    self.err(f"duplicate variable {s.name} in the same scope")
                names.add(s.name)
            env[s.name] = ty
            s.ty = ty
        elif isinstance(s, Assign):
            tty = self.target_type(s.target, env)
            if isinstance(s.value, (ArrayLit, ObjectLit)):
                self.check_container_literal(s.value, tty, env)
            else:
                vty = self.check_expr(s.value, env)
                self.check_assignable(tty, vty)
        elif isinstance(s, ExprStmt):
            self.check_expr(s.expr, env)
        elif isinstance(s, If):
            self.check_cond(s.cond, env)
            self.check_stmt(s.then, Scope(env))
            if s.els:
                self.check_stmt(s.els, Scope(env))
        elif isinstance(s, While):
            self.check_cond(s.cond, env)
            self.loop_depth += 1
            self.break_depth += 1
            self.check_stmt(s.body, Scope(env))
            self.break_depth -= 1
            self.loop_depth -= 1
        elif isinstance(s, For):
            fenv = Scope(env)
            if s.init:
                self.check_stmt(s.init, fenv)
            if s.cond:
                self.check_cond(s.cond, fenv)
            self.loop_depth += 1
            self.break_depth += 1
            self.check_stmt(s.body, Scope(fenv))
            self.break_depth -= 1
            self.loop_depth -= 1
            if s.step:
                self.check_stmt(s.step, fenv)
        elif isinstance(s, ForEach):
            it = self.check_expr(s.iter, env)
            kind, et = self.container_info(it)
            fenv = Scope(env)
            if s.kind == "of":
                if kind not in ("array", "linked"):
                    self.err(f"for-of requires Array/Linked, got {it}")
                fenv[s.var] = et
            elif s.kind == "in":
                if kind != "map":
                    self.err(f"for-in requires Map, got {it}")
                fenv[s.var] = "String"
            else:
                self.err(f"unknown for-each kind {s.kind}")
            self.loop_depth += 1
            self.break_depth += 1
            self.check_stmt(s.body, fenv)
            self.break_depth -= 1
            self.loop_depth -= 1
        elif isinstance(s, Return):
            if s.value is None:
                if self.cur_ret != "void":
                    self.err(f"return without a value in a {self.cur_ret} function")
            else:
                vt = self.check_expr(s.value, env)
                if self.cur_ret == "void":
                    self.err("return with a value in a void function")
                self.check_assignable(self.cur_ret, vt)
        elif isinstance(s, Switch):
            ct = self.check_expr(s.expr, env)
            if ct not in ("int", "long", "String"):
                self.err("switch expression must be int/long/String")
            seen = set()
            self.break_depth += 1
            for val, body in s.cases:
                vt = self.check_expr(val, env)
                if ct == "String":
                    if vt != "String":
                        self.err("String switch case value must be a string")
                    if not self.is_const_case(val):
                        self.err("case value must be a constant")
                    sv = self.case_string_value(val)
                    if sv is None:
                        self.err("String switch case value must be a string literal or enum member")
                    k = ("str", sv)
                else:
                    self.check_assignable(ct, vt)
                    if not self.is_const_case(val):
                        self.err("case value must be a constant")
                    iv = self.case_int_value(val)
                    k = ("int", iv) if iv is not None else None
                if k is not None:
                    if k in seen:
                        self.err("duplicate case value")
                    seen.add(k)
                self.check_block(body, Scope(env))
            if s.default:
                self.check_block(s.default, Scope(env))
            self.break_depth -= 1
        elif isinstance(s, Block):
            self.check_block(s, Scope(env))
        elif isinstance(s, (Break, Continue)):
            if isinstance(s, Break):
                if self.break_depth <= 0:
                    self.err("break outside of a loop/switch")
            else:
                if self.loop_depth <= 0:
                    self.err("continue outside of a loop")
        else:
            self.err(f"unknown statement {type(s).__name__}")

    def check_cond(self, e, env):
        t = self.check_expr(e, env)
        if t not in ("bool", "int", "long"):
            self.err(f"condition must be bool/int/long, got {t}")

    def target_type(self, tgt, env):
        if isinstance(tgt, Ident):
            if tgt.name not in env:
                self.err(f"assignment to undeclared variable {tgt.name}")
            return env[tgt.name]
        if isinstance(tgt, Member) and isinstance(tgt.obj, Ident) and tgt.obj.name in self.statics:
            st = self.statics[tgt.obj.name]
            if tgt.name not in st["fields"]:
                self.err(f"static {tgt.obj.name} has no field {tgt.name}")
            return st["fields"][tgt.name].type.name
        if isinstance(tgt, Member):
            ot = self.check_expr(tgt.obj, env)
            ft = self.field_type(ot, tgt.name)
            if ft is None:
                self.err(f"unknown field {tgt.name} on {ot}")
            return ft
        if isinstance(tgt, Index):
            ot = self.check_expr(tgt.obj, env)
            kind, et = self.container_info(ot)
            it = self.check_expr(tgt.index, env)
            if kind == "array":
                if it not in ("int", "long"):
                    self.err("array index must be int/long")
                return et
            if kind == "map":
                if it != "String":
                    self.err("Map key must be String")
                return et
            self.err(f"index assignment requires Array/Map, got {ot}")
        self.err("invalid assignment target")

    def check_container_literal(self, e, expected, env):
        kind, et = self.container_info(expected)
        if kind == "array":
            if not isinstance(e, ArrayLit):
                self.err(f"expected Array literal for {expected}")
            for it in e.items:
                t = self.check_expr(it, env)
                self.check_assignable(et, t)
            e.ty = expected
            return
        if kind == "map":
            if not isinstance(e, ObjectLit):
                self.err(f"expected Map literal for {expected}")
            for k, v in e.pairs:
                t = self.check_expr(v, env)
                self.check_assignable(et, t)
            e.ty = expected
            return
        self.err(f"expected a container type, got {expected}")

    # ---------- expressions ----------
    def check_expr(self, e, env):
        self.expr_depth += 1
        if self.expr_depth > 200:
            self.err("expression nesting too deep (max 200)")
        try:
            return self._check_expr(e, env)
        finally:
            self.expr_depth -= 1

    def _check_expr(self, e, env):
        if isinstance(e, (ArrayLit, ObjectLit)) and not self.allow_container_literal:
            # Only var declarations / assignments / static defaults materialize
            # container literals (they need an owned container); every other
            # context (return, ternary, operand, print, ...) is rejected here
            # instead of crashing codegen later.
            self.err("container literal is only allowed as a var/assignment initializer; "
                     "assign it to a variable first")
        if isinstance(e, IntLit):
            if e.value > 9223372036854775807 or e.value < -9223372036854775808:
                self.err("integer literal out of 64-bit range")
            e.ty = "long" if (e.value > 2147483647 or e.value < -2147483648) else "int"
        elif isinstance(e, FloatLit):
            e.ty = "double"
        elif isinstance(e, StrLit):
            e.ty = "String"
        elif isinstance(e, BoolLit):
            e.ty = "bool"
        elif isinstance(e, NullLit):
            e.ty = "null"
        elif isinstance(e, Ident):
            if e.name in env:
                e.ty = env[e.name]
            elif e.name in ("true", "false"):
                e.ty = "bool"
            elif e.name in self.consts:
                e.ty = self.consts[e.name].type.name
            elif e.name in self.enums:
                self.err(f"enum type {e.name} used as value")
            elif e.name in self.funcs:
                self.err(f"function {e.name} used as value")
            else:
                self.err(f"undefined identifier {e.name}")
        elif isinstance(e, ArrayLit):
            if not e.items:
                self.err("empty array literal needs a type annotation")
            et = None
            for it in e.items:
                t = self.check_expr(it, env)
                if t == "void":
                    self.err("cannot use a void value in an array literal")
                if t == "null":
                    continue
                if et is None:
                    et = t
                elif et != t:
                    merged = self.merge_numeric(et, t)
                    if merged is None:
                        self.err(f"array literal elements must share one type: {et} vs {t}")
                    et = merged
            if et is None:
                self.err("cannot infer element type of array literal")
            e.ty = f"Array<{et}>"
        elif isinstance(e, ObjectLit):
            if not e.pairs:
                self.err("empty object literal needs a type annotation")
            vt = None
            for k, v in e.pairs:
                t = self.check_expr(v, env)
                if t == "void":
                    self.err("cannot use a void value in an object literal")
                if t == "null":
                    continue
                if vt is None:
                    vt = t
                elif vt != t:
                    merged = self.merge_numeric(vt, t)
                    if merged is None:
                        self.err(f"object literal values must share one type: {vt} vs {t}")
                    vt = merged
            if vt is None:
                self.err("cannot infer value type of object literal")
            e.ty = f"Map<{vt}>"
        elif isinstance(e, Index):
            ot = self.check_expr(e.obj, env)
            if ot == "String":
                it = self.check_expr(e.index, env)
                if it not in ("int", "long"):
                    self.err("String index must be int/long")
                e.ty = "int"
                e.str_index = True
                e.str_obj = e.obj
                return e.ty
            if ot == "Bytes":
                it = self.check_expr(e.index, env)
                if it not in ("int", "long"):
                    self.err("Bytes index must be int/long")
                e.ty = "int"
                e.bytes_index = True
                return e.ty
            kind, et = self.container_info(ot)
            if kind == "array":
                it = self.check_expr(e.index, env)
                if it not in ("int", "long"):
                    self.err("array index must be int/long")
            elif kind == "map":
                it = self.check_expr(e.index, env)
                if it != "String":
                    self.err("Map key must be String")
            else:
                self.err(f"index access requires Array/Map, got {ot}")
            e.ty = et
            e.elem_ty = et
        elif isinstance(e, Member):
            # StaticName.field
            if isinstance(e.obj, Ident) and e.obj.name in self.statics:
                st = self.statics[e.obj.name]
                if e.name not in st["fields"]:
                    self.err(f"static {e.obj.name} has no field {e.name}")
                e.ty = st["fields"][e.name].type.name
                e.static_ref = (e.obj.name, e.name)
                return e.ty
            # Enum.Member
            if isinstance(e.obj, Ident) and e.obj.name in self.enums:
                en = self.enums[e.obj.name]
                names = [n for n, _ in en["members"]]
                if e.name not in names:
                    self.err(f"enum {e.obj.name} has no member {e.name}")
                e.ty = en["type"]
                e.is_enum = True
            else:
                ot = self.check_expr(e.obj, env)
                ft = self.field_type(ot, e.name)
                if ft is None and ot == "String" and e.name == "length":
                    e.ty = "int"
                    e.str_len = True
                    return e.ty
                if ft is None and ot == "Bytes" and e.name == "length":
                    e.ty = "long"
                    e.container_len = "bytes"
                    return e.ty
                kind, _ = self.container_info(ot)
                if ft is None and kind == "array" and e.name == "length":
                    e.ty = "long"
                    e.container_len = "array"
                    return e.ty
                if ft is None and kind == "map" and e.name == "size":
                    e.ty = "long"
                    e.container_len = "map"
                    return e.ty
                if ft is None and kind == "linked" and e.name == "length":
                    e.ty = "long"
                    e.container_len = "linked"
                    return e.ty
                if ft is None:
                    self.err(f"unknown field {e.name} on {ot}")
                e.ty = ft
        elif isinstance(e, Unary):
            t = self.check_expr(e.expr, env)
            if e.op == "!":
                if t not in ("bool", "int", "long"):
                    self.err(f"operator ! requires a bool operand, got {t}")
                e.ty = "bool"
            elif e.op in ("-", "+", "~"):
                if t not in NUMERIC:
                    self.err(f"operator {e.op} requires a numeric operand, got {t}")
                if e.op == "~" and t in ("double", "float"):
                    self.err("operator ~ requires an integer operand")
                e.ty = t
            else:
                self.err(f"unknown unary operator {e.op}")
        elif isinstance(e, Binary):
            self.check_binary(e, env)
        elif isinstance(e, New):
            ty = e.type.name
            self.check_type_name(ty)
            if ty in self.statics:
                self.err(f"static {ty} cannot be instantiated")
            if ty == "Bytes":
                if e.args or e.init:
                    self.err("new Bytes() takes no arguments")
                e.ty = "Bytes"
                e.bytes_new = True
                return e.ty
            kind, _ = self.container_info(ty)
            if kind:
                if e.args or e.init:
                    self.err(f"new {ty}() takes no arguments")
                e.ty = ty
                e.container_new = kind
                return e.ty
            if ty not in self.structs:
                self.err(f"cannot construct unknown type {ty}")
            is_event = self.structs[ty]["kind"] == "event"
            if is_event:
                if len(e.args) > 1:
                    self.err("event constructor takes only targetOid")
                if len(e.args) == 1:
                    at = self.check_expr(e.args[0], env)
                    if at not in ("int", "long"):
                        self.err("event target oid must be int/long")
            elif e.args:
                self.err("struct constructor takes no arguments")
            e.ty = ty
            names = {f.name for f in self.structs[ty]["fields"]}
            for fn, v in e.init:
                if fn not in names:
                    self.err(f"new {ty}: unknown field {fn}")
                vt = self.check_expr(v, env)
                ft = self.field_type(ty, fn)
                self.check_assignable(ft, vt)
        elif isinstance(e, Call):
            self.check_call(e, env)
        else:
            self.err(f"unknown expression {type(e).__name__}")
        return getattr(e, "ty", None)

    def check_binary(self, e: Binary, env):
        if e.op == "?:":
            ct = self.check_expr(e.left, env)
            if ct not in ("bool", "int", "long"):
                self.err(f"ternary condition must be bool/int/long, got {ct}")
            inner = e.right  # Binary(":", a, b)
            if isinstance(inner, Binary) and inner.op == ":":
                ta = self.check_expr(inner.left, env)
                tb = self.check_expr(inner.right, env)
                if ta == tb:
                    e.ty = ta
                elif "null" in (ta, tb):
                    other = tb if ta == "null" else ta
                    if not self.is_ref_type(other):
                        self.err(f"ternary: cannot mix null with {other}")
                    e.ty = other
                elif ta in NUMERIC and tb in NUMERIC:
                    rank = {"int": 0, "long": 1, "double": 2, "float": 2}
                    e.ty = ta if rank.get(ta, 0) >= rank.get(tb, 0) else tb
                else:
                    self.err(f"ternary branches have incompatible types {ta} and {tb}")
            else:
                e.ty = self.check_expr(inner, env)
            return
        if e.op == ":":
            ta = self.check_expr(e.left, env)
            tb = self.check_expr(e.right, env)
            e.ty = ta if ta != "null" else tb
            return
        lt = self.check_expr(e.left, env)
        rt = self.check_expr(e.right, env)
        if e.op == "+" and ("String" in (lt, rt)):
            if lt != "String" or rt != "String":
                self.err("String concatenation requires both operands to be String")
            e.ty = "String"
        elif e.op in ("==", "!="):
            if "null" in (lt, rt):
                if lt == rt:
                    pass  # null == null
                else:
                    other = rt if lt == "null" else lt
                    if not self.is_ref_type(other):
                        self.err(f"cannot compare {other} with null")
            elif lt != rt and not (lt in NUMERIC and rt in NUMERIC):
                self.err(f"cannot compare {lt} with {rt}")
            e.ty = "bool"
        elif e.op in ("<", "<=", ">", ">="):
            if lt not in NUMERIC or rt not in NUMERIC:
                self.err(f"operator {e.op} requires numeric operands, got {lt} and {rt}")
            e.ty = "bool"
        elif e.op in ("&&", "||"):
            if lt not in ("bool", "int", "long") or rt not in ("bool", "int", "long"):
                self.err(f"operator {e.op} requires bool operands, got {lt} and {rt}")
            e.ty = "bool"
        elif e.op == "??":
            self.err("operator ?? is not implemented; write `x == null ? y : x` instead")
        elif e.op == "[]":
            e.ty = "int"
        elif e.op in ("+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>"):
            if lt not in NUMERIC or rt not in NUMERIC:
                self.err(f"operator {e.op} requires numeric operands, got {lt} and {rt}")
            if e.op == "%" and (lt in ("double", "float") or rt in ("double", "float")):
                self.err("operator % requires integer operands")
            if e.op in ("&", "|", "^", "<<", ">>"):
                e.ty = "int"
            elif lt in ("double", "float") or rt in ("double", "float"):
                e.ty = "double"
            elif lt == "long" or rt == "long":
                e.ty = "long"
            else:
                e.ty = "int"
            if e.op in ("/", "%") and isinstance(e.right, IntLit) and e.right.value == 0:
                self.err("division by zero" if e.op == "/" else "modulo by zero")
            if e.ty in ("int", "long"):
                v = self.fold_int(e)
                if v is not None:
                    lo, hi = (-2147483648, 2147483647) if e.ty == "int" else (-9223372036854775808, 9223372036854775807)
                    if v < lo or v > hi:
                        self.err(f"integer constant overflow ({e.ty})")
        else:
            e.ty = lt

    def fold_int(self, e, depth=0):
        """Fold integer-literal-only expressions (used for compile-time range checks)."""
        if depth > 200:
            return None
        if isinstance(e, IntLit):
            return e.value
        if isinstance(e, Unary) and e.op == "-":
            v = self.fold_int(e.expr, depth + 1)
            return -v if v is not None else None
        if isinstance(e, Binary) and e.op in ("+", "-", "*", "/", "%"):
            a = self.fold_int(e.left, depth + 1)
            b = self.fold_int(e.right, depth + 1)
            if a is None or b is None:
                return None
            if e.op in ("/", "%") and b == 0:
                return None
            if e.op == "+":
                return a + b
            if e.op == "-":
                return a - b
            if e.op == "*":
                return a * b
            q = abs(a) // abs(b)
            if (a < 0) != (b < 0):
                q = -q
            return q if e.op == "/" else a - q * b
        return None

    def merge_numeric(self, a: str, b: str):
        """Widened type when both are numeric, else None (literal inference)."""
        ar = self.numeric_rank(a)
        br = self.numeric_rank(b)
        if ar < 0 or br < 0:
            return None
        rank = max(ar, br)
        return "double" if rank >= 2 else ("long" if rank == 1 else "int")

    def case_int_value(self, e, depth=0):
        """Compile-time integer value of a switch case expression, or None."""
        if depth > 32:
            return None
        if isinstance(e, IntLit):
            return e.value
        if isinstance(e, Unary) and e.op == "-":
            v = self.case_int_value(e.expr, depth + 1)
            return -v if v is not None else None
        if isinstance(e, Ident) and e.name in self.consts:
            return self.case_int_value(self.consts[e.name].value, depth + 1)
        if isinstance(e, Member) and getattr(e, "is_enum", False) \
                and isinstance(e.obj, Ident) and e.obj.name in self.enums:
            en = self.enums[e.obj.name]
            if en["type"] == "int":
                for mn, mv in en["members"]:
                    if mn == e.name:
                        return mv
            return None
        if isinstance(e, Binary) and e.op in ("+", "-", "*", "/", "%"):
            a = self.case_int_value(e.left, depth + 1)
            b = self.case_int_value(e.right, depth + 1)
            if a is None or b is None:
                return None
            if e.op == "+":
                return a + b
            if e.op == "-":
                return a - b
            if e.op == "*":
                return a * b
            if b == 0:
                return None
            q = abs(a) // abs(b)
            if (a < 0) != (b < 0):
                q = -q
            return q if e.op == "/" else a - q * b
        return None

    def case_string_value(self, e):
        """Compile-time String value of a switch case expression, or None."""
        if isinstance(e, StrLit):
            return e.value
        if isinstance(e, Ident) and e.name in self.consts:
            c = self.consts[e.name]
            if c.type.name == "String":
                return self.case_string_value(c.value)
            return None
        if isinstance(e, Member) and getattr(e, "is_enum", False) \
                and isinstance(e.obj, Ident) and e.obj.name in self.enums:
            en = self.enums[e.obj.name]
            if en["type"] == "String":
                for mn, mv in en["members"]:
                    if mn == e.name:
                        return mv
            return None
        return None

    def is_infinite_cond(self, cond) -> bool:
        """True for ``for(;;)`` / ``while(true)`` / ``while(1)`` style loops."""
        if cond is None:
            return True
        if isinstance(cond, BoolLit):
            return cond.value
        if isinstance(cond, IntLit):
            return cond.value != 0
        return False

    def loop_body_breaks(self, s) -> bool:
        """Does the loop body contain a ``break`` that exits *this* loop?"""
        if isinstance(s, Break):
            return True
        if isinstance(s, (While, For, ForEach, Switch)):
            # break inside a nested loop/switch targets the inner construct
            return False
        if isinstance(s, Block):
            return any(self.loop_body_breaks(x) for x in s.stmts)
        if isinstance(s, If):
            return self.loop_body_breaks(s.then) or (
                s.els is not None and self.loop_body_breaks(s.els))
        return False

    def is_event_subtype(self, child: str, base: str) -> bool:
        """True when `child` is `base` or inherits it (event/struct field flattening)."""
        cur = child
        seen = set()
        while cur and cur not in ("Event", "Vo"):
            if cur == base:
                return True
            if cur in seen:
                return False
            seen.add(cur)
            d = self.structs.get(cur)
            if not d:
                return False
            cur = d.get("base")
        return cur == base

    def check_call(self, e: Call, env):
        for a in e.args:
            if isinstance(a, (ArrayLit, ObjectLit)):
                self.err("container literal cannot be used as a call argument; assign it to a variable first")
        # EnumName.fromInt / EnumName.toString
        if isinstance(e.func, Member) and isinstance(e.func.obj, Ident) and e.func.obj.name in self.enums:
            en = self.enums[e.func.obj.name]
            ename = e.func.obj.name
            m = e.func.name
            if m == "fromInt":
                if len(e.args) != 1:
                    self.err(f"{ename}.fromInt(value) expects 1 argument")
                at = self.check_expr(e.args[0], env)
                if at not in ("int", "long"):
                    self.err(f"{ename}.fromInt: value must be int/long")
                if en["type"] != "int":
                    self.err(f"{ename}.fromInt: only int enums are supported")
                e.ty = "int"
                e.enum_op = "from_int"
                e.enum_name = ename
                return
            if m == "toString":
                if len(e.args) != 1:
                    self.err(f"{ename}.toString(value) expects 1 argument")
                at = self.check_expr(e.args[0], env)
                self.check_assignable(en["type"], at)
                e.ty = "String"
                e.enum_op = "to_string"
                e.enum_name = ename
                return
            self.err(f"unknown enum method {m}")
        # NamespaceName.func(...)
        if isinstance(e.func, Member) and isinstance(e.func.obj, Ident) and            (e.func.obj.name in self.namespaces or e.func.obj.name in self.statics):
            ns = e.func.obj.name
            table = self.namespaces.get(ns) or self.statics[ns]["funcs"]
            fname = e.func.name
            if fname not in table:
                self.err(f"namespace {ns} has no function {fname}")
            f = table[fname]
            if len(e.args) != len(f.params):
                self.err(f"{ns}.{fname} expects {len(f.params)} arguments, got {len(e.args)}")
            for i, a in enumerate(e.args):
                at = self.check_expr(a, env)
                self.check_assignable(f.params[i][1].name, at)
            e.ty = f.ret.name if f.ret else "void"
            e.call_ns = (ns, fname)
            return
        # File.*
        if isinstance(e.func, Member) and isinstance(e.func.obj, Ident) and e.func.obj.name == "File":
            name = e.func.name
            if name == "read":
                if len(e.args) != 1:
                    self.err("File.read(path) expects 1 argument")
                if self.check_expr(e.args[0], env) != "String":
                    self.err("File.read: path must be String")
                e.ty = "String"
                e.builtin = "file_read"
                return
            if name == "write":
                if len(e.args) != 2:
                    self.err("File.write(path, text) expects 2 arguments")
                if self.check_expr(e.args[0], env) != "String" or \
                   self.check_expr(e.args[1], env) != "String":
                    self.err("File.write: path/text must be String")
                e.ty = "void"
                e.builtin = "file_write"
                return
            if name == "exists":
                if len(e.args) != 1:
                    self.err("File.exists(path) expects 1 argument")
                if self.check_expr(e.args[0], env) != "String":
                    self.err("File.exists: path must be String")
                e.ty = "bool"
                e.builtin = "file_exists"
                return
            if name == "remove":
                if len(e.args) != 1:
                    self.err("File.remove(path) expects 1 argument")
                if self.check_expr(e.args[0], env) != "String":
                    self.err("File.remove: path must be String")
                e.ty = "void"
                e.builtin = "file_remove"
                return
            self.err(f"unknown File method {name}")
        # Host.*
        if isinstance(e.func, Member) and isinstance(e.func.obj, Ident) and e.func.obj.name == "Host":
            name = e.func.name
            if name == "nowMs":
                if e.args:
                    self.err("Host.nowMs() takes no arguments")
                e.ty = "long"
                e.builtin = "host_now"
                return
            if name == "env":
                if len(e.args) != 1:
                    self.err("Host.env(name) expects 1 argument")
                if self.check_expr(e.args[0], env) != "String":
                    self.err("Host.env: name must be String")
                e.ty = "String"
                e.builtin = "host_env"
                return
            if name == "log":
                for a in e.args:
                    self.check_expr(a, env)
                e.ty = "void"
                e.builtin = "host_log"
                return
            self.err(f"unknown Host method {name}")
        # Mysql.*
        if isinstance(e.func, Member) and isinstance(e.func.obj, Ident) and e.func.obj.name == "Mysql":
            self.p.uses_mysql = True
            name = e.func.name
            if name == "connect":
                if len(e.args) != 5:
                    self.err("Mysql.connect(host, port, user, password, db) expects 5 arguments")
                for k in range(5):
                    if k == 1:
                        if self.check_expr(e.args[k], env) not in ("int", "long"):
                            self.err("Mysql.connect: port must be int")
                    elif self.check_expr(e.args[k], env) != "String":
                        self.err("Mysql.connect: string arguments required")
                e.ty = "void"
                e.builtin = "mysql_connect"
                return
            if name == "exec":
                if len(e.args) != 1 or self.check_expr(e.args[0], env) != "String":
                    self.err("Mysql.exec(sql) expects String")
                e.ty = "int"
                e.builtin = "mysql_exec"
                return
            if name == "queryJson":
                if len(e.args) != 1 or self.check_expr(e.args[0], env) != "String":
                    self.err("Mysql.queryJson(sql) expects String")
                e.ty = "String"
                e.builtin = "mysql_query_json"
                return
            if name == "close":
                if e.args:
                    self.err("Mysql.close() takes no arguments")
                e.ty = "void"
                e.builtin = "mysql_close"
                return
            if name == "pool":
                if len(e.args) != 7:
                    self.err("Mysql.pool(host, port, user, password, db, min, max) expects 7 arguments")
                for k in (0, 2, 3, 4):
                    if self.check_expr(e.args[k], env) != "String":
                        self.err("Mysql.pool: string argument required")
                for k in (1, 5, 6):
                    if self.check_expr(e.args[k], env) not in ("int", "long"):
                        self.err("Mysql.pool: int argument required")
                e.ty = "void"
                e.builtin = "mysql_pool"
                return
            if name == "execPool":
                if len(e.args) != 1 or self.check_expr(e.args[0], env) != "String":
                    self.err("Mysql.execPool(sql) expects String")
                e.ty = "int"
                e.builtin = "mysql_exec_pool"
                return
            if name == "queryJsonPool":
                if len(e.args) != 1 or self.check_expr(e.args[0], env) != "String":
                    self.err("Mysql.queryJsonPool(sql) expects String")
                e.ty = "String"
                e.builtin = "mysql_query_json_pool"
                return
            if name == "closePool":
                if e.args:
                    self.err("Mysql.closePool() takes no arguments")
                e.ty = "void"
                e.builtin = "mysql_close_pool"
                return
            self.err(f"unknown Mysql method {name}")
        # Ws.*
        if isinstance(e.func, Member) and isinstance(e.func.obj, Ident) and e.func.obj.name == "Ws":
            self.p.uses_ws = True
            name = e.func.name
            if name == "route":
                if len(e.args) != 4:
                    self.err("Ws.route(path, Open, Message, Close) expects 4 arguments")
                if self.check_expr(e.args[0], env) != "String":
                    self.err("Ws.route: path must be String")
                names = []
                for k in range(1, 4):
                    h = e.args[k]
                    if not isinstance(h, Ident) or h.name not in self.handlers:
                        self.err("Ws.route: handlers must be handler types")
                    names.append(h)
                oe = self.handler_event[names[0].name].name
                me = self.handler_event[names[1].name].name
                ce = self.handler_event[names[2].name].name
                of = self.event_fields(oe)
                mf = self.event_fields(me)
                cf = self.event_fields(ce)
                if of.get("clientId") not in ("int", "long") or \
                   of.get("path") != "String" or of.get("query") != "String":
                    self.err(f"Ws.route: {oe} needs clientId/path/query")
                if mf.get("clientId") not in ("int", "long") or mf.get("text") != "String":
                    self.err(f"Ws.route: {me} needs clientId/text")
                if cf.get("clientId") not in ("int", "long") or cf.get("code") != "int" or cf.get("reason") != "String":
                    self.err(f"Ws.route: {ce} needs clientId/code/reason")
                if not hasattr(self.p, "ws_routes"):
                    self.p.ws_routes = []
                e.route_index = len(self.p.ws_routes)
                self.p.ws_routes.append({
                    "open": names[0].name, "msg": names[1].name, "close": names[2].name,
                    "open_ev": oe, "msg_ev": me, "close_ev": ce,
                })
                e.ty = "void"
                e.builtin = "ws_route"
                return
            if name == "listen":
                if len(e.args) != 1 or self.check_expr(e.args[0], env) not in ("int", "long"):
                    self.err("Ws.listen(port) expects int port")
                e.ty = "void"
                e.builtin = "ws_listen"
                return
            if name == "stop":
                if e.args:
                    self.err("Ws.stop() takes no arguments")
                e.ty = "void"
                e.builtin = "ws_stop"
                return
            if name == "send":
                if len(e.args) != 2:
                    self.err("Ws.send(clientId, text) expects 2 arguments")
                if self.check_expr(e.args[0], env) not in ("int", "long"):
                    self.err("Ws.send: clientId must be int/long")
                if self.check_expr(e.args[1], env) != "String":
                    self.err("Ws.send: text must be String")
                e.ty = "void"
                e.builtin = "ws_send"
                return
            if name == "close":
                if len(e.args) != 3:
                    self.err("Ws.close(clientId, code, reason) expects 3 arguments")
                if self.check_expr(e.args[0], env) not in ("int", "long"):
                    self.err("Ws.close: clientId must be int/long")
                if self.check_expr(e.args[1], env) != "int":
                    self.err("Ws.close: code must be int")
                if self.check_expr(e.args[2], env) != "String":
                    self.err("Ws.close: reason must be String")
                e.ty = "void"
                e.builtin = "ws_close"
                return
            self.err(f"unknown Ws method {name}")
        # Http.*
        if isinstance(e.func, Member) and isinstance(e.func.obj, Ident) and e.func.obj.name == "Http":
            self.p.uses_http = True
            name = e.func.name
            if name == "route":
                if len(e.args) != 3:
                    self.err("Http.route(path, method, Handler) expects 3 arguments")
                if self.check_expr(e.args[0], env) != "String" or \
                   self.check_expr(e.args[1], env) != "String":
                    self.err("Http.route: path/method must be String")
                h = e.args[2]
                if not isinstance(h, Ident) or h.name not in self.handlers:
                    self.err("Http.route: third argument must be a handler")
                ev = self.handler_event[h.name].name
                fields = self.event_fields(ev)
                if fields.get("reqId") not in ("int", "long"):
                    self.err(f"Http.route: {ev} needs reqId: int/long")
                for fn in ("method", "path", "query", "body"):
                    if fields.get(fn) != "String":
                        self.err(f"Http.route: {ev} needs {fn}: String")
                if not hasattr(self.p, "http_routes"):
                    self.p.http_routes = []
                e.route_index = len(self.p.http_routes)
                self.p.http_routes.append({"handler": h.name, "event": ev})
                e.ty = "void"
                e.builtin = "http_route"
                return
            if name == "listen":
                if len(e.args) != 1:
                    self.err("Http.listen(port) expects 1 argument")
                if self.check_expr(e.args[0], env) not in ("int", "long"):
                    self.err("Http.listen: port must be int")
                e.ty = "void"
                e.builtin = "http_listen"
                return
            if name == "stop":
                if e.args:
                    self.err("Http.stop() takes no arguments")
                e.ty = "void"
                e.builtin = "http_stop"
                return
            if name == "respond":
                if len(e.args) != 3:
                    self.err("Http.respond(reqId, status, body) expects 3 arguments")
                if self.check_expr(e.args[0], env) not in ("int", "long"):
                    self.err("Http.respond: reqId must be int/long")
                if self.check_expr(e.args[1], env) != "int":
                    self.err("Http.respond: status must be int")
                if self.check_expr(e.args[2], env) != "String":
                    self.err("Http.respond: body must be String")
                e.ty = "void"
                e.builtin = "http_respond"
                return
            self.err(f"unknown Http method {name}")
        # Json.parse(Type, String) / Json.tryParse(Type, String)
        if isinstance(e.func, Member) and isinstance(e.func.obj, Ident) and e.func.obj.name == "Json":
            jname = e.func.name
            if jname == "stringify":
                if len(e.args) not in (1, 2):
                    self.err("Json.stringify(value[, indent]) expects 1 or 2 arguments")
                at = self.check_expr(e.args[0], env)
                e.ty = "String"
                e.builtin = "json_stringify"
                e.json_str_kind = None
                if len(e.args) == 2:
                    it = self.check_expr(e.args[1], env)
                    if it not in ("int", "long"):
                        self.err("Json.stringify: indent must be int/long")
                    e.json_indent = e.args[1]
                if at == "null" or at in self.structs:
                    e.json_str_kind = "struct"
                elif at in ("int", "long", "double", "float", "bool", "String"):
                    e.json_str_kind = "double" if at in ("double", "float") else at
                else:
                    ckind, cet = self.container_info(at)
                    if ckind:
                        if cet.startswith(("Array<", "Map<", "Linked<")):
                            self.err("Json.stringify: nested container element type is not supported")
                        if cet not in ("int", "long", "float", "double", "bool", "String") and cet not in self.structs:
                            self.err(f"Json.stringify: unsupported container element type {cet}")
                        e.json_str_kind = ckind
                        e.json_elem_ty = cet
                    else:
                        self.err(f"Json.stringify: unsupported argument type {at}")
                return
            if jname not in ("parse", "tryParse"):
                self.err(f"unknown Json method {jname}")
            if len(e.args) != 2:
                self.err(f"Json.{jname}(Type, String) expects 2 arguments")
            if not isinstance(e.args[0], Ident) or e.args[0].name not in self.structs:
                self.err(f"Json.{jname}: first argument must be a struct/event type")
            st = self.check_expr(e.args[1], env)
            if st != "String":
                self.err(f"Json.{jname}: second argument must be String")
            e.ty = e.args[0].name
            e.builtin = "json_parse" if jname == "parse" else "json_try_parse"
            e.json_type = e.args[0].name
            return
        # IdUtil.getId()
        if isinstance(e.func, Member) and isinstance(e.func.obj, Ident) and e.func.obj.name == "IdUtil":
            if e.func.name != "getId":
                self.err(f"unknown IdUtil method {e.func.name}")
            if e.args:
                self.err("IdUtil.getId() takes no arguments")
            e.ty = "long"
            e.builtin = "idutil_get"
            return
        # Timer.once(ms, Handler) / Timer.loop(ms, Handler) / Timer.cancel(id)
        if isinstance(e.func, Member) and isinstance(e.func.obj, Ident) and e.func.obj.name == "Timer":
            name = e.func.name
            if name in ("once", "loop"):
                if len(e.args) != 2:
                    self.err(f"Timer.{name}(ms, Handler) expects 2 arguments")
                if self.check_expr(e.args[0], env) not in ("int", "long"):
                    self.err(f"Timer.{name}: delay must be int/long (ms)")
                h = e.args[1]
                if not isinstance(h, Ident) or h.name not in self.handlers:
                    self.err(f"Timer.{name}: second argument must be a handler")
                ev = self.handler_event[h.name].name
                e.ty = "long"
                e.builtin = "timer_" + name
                e.timer_index = len(self.p.timer_calls)
                e.timer_handler = h.name
                e.timer_event = ev
                self.p.timer_calls.append({"event": ev, "handler": h.name, "op": name})
                return
            if name == "cancel":
                if len(e.args) != 1:
                    self.err("Timer.cancel(id) expects 1 argument")
                if self.check_expr(e.args[0], env) not in ("int", "long"):
                    self.err("Timer.cancel: id must be int/long")
                e.ty = "void"
                e.builtin = "timer_cancel"
                return
            self.err(f"unknown Timer method {name}")
        # String.fromByte(code)
        if isinstance(e.func, Member) and isinstance(e.func.obj, Ident) and e.func.obj.name == "String":
            if e.func.name == "fromByte":
                if len(e.args) != 1:
                    self.err("String.fromByte(code) expects 1 argument")
                it = self.check_expr(e.args[0], env)
                if it not in ("int", "long"):
                    self.err("String.fromByte: code must be int/long")
                e.ty = "String"
                e.builtin = "str_from_byte"
                return
            if e.func.name == "fromCodePoint":
                if len(e.args) != 1:
                    self.err("String.fromCodePoint(cp) expects 1 argument")
                it = self.check_expr(e.args[0], env)
                if it not in ("int", "long"):
                    self.err("String.fromCodePoint: cp must be int/long")
                e.ty = "String"
                e.builtin = "str_from_codepoint"
                return
            if e.func.name == "fromInt":
                if len(e.args) != 1:
                    self.err("String.fromInt(value) expects 1 argument")
                it = self.check_expr(e.args[0], env)
                if it not in ("int", "long", "bool"):
                    self.err("String.fromInt: value must be int/long")
                e.ty = "String"
                e.builtin = "str_from_int"
                return
            self.err(f"unknown String method {e.func.name}")
        # container / String methods
        if isinstance(e.func, Member) and not (isinstance(e.func.obj, Ident) and e.func.obj.name in ("Model", "Cache")):
            ot = self.check_expr(e.func.obj, env)
            if ot == "String":
                self.check_string_method(e, env)
                return
            if ot == "Bytes":
                self.check_bytes_method(e, env)
                return
            kind, et = self.container_info(ot)
            if kind:
                name = e.func.name
                if kind == "array":
                    if name == "push":
                        if len(e.args) != 1:
                            self.err("Array.push(value) expects 1 argument")
                        at = self.check_expr(e.args[0], env)
                        self.check_assignable(et, at)
                        e.ty = "void"
                        e.method_op = "array_push"
                        e.method_obj = e.func.obj
                        e.elem_ty = et
                        return
                    if name == "pop":
                        if e.args:
                            self.err("Array.pop() takes no arguments")
                        e.ty = et
                        e.method_op = "array_pop"
                        e.method_obj = e.func.obj
                        e.elem_ty = et
                        return
                    if name == "get":
                        if len(e.args) != 1:
                            self.err("Array.get(index) expects 1 argument")
                        it = self.check_expr(e.args[0], env)
                        if it not in ("int", "long"):
                            self.err("array index must be int/long")
                        e.ty = et
                        e.method_op = "array_get"
                        e.method_obj = e.func.obj
                        e.elem_ty = et
                        return
                    if name == "set":
                        if len(e.args) != 2:
                            self.err("Array.set(index, value) expects 2 arguments")
                        it = self.check_expr(e.args[0], env)
                        if it not in ("int", "long"):
                            self.err("array index must be int/long")
                        vt = self.check_expr(e.args[1], env)
                        self.check_assignable(et, vt)
                        e.ty = "void"
                        e.method_op = "array_set"
                        e.method_obj = e.func.obj
                        e.elem_ty = et
                        return
                    if name == "slice":
                        if len(e.args) != 2:
                            self.err("Array.slice(start, end) expects 2 arguments")
                        for a in e.args:
                            if self.check_expr(a, env) not in ("int", "long"):
                                self.err("Array index must be int/long")
                        e.ty = ot
                        e.elem_ty = et
                        e.method_op = "array_slice"
                        e.method_obj = e.func.obj
                        return
                    if name == "concat":
                        if len(e.args) != 1:
                            self.err("Array.concat(other) expects 1 argument")
                        at = self.check_expr(e.args[0], env)
                        if at != ot:
                            self.err(f"Array.concat expects {ot}, got {at}")
                        e.ty = ot
                        e.elem_ty = et
                        e.method_op = "array_concat"
                        e.method_obj = e.func.obj
                        return
                    if name == "join":
                        if len(e.args) != 1:
                            self.err("Array.join(sep) expects 1 argument")
                        if et != "String":
                            self.err("Array.join requires Array<String>")
                        if self.check_expr(e.args[0], env) != "String":
                            self.err("Array.join: sep must be String")
                        e.ty = "String"
                        e.method_op = "array_join"
                        e.method_obj = e.func.obj
                        return
                    if name in ("indexOf", "includes"):
                        if len(e.args) != 1:
                            self.err(f"Array.{name}(value) expects 1 argument")
                        at = self.check_expr(e.args[0], env)
                        self.check_assignable(et, at)
                        e.ty = "int" if name == "indexOf" else "bool"
                        e.method_op = "array_index_of" if name == "indexOf" else "array_includes"
                        e.method_obj = e.func.obj
                        e.elem_ty = et
                        return
                    if name == "clear":
                        if e.args:
                            self.err("Array.clear() takes no arguments")
                        e.ty = "void"
                        e.method_op = "array_clear"
                        e.method_obj = e.func.obj
                        return
                    if name == "sort":
                        if e.args:
                            self.err("Array.sort() takes no arguments")
                        if et not in ("int", "long", "bool", "double", "float", "String"):
                            self.err(f"Array.sort supports int/long/double/String elements, got {et}")
                        e.ty = "void"
                        e.elem_ty = et
                        e.method_op = "array_sort"
                        e.method_obj = e.func.obj
                        return
                    if name == "reverse":
                        if e.args:
                            self.err("Array.reverse() takes no arguments")
                        e.ty = "void"
                        e.elem_ty = et
                        e.method_op = "array_reverse"
                        e.method_obj = e.func.obj
                        return
                    if name == "shift":
                        if e.args:
                            self.err("Array.shift() takes no arguments")
                        e.ty = et
                        e.elem_ty = et
                        e.method_op = "array_shift"
                        e.method_obj = e.func.obj
                        return
                    if name == "unshift":
                        if len(e.args) != 1:
                            self.err("Array.unshift(value) expects 1 argument")
                        at = self.check_expr(e.args[0], env)
                        self.check_assignable(et, at)
                        e.ty = "void"
                        e.elem_ty = et
                        e.method_op = "array_unshift"
                        e.method_obj = e.func.obj
                        return
                    self.err(f"unknown Array method {name}")
                if kind == "linked":
                    if name in ("pushHead", "pushTail"):
                        if len(e.args) != 1:
                            self.err(f"Linked.{name}(value) expects 1 argument")
                        at = self.check_expr(e.args[0], env)
                        self.check_assignable(et, at)
                        e.ty = "void"
                        e.method_op = "linked_" + name
                        e.method_obj = e.func.obj
                        e.elem_ty = et
                        return
                    if name in ("popHead", "popTail", "head", "tail"):
                        if e.args:
                            self.err(f"Linked.{name}() takes no arguments")
                        e.ty = et
                        e.method_op = "linked_" + name
                        e.method_obj = e.func.obj
                        e.elem_ty = et
                        return
                    if name == "get":
                        if len(e.args) != 1:
                            self.err("Linked.get(index) expects 1 argument")
                        it = self.check_expr(e.args[0], env)
                        if it not in ("int", "long"):
                            self.err("Linked index must be int/long")
                        e.ty = et
                        e.method_op = "linked_get"
                        e.method_obj = e.func.obj
                        e.elem_ty = et
                        return
                    self.err(f"unknown Linked method {name}")
                if kind == "map":
                    if name == "has":
                        if len(e.args) != 1:
                            self.err("Map.has(key) expects 1 argument")
                        kt = self.check_expr(e.args[0], env)
                        if kt != "String":
                            self.err("Map key must be String")
                        e.ty = "bool"
                        e.method_op = "map_has"
                        e.method_obj = e.func.obj
                        return
                    if name == "get":
                        if len(e.args) != 1:
                            self.err("Map.get(key) expects 1 argument")
                        kt = self.check_expr(e.args[0], env)
                        if kt != "String":
                            self.err("Map key must be String")
                        e.ty = et
                        e.method_op = "map_get"
                        e.method_obj = e.func.obj
                        e.elem_ty = et
                        return
                    if name == "set":
                        if len(e.args) != 2:
                            self.err("Map.set(key, value) expects 2 arguments")
                        kt = self.check_expr(e.args[0], env)
                        if kt != "String":
                            self.err("Map key must be String")
                        vt = self.check_expr(e.args[1], env)
                        self.check_assignable(et, vt)
                        e.ty = "void"
                        e.method_op = "map_set"
                        e.method_obj = e.func.obj
                        e.elem_ty = et
                        return
                    if name == "delete":
                        if len(e.args) != 1:
                            self.err("Map.delete(key) expects 1 argument")
                        kt = self.check_expr(e.args[0], env)
                        if kt != "String":
                            self.err("Map key must be String")
                        e.ty = "void"
                        e.method_op = "map_delete"
                        e.method_obj = e.func.obj
                        return
                    if name == "clear":
                        if e.args:
                            self.err("Map.clear() takes no arguments")
                        e.ty = "void"
                        e.method_op = "map_clear"
                        e.method_obj = e.func.obj
                        return
                    if name == "keys":
                        if e.args:
                            self.err("Map.keys() takes no arguments")
                        e.ty = "Array<String>"
                        e.elem_ty = "String"
                        e.method_op = "map_keys"
                        e.method_obj = e.func.obj
                        return
                    if name == "values":
                        if e.args:
                            self.err("Map.values() takes no arguments")
                        e.ty = f"Array<{et}>"
                        e.elem_ty = et
                        e.method_op = "map_values"
                        e.method_obj = e.func.obj
                        return
                    self.err(f"unknown Map method {name}")
        # Cache.xxx (cache-only API, separate from Model/Store)
        if isinstance(e.func, Member) and isinstance(e.func.obj, Ident) and e.func.obj.name == "Cache":
            cname = e.func.name
            if cname == "get":
                if len(e.args) != 2 or not isinstance(e.args[0], Ident):
                    self.err("Cache.get(Type, oid) expects a type name and oid")
                ty = e.args[0].name
                self.check_type_name(ty)
                if ty not in self.structs:
                    self.err(f"Cache.get: {ty} is not a struct/event type")
                ot = self.check_expr(e.args[1], env)
                if ot not in ("int", "long"):
                    self.err("Cache.get: oid must be int/long")
                e.ty = ty
                e.cache_op = "get"
                e.cache_type = ty
                return
            if cname == "set":
                if len(e.args) != 1:
                    self.err("Cache.set(value) expects 1 argument")
                at = self.check_expr(e.args[0], env)
                if at not in self.structs:
                    self.err("Cache.set: argument must be a struct/event")
                e.ty = "void"
                e.cache_op = "set"
                return
            if cname == "remove":
                if e.args:
                    self.err("Cache.remove() takes no arguments")
                e.ty = "void"
                e.cache_op = "remove_all"
                return
            self.err(f"unknown Cache method {cname}")
        # Model.xxx
        if isinstance(e.func, Member) and isinstance(e.func.obj, Ident) and e.func.obj.name == "Model":
            name = e.func.name
            if name in ("get", "peek", "findByOid"):
                if len(e.args) != 2 or not isinstance(e.args[0], Ident):
                    self.err(f"Model.{name}(Type, oid) expects a type name and oid")
                ty = e.args[0].name
                self.check_type_name(ty, allow_roots=True)
                if ty not in self.structs and ty not in ("Vo",):
                    self.err(f"Model.{name}: {ty} is not a struct/event type")
                ot = self.check_expr(e.args[1], env)
                if ot not in ("int", "long"):
                    self.err(f"Model.{name}: oid must be int/long")
                e.ty = ty
                e.model_op = name
                e.model_type = ty
                return
            if name == "set":
                if len(e.args) != 1:
                    self.err("Model.set(value) expects 1 argument")
                at = self.check_expr(e.args[0], env)
                if at not in self.structs:
                    self.err("Model.set: argument must be a struct/event")
                e.ty = "void"
                e.model_op = "set"
                return
            if name == "remove":
                if len(e.args) != 2 or not isinstance(e.args[0], Ident):
                    self.err("Model.remove(Type, oid) expects a type name and oid")
                ty = e.args[0].name
                self.check_type_name(ty)
                ot = self.check_expr(e.args[1], env)
                if ot not in ("int", "long"):
                    self.err("Model.remove: oid must be int/long")
                e.ty = "void"
                e.model_op = "remove"
                e.model_type = ty
                return
            self.err(f"unknown Model method {name}")
        # builtins
        if isinstance(e.func, Ident):
            name = e.func.name
            if name == "print":
                for a in e.args:
                    at = self.check_expr(a, env)
                    if at == "void":
                        self.err("print: cannot print a void expression")
                e.ty = "void"
                e.builtin = "print"
                return
            if name in ("emit", "on", "once", "off"):
                if name == "emit":
                    if len(e.args) not in (1, 2):
                        self.err("emit(event[, channel]) expects 1 or 2 arguments")
                    et = self.check_expr(e.args[0], env)
                    if et not in self.structs or self.structs[et]["kind"] != "event":
                        self.err("emit: first argument must be an event")
                    e.event_type = et
                    e.event_arg = e.args[0]
                    if len(e.args) == 2:
                        e.channel_arg = e.args[1]
                        self.check_channel(e.args[1], env, allow_all=False)
                    e.ty = "void"
                    e.builtin = name
                    return
                # on/once/off: (Handler[, channel]) infers the event from the
                # handler's onEvent parameter; (EventType, Handler[, channel])
                # keeps the explicit form.
                args = e.args
                h = None
                if len(args) == 1:
                    a0 = args[0]
                    if not isinstance(a0, Ident) or a0.name not in self.handlers:
                        self.err(f"{name}(Handler) expects a handler; explicit form is {name}(EventType, Handler[, channel])")
                    h = a0
                    e.event_type = self.handler_event[h.name].name
                elif len(args) == 2 and isinstance(args[0], Ident) and args[0].name in self.handlers:
                    h = args[0]
                    e.event_type = self.handler_event[h.name].name
                    e.channel_arg = args[1]
                    self.check_channel(args[1], env, allow_all=True)
                elif len(args) in (2, 3):
                    e.event_type = self.resolve_event_type(args[0])
                    h = args[1]
                    if not isinstance(h, Ident) or h.name not in self.handlers:
                        self.err(f"{name}: second argument must be a handler")
                    hev = self.handler_event[h.name].name
                    if not self.is_event_subtype(e.event_type, hev):
                        self.err(f"{name}: handler {h.name} handles {hev}, not {e.event_type} (the explicit event type must be {hev} or inherit from it)")
                    if len(args) == 3:
                        e.channel_arg = args[2]
                        self.check_channel(args[2], env, allow_all=True)
                else:
                    self.err(f"{name} expects (Handler[, channel]) or (EventType, Handler[, channel])")
                e.handler_name = h.name
                e.ty = "void"
                e.builtin = name
                return
            if name in ("start", "stop"):
                if len(e.args) != 1 or not isinstance(e.args[0], Ident):
                    self.err(f"{name}(Controller) expects a controller name")
                cn = e.args[0].name
                if cn not in self.controllers:
                    self.err(f"unknown controller {cn}")
                e.ty = "void"
                e.builtin = name
                e.controller_name = cn
                return
            _sib = None
            if self.current_namespace:
                if self.current_namespace in self.namespaces:
                    _sib = self.namespaces[self.current_namespace]
                elif self.current_namespace in self.statics:
                    _sib = self.statics[self.current_namespace]["funcs"]
            if _sib is not None and name in _sib:
                f = _sib[name]
                if len(e.args) != len(f.params):
                    self.err(f"{name} expects {len(f.params)} arguments, got {len(e.args)}")
                for i, a in enumerate(e.args):
                    at = self.check_expr(a, env)
                    self.check_assignable(f.params[i][1].name, at)
                e.ty = f.ret.name if f.ret else "void"
                e.call_ns = (self.current_namespace, name)
                return
            if name in self.funcs:
                f = self.funcs[name]
                if len(e.args) != len(f.params):
                    self.err(f"{name} expects {len(f.params)} arguments, got {len(e.args)}")
                for i, a in enumerate(e.args):
                    at = self.check_expr(a, env)
                    self.check_assignable(f.params[i][1].name, at)
                e.ty = f.ret.name if f.ret else "void"
                e.call_user = name
                return
            self.err(f"unknown function {name}")
        # method calls unsupported
        self.err("unsupported call expression (only functions, Model.*, on/once/off/emit/start/print)")

    def check_string_method(self, e, env):
        name = e.func.name
        if name in ("byteAt", "charCodeAt"):
            if len(e.args) != 1:
                self.err(f"String.{name}(index) expects 1 argument")
            it = self.check_expr(e.args[0], env)
            if it not in ("int", "long"):
                self.err("String index must be int/long")
            e.ty = "int"
            e.method_op = "str_byte_at"
        elif name == "codePointAt":
            if len(e.args) != 1:
                self.err("String.codePointAt(index) expects 1 argument")
            it = self.check_expr(e.args[0], env)
            if it not in ("int", "long"):
                self.err("String.codePointAt: index must be int/long")
            e.ty = "int"
            e.method_op = "str_code_point_at"
        elif name in ("substring", "slice"):
            if len(e.args) != 2:
                self.err(f"String.{name}(start, end) expects 2 arguments")
            for a in e.args:
                if self.check_expr(a, env) not in ("int", "long"):
                    self.err("String index must be int/long")
            e.ty = "String"
            e.method_op = "str_substring"
        elif name == "indexOf":
            if len(e.args) not in (1, 2):
                self.err("String.indexOf(needle[, from]) expects 1 or 2 arguments")
            if self.check_expr(e.args[0], env) != "String":
                self.err("String.indexOf: needle must be String")
            if len(e.args) == 2:
                if self.check_expr(e.args[1], env) not in ("int", "long"):
                    self.err("String.indexOf: from must be int/long")
                e.method_op = "str_index_of_from"
            else:
                e.method_op = "str_index_of"
            e.ty = "int"
        elif name in ("contains", "startsWith", "endsWith"):
            if len(e.args) != 1:
                self.err(f"String.{name}(x) expects 1 argument")
            if self.check_expr(e.args[0], env) != "String":
                self.err(f"String.{name}: argument must be String")
            e.ty = "bool"
            e.method_op = {
                "contains": "str_contains",
                "startsWith": "str_starts_with",
                "endsWith": "str_ends_with",
            }[name]
        elif name in ("toLower", "toUpper"):
            if e.args:
                self.err(f"String.{name}() takes no arguments")
            e.ty = "String"
            e.method_op = "str_to_lower" if name == "toLower" else "str_to_upper"
        elif name == "trim":
            if e.args:
                self.err("String.trim() takes no arguments")
            e.ty = "String"
            e.method_op = "str_trim"
        elif name == "replace":
            if len(e.args) != 2:
                self.err("String.replace(old, new) expects 2 arguments")
            for a in e.args:
                if self.check_expr(a, env) != "String":
                    self.err("String.replace: arguments must be String")
            e.ty = "String"
            e.method_op = "str_replace"
        elif name == "split":
            if len(e.args) != 1:
                self.err("String.split(sep) expects 1 argument")
            if self.check_expr(e.args[0], env) != "String":
                self.err("String.split: sep must be String")
            e.ty = "Array<String>"
            e.elem_ty = "String"
            e.method_op = "str_split"
        else:
            self.err(f"unknown String method {name}")
        e.method_obj = e.func.obj

    def check_bytes_method(self, e, env):
        name = e.func.name
        if name == "push":
            if len(e.args) != 1:
                self.err("Bytes.push(byte) expects 1 argument")
            if self.check_expr(e.args[0], env) not in ("int", "long"):
                self.err("Bytes.push: value must be int/long")
            e.ty = "void"
            e.method_op = "bytes_push"
        elif name in ("get", "byteAt"):
            if len(e.args) != 1:
                self.err("Bytes.get(index) expects 1 argument")
            if self.check_expr(e.args[0], env) not in ("int", "long"):
                self.err("Bytes index must be int/long")
            e.ty = "int"
            e.method_op = "bytes_get"
        elif name == "set":
            if len(e.args) != 2:
                self.err("Bytes.set(index, byte) expects 2 arguments")
            if self.check_expr(e.args[0], env) not in ("int", "long"):
                self.err("Bytes index must be int/long")
            if self.check_expr(e.args[1], env) not in ("int", "long"):
                self.err("Bytes.set: value must be int/long")
            e.ty = "void"
            e.method_op = "bytes_set"
        elif name == "append":
            if len(e.args) != 1:
                self.err("Bytes.append(other) expects 1 argument")
            if self.check_expr(e.args[0], env) != "Bytes":
                self.err("Bytes.append: argument must be Bytes")
            e.ty = "void"
            e.method_op = "bytes_append"
        elif name == "appendStr":
            if len(e.args) != 1:
                self.err("Bytes.appendStr(text) expects 1 argument")
            if self.check_expr(e.args[0], env) != "String":
                self.err("Bytes.appendStr: argument must be String")
            e.ty = "void"
            e.method_op = "bytes_append_str"
        elif name == "clear":
            if e.args:
                self.err("Bytes.clear() takes no arguments")
            e.ty = "void"
            e.method_op = "bytes_clear"
        elif name == "toString":
            if e.args:
                self.err("Bytes.toString() takes no arguments")
            e.ty = "String"
            e.method_op = "bytes_to_string"
        else:
            self.err(f"unknown Bytes method {name}")
        e.method_obj = e.func.obj

    def resolve_event_type(self, e) -> str:
        if isinstance(e, Ident) and e.name in self.structs and self.structs[e.name]["kind"] == "event":
            return e.name
        self.err("expected an event type (e.g. on(UserLoginEvent, Handler))")

    def check_channel(self, e, env, allow_all: bool):
        if isinstance(e, Ident) and e.name == "ALL":
            if not allow_all:
                self.err("ALL is only allowed in on/once/off channel")
            e.is_all = True
            e.ty = "long"
            return
        t = self.check_expr(e, env)
        if t not in ("int", "long"):
            self.err("channel must be long/int (or ALL for subscriptions)")
        e.channel = True

    def container_info(self, ty):
        if ty.startswith("Array<") and ty.endswith(">"):
            return "array", ty[6:-1]
        if ty.startswith("Map<") and ty.endswith(">"):
            return "map", ty[4:-1]
        if ty.startswith("Linked<") and ty.endswith(">"):
            return "linked", ty[7:-1]
        return None, None

    def check_type_name(self, ty, allow_void=False, allow_roots=False):
        kind, et = self.container_info(ty)
        if kind:
            if et in ("void", "null", "") or et.startswith("Array<") and not et.endswith(">"):
                self.err(f"invalid container type {ty}")
            self.check_type_name(et, allow_void=allow_void, allow_roots=allow_roots)
            return
        if ty in BUILTIN:
            if ty == "void" and not allow_void:
                self.err("void cannot be used as a value type")
            return
        if ty in self.statics:
            self.err(f"static {ty} cannot be used as a value type")
        if ty in self.structs:
            return
        if ty in ROOTS:
            if allow_roots:
                return
            self.err(f"{ty} cannot be used as a value type")
        self.err(f"unknown type {ty}")

    def field_type(self, ty, field):
        if ty in self.structs:
            for f in self.structs[ty]["fields"]:
                if f.name == field:
                    return f.type.name
        return None

    def is_ref_type(self, ty) -> bool:
        if ty in ("String", "Bytes"):
            return True
        kind, _ = self.container_info(ty)
        if kind:
            return True
        return ty in self.structs

    @staticmethod
    def numeric_rank(ty: str) -> int:
        """Widening rank (lower -> higher); ``-1`` for non-numeric types."""
        return {"bool": 0, "int": 0, "long": 1, "float": 2, "double": 2}.get(ty, -1)

    def check_assignable(self, target, value):
        if value == "null":
            if self.is_ref_type(target):
                return
            self.err(f"cannot assign null to {target}")
        if value == "void":
            self.err("cannot use a void value as an expression")
        if target == value:
            return
        if target in ("bool", "int") and value in ("bool", "int"):
            return
        tr = self.numeric_rank(target)
        vr = self.numeric_rank(value)
        if tr >= 0 and vr >= 0:
            if tr >= vr:
                return
            self.err(f"cannot assign {value} to {target} (narrowing conversion is not allowed)")
        # String is strict; refs strict
        self.err(f"cannot assign {value} to {target}")
