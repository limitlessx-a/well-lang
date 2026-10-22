"""Lightweight type checker + symbol tables for Well MVP."""
from .ast_nodes import *

BUILTIN = {"bool", "int", "long", "float", "double", "String", "Bytes", "void", "null"}
NUMERIC = {"int", "long", "float", "double"}
ROOTS = {"Vo", "Event", "Controller", "Handler"}


class CheckError(Exception):
    pass


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

    # ---------- public ----------
    def run(self):
        self.collect()
        self.check_decl_types()
        for name, h in self.controllers.items():
            for m in h.methods:
                if m.name == "run":
                    self.check_method(m, {}, owner=None)
        for name, h in self.handlers.items():
            ev = self.handler_event.get(name)
            if ev is None:
                self.err(f"handler {name} has no onEvent/handle method")
            for m in h.methods:
                if m.name == self.handler_method[name]:
                    self.check_method(m, {"oid": "long"}, owner=name, event_type=ev)
        for name, f in self.funcs.items():
            self.check_method(f, {}, owner=None, is_func=True)
        for ns, funcs in self.namespaces.items():
            old = self.current_namespace
            self.current_namespace = ns
            for fname, f in funcs.items():
                self.check_method(f, {}, owner=None, is_func=True)
            self.current_namespace = old
        for sname, st in self.statics.items():
            for fname, f in st["fields"].items():
                self.check_type_name(f.type.name)
                self.check_static_field_type(sname, fname, f.type.name, set())
                if f.default is None:
                    continue
                d = f.default
                if isinstance(d, ArrayLit):
                    kind, _ = self.container_info(f.type.name)
                    if kind != "array":
                        self.err(f"static {sname}.{fname}: array literal default requires Array<T>")
                    self.check_container_literal(d, f.type.name, {})
                elif isinstance(d, ObjectLit):
                    kind, _ = self.container_info(f.type.name)
                    if kind != "map":
                        self.err(f"static {sname}.{fname}: map literal default requires Map<T>")
                    self.check_container_literal(d, f.type.name, {})
                elif isinstance(d, New):
                    dt = self.check_expr(d, {})
                    if not (getattr(d, "bytes_new", False) or getattr(d, "container_new", None)):
                        self.err(f"static {sname}.{fname}: default new is limited to Bytes/Array/Map")
                    self.check_assignable(f.type.name, dt)
                else:
                    if not isinstance(d, (IntLit, FloatLit, StrLit, BoolLit, NullLit, Ident)):
                        self.err(f"static {sname}.{fname}: default value must be a literal/const/container literal")
                    dt = self.check_expr(d, {})
                    self.check_assignable(f.type.name, dt)
            old = self.current_namespace
            self.current_namespace = sname
            for fname, f in st["funcs"].items():
                self.check_method(f, {}, owner=None, is_func=True)
            self.current_namespace = old
        if self.p.entry and self.p.entry not in self.controllers:
            self.err(f"entry '{self.p.entry}' is not a controller")
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

    def err(self, msg):
        raise CheckError(f"{self.fn}: {msg}")

    # ---------- declarations ----------
    def collect(self):
        # first pass: names
        for d in self.p.decls:
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
            elif isinstance(d, HandlerDecl):
                self.handlers[d.name] = d
            elif isinstance(d, NamespaceDecl):
                if d.name in self.namespaces:
                    self.err(f"duplicate namespace {d.name}")
                table = {}
                for f in d.funcs:
                    if f.name in table:
                        self.err(f"namespace {d.name}: duplicate function {f.name}")
                    table[f.name] = f
                self.namespaces[d.name] = table
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
        # enums: normalize members (auto values, String default = member name)
        for name, d in self.enums.items():
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
            if kind is None:
                kind = "int"
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
                self.structs[d.name] = {
                    "kind": "event" if d.is_event else "struct",
                    "fields": self.flatten_fields(d.name, d, set()),
                    "base": d.base,
                    "name": d.name,
                }
        # consts
        for d in self.p.decls:
            if isinstance(d, ConstDecl):
                self.consts[d.name] = d
        # handlers: find event method
        for name, h in self.handlers.items():
            for m in h.methods:
                if m.name in ("onEvent", "handle", "on", "handler"):
                    if len(m.params) != 1:
                        self.err(f"handler {name}.{m.name} must take exactly one event param")
                    pname, ptype = m.params[0]
                    if ptype.name not in self.structs or self.structs[ptype.name]["kind"] != "event":
                        if ptype.name not in ("Event",):
                            self.err(f"handler {name}.{m.name} parameter must be an event type")
                    self.handler_event[name] = ptype
                    self.handler_method[name] = m.name
                    break
            if name not in self.handler_event:
                self.err(f"handler {name} must define onEvent(e:SomeEvent)")

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
        env = dict(outer)
        for pname, ptype in m.params:
            self.check_type_name(ptype.name)
            env[pname] = ptype.name
        if m.ret:
            self.check_type_name(m.ret.name)
        ret = m.ret.name if m.ret else "void"
        old_ret, old_loop, old_break = self.cur_ret, self.loop_depth, self.break_depth
        self.cur_ret = ret
        self.loop_depth = 0
        self.break_depth = 0
        self.check_block(m.body, env)
        self.cur_ret = old_ret
        self.loop_depth = old_loop
        self.break_depth = old_break
        if ret != "void" and not self.block_returns(m.body):
            self.err(f"{m.name}: not all paths return a value")

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
            for f in st["fields"]:
                if f.type.name == "void":
                    self.err(f"{name}.{f.name}: a field cannot be void")
                self.check_type_name(f.type.name)
        for name, f in self.funcs.items():
            for pn, pt in f.params:
                self.check_type_name(pt.name)
            if f.ret:
                self.check_type_name(f.ret.name)
        for name, c in self.controllers.items():
            for m in c.methods:
                for pn, pt in m.params:
                    self.check_type_name(pt.name)
                if m.ret:
                    self.check_type_name(m.ret.name)
        for name, h in self.handlers.items():
            for m in h.methods:
                for pn, pt in m.params:
                    self.check_type_name(pt.name)
                if m.ret:
                    self.check_type_name(m.ret.name)
        for ns, funcs in self.namespaces.items():
            for fname, f in funcs.items():
                for pn, pt in f.params:
                    self.check_type_name(pt.name)
                if f.ret:
                    self.check_type_name(f.ret.name)
        for sname, st in self.statics.items():
            for fname, f in st["funcs"].items():
                for pn, pt in f.params:
                    self.check_type_name(pt.name)
                if f.ret:
                    self.check_type_name(f.ret.name)
        for name, d in self.consts.items():
            self.check_type_name(d.type.name)

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

    def check_block(self, b: Block, env):
        for s in b.stmts:
            self.check_stmt(s, env)

    # ---------- statements ----------
    def check_stmt(self, s, env):
        if isinstance(s, VarDecl):
            ty = None
            if s.type:
                ty = s.type.name
                self.check_type_name(ty)
            if ty and isinstance(s.init, (ArrayLit, ObjectLit)):
                self.check_container_literal(s.init, ty, env)
                et = ty
            else:
                et = self.check_expr(s.init, env)
            if ty is None:
                ty = et
                if ty in ("null", None):
                    self.err(f"cannot infer type of var {s.name} (add annotation)")
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
            self.check_stmt(s.then, dict(env))
            if s.els:
                self.check_stmt(s.els, dict(env))
        elif isinstance(s, While):
            self.check_cond(s.cond, env)
            self.loop_depth += 1
            self.break_depth += 1
            self.check_stmt(s.body, dict(env))
            self.break_depth -= 1
            self.loop_depth -= 1
        elif isinstance(s, For):
            fenv = dict(env)
            if s.init:
                self.check_stmt(s.init, fenv)
            if s.cond:
                self.check_cond(s.cond, fenv)
            self.loop_depth += 1
            self.break_depth += 1
            self.check_stmt(s.body, dict(fenv))
            self.break_depth -= 1
            self.loop_depth -= 1
            if s.step:
                self.check_stmt(s.step, fenv)
        elif isinstance(s, ForEach):
            it = self.check_expr(s.iter, env)
            kind, et = self.container_info(it)
            fenv = dict(env)
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
            self.check_stmt(s.body, fenv)
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
                    if vt != "String" or not isinstance(val, StrLit):
                        self.err("String switch case value must be a string literal")
                    k = ("str", val.value)
                else:
                    self.check_assignable(ct, vt)
                    if not self.is_const_case(val):
                        self.err("case value must be a constant")
                    if isinstance(val, IntLit):
                        k = ("int", val.value)
                    elif isinstance(val, Unary) and val.op == "-" and isinstance(val.expr, IntLit):
                        k = ("int", -val.expr.value)
                    else:
                        k = None
                if k is not None:
                    if k in seen:
                        self.err("duplicate case value")
                    seen.add(k)
                self.check_block(body, dict(env))
            if s.default:
                self.check_block(s.default, dict(env))
            self.break_depth -= 1
        elif isinstance(s, Block):
            self.check_block(s, dict(env))
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
        if t not in ("bool", "int", "long", "null"):
            # allow truthy-ish but warn/err
            pass

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
                if t == "null":
                    continue
                if et is None:
                    et = t
                elif et != t:
                    self.err(f"array literal elements must share one type: {et} vs {t}")
            if et is None:
                self.err("cannot infer element type of array literal")
            e.ty = f"Array<{et}>"
        elif isinstance(e, ObjectLit):
            if not e.pairs:
                self.err("empty object literal needs a type annotation")
            vt = None
            for k, v in e.pairs:
                t = self.check_expr(v, env)
                if t == "null":
                    continue
                if vt is None:
                    vt = t
                elif vt != t:
                    self.err(f"object literal values must share one type: {vt} vs {t}")
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
            e.ty = "bool" if e.op == "!" else t
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
            self.check_expr(e.left, env)
            inner = e.right  # Binary(":", a, b)
            if isinstance(inner, Binary) and inner.op == ":":
                ta = self.check_expr(inner.left, env)
                tb = self.check_expr(inner.right, env)
                e.ty = ta if ta != "null" else tb
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
        elif e.op in ("==", "!=", "<", "<=", ">", ">=", "&&", "||"):
            e.ty = "bool"
        elif e.op == "??":
            e.ty = lt if lt != "null" else rt
        elif e.op == "[]":
            e.ty = "int"
        elif e.op in ("+", "-", "*", "/", "%"):
            if lt in ("double", "float") or rt in ("double", "float"):
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
        elif e.op in ("&", "|", "^", "<<", ">>"):
            e.ty = "int"
        else:
            e.ty = lt

    def fold_int(self, e):
        """Fold integer-literal-only expressions (used for compile-time range checks)."""
        if isinstance(e, IntLit):
            return e.value
        if isinstance(e, Unary) and e.op == "-":
            v = self.fold_int(e.expr)
            return -v if v is not None else None
        if isinstance(e, Binary) and e.op in ("+", "-", "*", "/", "%"):
            a = self.fold_int(e.left)
            b = self.fold_int(e.right)
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
                of = {f.name: f.type.name for f in self.structs[oe]["fields"]}
                mf = {f.name: f.type.name for f in self.structs[me]["fields"]}
                cf = {f.name: f.type.name for f in self.structs[ce]["fields"]}
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
                fields = {f.name: f.type.name for f in self.structs[ev]["fields"]}
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
        if isinstance(e.func, Member) and not (isinstance(e.func.obj, Ident) and e.func.obj.name == "Model"):
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
        # Model.xxx
        if isinstance(e.func, Member) and isinstance(e.func.obj, Ident) and e.func.obj.name == "Model":
            name = e.func.name
            if name in ("get", "peek", "findByOid"):
                if len(e.args) != 2 or not isinstance(e.args[0], Ident):
                    self.err(f"Model.{name}(Type, oid) expects a type name and oid")
                ty = e.args[0].name
                self.check_type_name(ty)
                if ty not in self.structs and ty not in ("Vo",):
                    self.err(f"Model.{name}: {ty} is not a struct/event type")
                self.check_expr(e.args[1], env)
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
                self.check_expr(e.args[1], env)
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
                    self.check_expr(a, env)
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
                    if hev != e.event_type:
                        self.err(f"{name}: handler {h.name} handles {hev}, not {e.event_type}")
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
                    self.check_expr(a, env)
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

    def check_type_name(self, ty):
        kind, et = self.container_info(ty)
        if kind:
            if et in ("void", "null", "") or et.startswith("Array<") and not et.endswith(">"):
                self.err(f"invalid container type {ty}")
            self.check_type_name(et)
            return
        if ty in BUILTIN:
            return
        if ty in self.statics:
            self.err(f"static {ty} cannot be used as a value type")
        if ty in self.structs or ty in ROOTS:
            return
        self.err(f"unknown type {ty}")

    def field_type(self, ty, field):
        if ty in self.structs:
            for f in self.structs[ty]["fields"]:
                if f.name == field:
                    return f.type.name
        return None

    def check_assignable(self, target, value):
        if value == "null":
            return
        if target == value:
            return
        if target in NUMERIC and value in NUMERIC:
            return
        if target in ("bool", "int") and value in ("bool", "int"):
            return
        # String is strict; refs strict
        raise CheckError(f"cannot assign {value} to {target}")
