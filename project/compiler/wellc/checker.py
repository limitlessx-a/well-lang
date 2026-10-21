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

    # ---------- public ----------
    def run(self):
        self.collect()
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
        if self.p.entry and self.p.entry not in self.controllers:
            self.err(f"entry '{self.p.entry}' is not a controller")
        # expose tables
        self.p.structs = self.structs
        self.p.events = {k: v for k, v in self.structs.items() if v["kind"] == "event"}
        self.p.enums = self.enums
        self.p.consts = self.consts
        self.p.funcs = self.funcs
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
            env[pname] = ptype.name
        self.check_block(m.body, env)
        # loose return checking skipped

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
            self.check_stmt(s.body, dict(env))
        elif isinstance(s, For):
            fenv = dict(env)
            if s.init:
                self.check_stmt(s.init, fenv)
            if s.cond:
                self.check_cond(s.cond, fenv)
            self.check_stmt(s.body, dict(fenv))
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
            if s.value:
                self.check_expr(s.value, env)
        elif isinstance(s, Block):
            self.check_block(s, dict(env))
        elif isinstance(s, (Break, Continue)):
            pass
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
            e.ty = "int"
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
                return
            if ot == "Bytes":
                it = self.check_expr(e.index, env)
                if it not in ("int", "long"):
                    self.err("Bytes index must be int/long")
                e.ty = "int"
                e.bytes_index = True
                return
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
                    return
                if ft is None and ot == "Bytes" and e.name == "length":
                    e.ty = "long"
                    e.container_len = "bytes"
                    return
                kind, _ = self.container_info(ot)
                if ft is None and kind == "array" and e.name == "length":
                    e.ty = "long"
                    e.container_len = "array"
                    return
                if ft is None and kind == "map" and e.name == "size":
                    e.ty = "long"
                    e.container_len = "map"
                    return
                if ft is None and kind == "linked" and e.name == "length":
                    e.ty = "long"
                    e.container_len = "linked"
                    return
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
            if ty == "Bytes":
                if e.args or e.init:
                    self.err("new Bytes() takes no arguments")
                e.ty = "Bytes"
                e.bytes_new = True
                return
            kind, _ = self.container_info(ty)
            if kind:
                if e.args or e.init:
                    self.err(f"new {ty}() takes no arguments")
                e.ty = ty
                e.container_new = kind
                return
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
        elif e.op in ("&", "|", "^", "<<", ">>"):
            e.ty = "int"
        else:
            e.ty = lt

    def check_call(self, e: Call, env):
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
        # Json.parse(Type, String)
        if isinstance(e.func, Member) and isinstance(e.func.obj, Ident) and e.func.obj.name == "Json":
            if e.func.name != "parse":
                self.err(f"unknown Json method {e.func.name}")
            if len(e.args) != 2:
                self.err("Json.parse(Type, String) expects 2 arguments")
            if not isinstance(e.args[0], Ident) or e.args[0].name not in self.structs:
                self.err("Json.parse: first argument must be a struct/event type")
            st = self.check_expr(e.args[1], env)
            if st != "String":
                self.err("Json.parse: second argument must be String")
            e.ty = e.args[0].name
            e.builtin = "json_parse"
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
                # on / once / off: on(EventType, Handler[, channel])
                if len(e.args) not in (2, 3):
                    self.err(f"{name}(EventType, Handler[, channel]) expects 2 or 3 arguments")
                e.event_type = self.resolve_event_type(e.args[0])
                h = e.args[1]
                if not isinstance(h, Ident) or h.name not in self.handlers:
                    self.err(f"{name}: second argument must be a handler")
                e.handler_name = h.name
                hev = self.handler_event[h.name].name
                if hev != e.event_type:
                    self.err(f"{name}: handler {h.name} handles {hev}, not {e.event_type}")
                if len(e.args) == 3:
                    e.channel_arg = e.args[2]
                    self.check_channel(e.args[2], env, allow_all=True)
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
        elif name == "trim":
            if e.args:
                self.err("String.trim() takes no arguments")
            e.ty = "String"
            e.method_op = "str_trim"
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
