"""Well -> C99 code generator (MVP)."""
from .ast_nodes import *

NUMERIC = {"int", "long", "float", "double"}
VALUE_TYPES = {"bool", "int", "long", "float", "double", "void", "null"}


def c_escape(s: str) -> str:
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        else:
            out.append(ch)
    return "".join(out)


class CodeGen:
    def __init__(self, prog: Program):
        self.p = prog
        self.lines: list[str] = []
        self.ind = 0
        self.env: dict[str, str] = {}
        self.current_ret = "void"
        self.type_ids: dict[str, int] = {}
        self.type_kind: dict[str, str] = {}
        self.controller_ids: dict[str, int] = {}
        self.scope_stack: list[tuple[str, bool]] = []
        self.continue_stack: list[str | None] = []
        self.assign_ids()
        self.entry = prog.entry
        self.ctx = "ctx"
        self.uid = 0

    def next_id(self) -> int:
        self.uid += 1
        return self.uid

    # ---------- helpers ----------
    def w(self, s=""):
        self.lines.append("    " * self.ind + s)

    def assign_ids(self):
        n = 100
        for d in self.p.decls:
            if isinstance(d, StructDecl):
                self.type_ids[d.name] = n
                self.type_kind[d.name] = "event" if d.is_event else "struct"
                n += 1
        n = 10000
        for name in self.p.controllers:
            self.controller_ids[name] = n
            n += 1

    def ctype(self, ty: str) -> str:
        if ty in ("int", "bool"):
            return "int"
        if ty == "long":
            return "long long"
        if ty in ("float", "double"):
            return "double"
        if ty == "String":
            return "WellString*"
        if ty.startswith("Array<") and ty.endswith(">"):
            return "WellArray*"
        if ty.startswith("Map<") and ty.endswith(">"):
            return "WellMap*"
        if ty.startswith("Linked<") and ty.endswith(">"):
            return "WellLinked*"
        if ty == "void":
            return "void"
        if ty == "null":
            return "void*"
        if ty in self.type_ids:
            return f"{ty}*"
        return ty

    def is_ref(self, ty: str) -> bool:
        return ty == "String" or ty in self.type_ids or self.is_container(ty)

    def is_container(self, ty: str) -> bool:
        return (ty.startswith("Array<") and ty.endswith(">")) or \
               (ty.startswith("Map<") and ty.endswith(">")) or \
               (ty.startswith("Linked<") and ty.endswith(">"))

    def container_info(self, ty):
        if ty.startswith("Array<") and ty.endswith(">"):
            return "array", ty[6:-1]
        if ty.startswith("Map<") and ty.endswith(">"):
            return "map", ty[4:-1]
        if ty.startswith("Linked<") and ty.endswith(">"):
            return "linked", ty[7:-1]
        return None, None

    def value_kind(self, ty: str) -> str:
        if ty in ("int", "long", "bool"):
            return "i"
        if ty in ("float", "double"):
            return "d"
        return "p"

    def container_ref_flag(self, et: str) -> str:
        return "1" if self.is_ref(et) else "0"

    def box_value(self, code: str, ty: str) -> str:
        return f"well_box_{self.value_kind(ty)}({code})"

    def unbox_value(self, code: str, ty: str) -> str:
        k = self.value_kind(ty)
        cast = self.ctype(ty)
        if k == "i":
            return f"(({cast})well_unbox_i({code}))"
        if k == "d":
            return f"(({cast})well_unbox_d({code}))"
        return f"(({cast})well_unbox_p({code}))"

    def is_owned(self, e: Expr) -> bool:
        if isinstance(e, New):
            return True
        if isinstance(e, Call) and getattr(e, "model_op", None) in ("get", "peek", "findByOid"):
            return True
        if isinstance(e, Call) and getattr(e, "builtin", None) in ("json_parse", "file_read", "host_env", "mysql_query_json", "mysql_query_json_pool"):
            return True
        if isinstance(e, Binary) and e.op == "+" and getattr(e, "ty", None) == "String":
            return True
        if isinstance(e, Call) and getattr(e, "call_user", None):
            return getattr(e, "ty", "void") not in ("void",) and self.is_ref(getattr(e, "ty"))
        if isinstance(e, Call) and getattr(e, "method_op", None) == "array_pop":
            return self.is_ref(getattr(e, "ty", ""))
        if isinstance(e, Call) and getattr(e, "method_op", None) in ("linked_popHead", "linked_popTail"):
            return self.is_ref(getattr(e, "ty", ""))
        if isinstance(e, Binary) and e.op == "??":
            return self.is_owned(e.left) or self.is_owned(e.right)
        return False

    def enum_macro(self, enum: str, member: str) -> str:
        return f"{enum}_{member}"

    def gen_channel(self, e) -> str:
        if e is None:
            return "0"
        if getattr(e, "is_all", False):
            return "-1"
        return f"(long long)({self.gen_expr(e)})"

    def field_def(self, ty: str, name: str):
        for f in self.p.structs[ty]["fields"]:
            if f.name == name:
                return f
        return None

    def field_type(self, ty: str, name: str) -> str:
        f = self.field_def(ty, name)
        return f.type.name if f else "int"

    def var_decl(self, name: str, ty: str):
        self.env[name] = ty

    # ---------- expression ----------
    def gen_expr(self, e: Expr) -> str:
        if isinstance(e, IntLit):
            return str(e.value)
        if isinstance(e, FloatLit):
            return repr(float(e.value))
        if isinstance(e, StrLit):
            return f'well_string_lit("{c_escape(e.value)}")'
        if isinstance(e, BoolLit):
            return "1" if e.value else "0"
        if isinstance(e, NullLit):
            return "((void*)0)"
        if isinstance(e, Ident):
            return e.name
        if isinstance(e, Member):
            if getattr(e, "is_enum", False) and isinstance(e.obj, Ident):
                return self.enum_macro(e.obj.name, e.name)
            if getattr(e, "str_len", False):
                return f"well_string_len({self.gen_expr(e.obj)})"
            if getattr(e, "container_len", None) == "array":
                return f"well_array_len({self.gen_expr(e.obj)})"
            if getattr(e, "container_len", None) == "map":
                return f"well_map_len({self.gen_expr(e.obj)})"
            if getattr(e, "container_len", None) == "linked":
                return f"well_linked_len({self.gen_expr(e.obj)})"
            return f"({self.gen_expr(e.obj)})->{e.name}"
        if isinstance(e, Index):
            ot = getattr(e.obj, "ty", None)
            kind, et = self.container_info(ot)
            obj = self.gen_expr(e.obj)
            idx = self.gen_expr(e.index)
            if kind == "array":
                val = f"well_array_get({obj}, (long long)({idx}))"
            elif kind == "map":
                val = f"well_map_get({obj}, {idx})"
            else:
                raise Exception("index on non-container")
            return self.unbox_value(val, et)
        if isinstance(e, Unary):
            return f"({e.op}{self.gen_expr(e.expr)})"
        if isinstance(e, Binary):
            return self.gen_binary(e)
        if isinstance(e, New):
            kind, et = self.container_info(e.type.name)
            if kind:
                return f"well_{kind}_new(ctx, {self.container_ref_flag(et)})"
            if e.init:
                raise Exception("new-with-init used outside supported statement context")
            oid = "ctx->oid"
            if self.type_kind.get(e.type.name) == "event" and e.args:
                oid = f"(long long)({self.gen_expr(e.args[0])})"
            return f"new_{e.type.name}(ctx, {oid})"
        if isinstance(e, (ArrayLit, ObjectLit)):
            raise Exception("container literal only supported in var/assignment context")
        if isinstance(e, Call):
            return self.gen_call_expr(e)
        raise Exception(f"unsupported expression {type(e).__name__}")

    def gen_binary(self, e: Binary) -> str:
        if e.op == "?:":
            inner = e.right
            if isinstance(inner, Binary) and inner.op == ":":
                return f"({self.gen_expr(e.left)} ? {self.gen_expr(inner.left)} : {self.gen_expr(inner.right)})"
            return f"({self.gen_expr(e.left)} ? {self.gen_expr(inner)} : 0)"
        if e.op == ":":
            return self.gen_expr(e.left)
        lt = getattr(e.left, "ty", None)
        if e.op == "+" and getattr(e, "ty", None) == "String":
            return f"well_string_concat({self.gen_expr(e.left)}, {self.gen_expr(e.right)})"
        if e.op in ("==", "!=") and (lt == "String" or getattr(e.right, "ty", None) == "String"):
            eq = f"well_string_eq({self.gen_expr(e.left)}, {self.gen_expr(e.right)})"
            return eq if e.op == "==" else f"(!{eq})"
        return f"({self.gen_expr(e.left)} {e.op} {self.gen_expr(e.right)})"

    def gen_call_expr(self, e: Call) -> str:
        if getattr(e, "builtin", None) == "mysql_connect":
            a = [self.gen_expr(x) for x in e.args]
            return (f"well_mysql_connect(well_string_cstr({a[0]}), (int)({a[1]}), "
                    f"well_string_cstr({a[2]}), well_string_cstr({a[3]}), well_string_cstr({a[4]}))")
        if getattr(e, "builtin", None) == "mysql_exec":
            return f"well_mysql_exec(well_string_cstr({self.gen_expr(e.args[0])}))"
        if getattr(e, "builtin", None) == "mysql_query_json":
            return f"well_mysql_query_json(well_string_cstr({self.gen_expr(e.args[0])}))"
        if getattr(e, "builtin", None) == "mysql_close":
            return "well_mysql_close()"
        if getattr(e, "builtin", None) == "mysql_pool":
            a = [self.gen_expr(x) for x in e.args]
            return (f"well_mysql_pool_configure(well_string_cstr({a[0]}), (int)({a[1]}), "
                    f"well_string_cstr({a[2]}), well_string_cstr({a[3]}), well_string_cstr({a[4]}), "
                    f"(int)({a[5]}), (int)({a[6]}))")
        if getattr(e, "builtin", None) == "mysql_exec_pool":
            return f"well_mysql_pool_exec_global(well_string_cstr({self.gen_expr(e.args[0])}))"
        if getattr(e, "builtin", None) == "mysql_query_json_pool":
            return f"well_mysql_pool_query_json_global(well_string_cstr({self.gen_expr(e.args[0])}))"
        if getattr(e, "builtin", None) == "mysql_close_pool":
            return "well_mysql_pool_close_global()"
        if getattr(e, "builtin", None) == "ws_route":
            n = e.route_index
            return ("well_ws_route(well_string_cstr(" + self.gen_expr(e.args[0]) + "), "
                    f"ws_open_{n}, ws_msg_{n}, ws_close_{n})")
        if getattr(e, "builtin", None) == "ws_listen":
            return f"well_ws_listen((int)({self.gen_expr(e.args[0])}))"
        if getattr(e, "builtin", None) == "ws_stop":
            return "well_ws_stop()"
        if getattr(e, "builtin", None) == "ws_send":
            return (f"well_ws_send((long long)({self.gen_expr(e.args[0])}), "
                    f"well_string_cstr({self.gen_expr(e.args[1])}))")
        if getattr(e, "builtin", None) == "ws_close":
            return (f"well_ws_close((long long)({self.gen_expr(e.args[0])}), "
                    f"(int)({self.gen_expr(e.args[1])}), "
                    f"well_string_cstr({self.gen_expr(e.args[2])}))")
        if getattr(e, "builtin", None) == "http_route":
            return (f"well_http_route(well_string_cstr({self.gen_expr(e.args[0])}), "
                    f"well_string_cstr({self.gen_expr(e.args[1])}), http_route_{e.route_index})")
        if getattr(e, "builtin", None) == "http_listen":
            return f"well_http_listen((int)({self.gen_expr(e.args[0])}))"
        if getattr(e, "builtin", None) == "http_stop":
            return "well_http_stop()"
        if getattr(e, "builtin", None) == "http_respond":
            return (f"well_http_respond((long long)({self.gen_expr(e.args[0])}), "
                    f"(int)({self.gen_expr(e.args[1])}), well_string_cstr({self.gen_expr(e.args[2])}))")
        if getattr(e, "builtin", None) == "file_read":
            return f"well_file_read(well_string_cstr({self.gen_expr(e.args[0])}))"
        if getattr(e, "builtin", None) == "file_write":
            return (f"well_file_write(well_string_cstr({self.gen_expr(e.args[0])}), "
                    f"well_string_cstr({self.gen_expr(e.args[1])}))")
        if getattr(e, "builtin", None) == "file_exists":
            return f"well_file_exists(well_string_cstr({self.gen_expr(e.args[0])}))"
        if getattr(e, "builtin", None) == "file_remove":
            return f"well_file_remove(well_string_cstr({self.gen_expr(e.args[0])}))"
        if getattr(e, "builtin", None) == "host_now":
            return "well_host_now_ms()"
        if getattr(e, "builtin", None) == "host_env":
            return f"well_host_env(well_string_cstr({self.gen_expr(e.args[0])}))"
        if getattr(e, "builtin", None) == "json_parse":
            ty = e.json_type
            arg = self.gen_expr(e.args[1])
            return f"(({ty}*)well_json_parse(T_{ty}, well_string_cstr({arg}), ctx->oid))"
        if getattr(e, "builtin", None) == "idutil_get":
            return "well_id_util_get_id()"
        if getattr(e, "method_op", None):
            op = e.method_op
            obj = self.gen_expr(e.method_obj)
            if op == "array_push":
                return f"well_array_push({obj}, {self.box_value(self.gen_expr(e.args[0]), e.elem_ty)}, ctx)"
            if op == "array_pop":
                return self.unbox_value(f"well_array_pop({obj}, ctx)", e.elem_ty)
            if op == "array_get":
                return self.unbox_value(f"well_array_get({obj}, (long long)({self.gen_expr(e.args[0])}))", e.elem_ty)
            if op == "array_set":
                return f"well_array_set({obj}, (long long)({self.gen_expr(e.args[0])}), {self.box_value(self.gen_expr(e.args[1]), e.elem_ty)}, ctx)"
            if op == "map_has":
                return f"well_map_has({obj}, {self.gen_expr(e.args[0])})"
            if op == "map_get":
                return self.unbox_value(f"well_map_get({obj}, {self.gen_expr(e.args[0])})", e.elem_ty)
            if op == "map_set":
                return f"well_map_set({obj}, {self.gen_expr(e.args[0])}, {self.box_value(self.gen_expr(e.args[1]), e.elem_ty)}, ctx)"
            if op == "map_delete":
                return f"well_map_delete({obj}, {self.gen_expr(e.args[0])}, ctx)"
            if op in ("linked_pushHead", "linked_pushTail"):
                fn = "well_linked_push_head" if op.endswith("Head") else "well_linked_push_tail"
                return f"{fn}({obj}, {self.box_value(self.gen_expr(e.args[0]), e.elem_ty)}, ctx)"
            if op in ("linked_popHead", "linked_popTail"):
                fn = "well_linked_pop_head" if op.endswith("Head") else "well_linked_pop_tail"
                return self.unbox_value(f"{fn}({obj}, ctx)", e.elem_ty)
            if op in ("linked_head", "linked_tail"):
                fn = "well_linked_head" if op == "linked_head" else "well_linked_tail"
                return self.unbox_value(f"{fn}({obj})", e.elem_ty)
            if op == "linked_get":
                return self.unbox_value(f"well_linked_get({obj}, (long long)({self.gen_expr(e.args[0])}))", e.elem_ty)
            raise Exception(f"unsupported method {op}")
        if getattr(e, "model_op", None) == "get":
            return f"(({self.ctype(e.model_type)})well_model_get_ctx(T_{e.model_type}, (long long)({self.gen_expr(e.args[1])}), ctx))"
        if getattr(e, "model_op", None) == "peek":
            return f"(({self.ctype(e.model_type)})well_model_peek_ctx(T_{e.model_type}, (long long)({self.gen_expr(e.args[1])}), ctx))"
        if getattr(e, "model_op", None) == "findByOid":
            return f"(({self.ctype(e.model_type)})well_model_get_ctx(T_{e.model_type}, (long long)({self.gen_expr(e.args[1])}), ctx))"
        if getattr(e, "model_op", None) == "remove":
            return f"well_model_remove(T_{e.model_type}, (long long)({self.gen_expr(e.args[1])}), ctx)"
        if getattr(e, "model_op", None) == "set":
            return f"well_model_set((void*)({self.gen_expr(e.args[0])}), ctx)"
        if getattr(e, "builtin", None) == "emit":
            channel = self.gen_channel(getattr(e, "channel_arg", None))
            return f"well_emit_event((void*)({self.gen_expr(e.event_arg)}), (long long)({channel}))"
        if getattr(e, "builtin", None) in ("on", "once", "off"):
            h = e.handler_name
            fn = f"{h}_onEvent"
            channel = self.gen_channel(getattr(e, "channel_arg", None))
            if e.builtin == "off":
                return f"well_off_type(T_{e.event_type}, {fn}, (long long)({channel}))"
            once = "1" if e.builtin == "once" else "0"
            return f"well_on_type(T_{e.event_type}, {fn}, {once}, (long long)({channel}))"
        if getattr(e, "builtin", None) == "start":
            return f"{e.controller_name}_ensure(ctx)"
        if getattr(e, "builtin", None) == "stop":
            return f"{e.controller_name}_stop(ctx)"
        if getattr(e, "builtin", None) in ("print", "host_log"):
            raise Exception("print/Host.log is only supported as a statement")
        if getattr(e, "call_user", None):
            args = ", ".join(["ctx"] + [self.gen_expr(a) for a in e.args])
            return f"{e.call_user}({args})"
        raise Exception("unsupported call")

    # ---------- statements ----------
    def gen_block(self, b: Block):
        for s in b.stmts:
            self.gen_stmt(s)

    def push_scope(self, mark: str, is_loop: bool = False):
        self.scope_stack.append((mark, is_loop))

    def pop_scope(self, mark: str):
        if self.scope_stack and self.scope_stack[-1][0] == mark:
            self.scope_stack.pop()

    def emit_scope_release(self, mark: str):
        self.w(f"well_ctx_release_from(ctx, {mark});")

    def emit_return_cleanup(self):
        for mark, _ in reversed(self.scope_stack):
            self.emit_scope_release(mark)

    def emit_break_cleanup(self):
        for mark, is_loop in reversed(self.scope_stack):
            self.emit_scope_release(mark)
            if is_loop:
                break

    def gen_stmt(self, s: Stmt):
        if isinstance(s, VarDecl):
            self.gen_var_decl(s)
        elif isinstance(s, Assign):
            self.gen_assign(s)
        elif isinstance(s, ExprStmt):
            self.gen_expr_stmt(s)
        elif isinstance(s, If):
            self.w(f"if ({self.gen_expr(s.cond)}) {{")
            self.ind += 1
            self.gen_stmt(s.then)
            self.ind -= 1
            if s.els:
                self.w("} else {")
                self.ind += 1
                self.gen_stmt(s.els)
                self.ind -= 1
            self.w("}")
        elif isinstance(s, While):
            self.w(f"while ({self.gen_expr(s.cond)}) {{")
            self.ind += 1
            m = self.next_id()
            mk = f"_m{m}"
            self.w(f"int {mk} = well_ctx_mark(ctx);")
            self.push_scope(mk, True)
            self.continue_stack.append(None)
            self.gen_stmt(s.body)
            self.continue_stack.pop()
            self.emit_scope_release(mk)
            self.pop_scope(mk)
            self.ind -= 1
            self.w("}")
        elif isinstance(s, ForEach):
            it = self.gen_expr(s.iter)
            kind, et = self.container_info(getattr(s.iter, "ty", None) or "")
            n = self.next_id()
            if kind == "array":
                self.w(f"for (long long _i{n} = 0; _i{n} < well_array_len({it}); _i{n}++) {{")
                self.ind += 1
                self.w(f"{self.ctype(et)} {s.var} = {self.unbox_value(f'well_array_get({it}, _i{n})', et)};")
            elif kind == "linked":
                self.w(f"for (WellLinkedNode* _n{n} = well_linked_first({it}); _n{n}; _n{n} = well_linked_next(_n{n})) {{")
                self.ind += 1
                self.w(f"{self.ctype(et)} {s.var} = {self.unbox_value(f'well_linked_value(_n{n})', et)};")
            else:
                self.w(f"for (long long _i{n} = 0; _i{n} < well_map_len({it}); _i{n}++) {{")
                self.ind += 1
                self.w(f"WellString* {s.var} = well_map_key_at({it}, _i{n});")
            m = self.next_id()
            mk = f"_m{m}"
            self.w(f"int {mk} = well_ctx_mark(ctx);")
            self.push_scope(mk, True)
            self.continue_stack.append(None)
            self.gen_stmt(s.body)
            self.continue_stack.pop()
            self.emit_scope_release(mk)
            self.pop_scope(mk)
            self.ind -= 1
            self.w("}")
        elif isinstance(s, For):
            self.w("{")
            self.ind += 1
            if s.init:
                self.gen_stmt(s.init)
            cond = self.gen_expr(s.cond) if s.cond else "1"
            self.w(f"while ({cond}) {{")
            self.ind += 1
            m = self.next_id()
            mk = f"_m{m}"
            self.w(f"int {mk} = well_ctx_mark(ctx);")
            self.push_scope(mk, True)
            cont = f"_cont{m}"
            self.continue_stack.append(cont)
            self.gen_stmt(s.body)
            self.continue_stack.pop()
            self.emit_scope_release(mk)
            self.w(f"{cont}: ;")
            self.pop_scope(mk)
            if s.step:
                self.gen_stmt(s.step)
            self.ind -= 1
            self.w("}")
            self.ind -= 1
            self.w("}")
        elif isinstance(s, Return):
            self.gen_return(s)
        elif isinstance(s, Break):
            self.emit_break_cleanup()
            self.w("break;")
        elif isinstance(s, Continue):
            self.emit_break_cleanup()
            if self.continue_stack and self.continue_stack[-1]:
                self.w(f"goto {self.continue_stack[-1]};")
            else:
                self.w("continue;")
        elif isinstance(s, Block):
            self.w("{")
            self.ind += 1
            m = self.next_id()
            mk = f"_m{m}"
            self.w(f"int {mk} = well_ctx_mark(ctx);")
            self.push_scope(mk, False)
            self.gen_block(s)
            self.emit_scope_release(mk)
            self.pop_scope(mk)
            self.ind -= 1
            self.w("}")
        else:
            raise Exception(f"unsupported stmt {type(s).__name__}")

    def gen_var_decl(self, s: VarDecl):
        ty = getattr(s, "ty", None) or (s.type.name if s.type else None)
        if not ty:
            raise Exception(f"cannot determine type for {s.name}")
        if isinstance(s.init, (ArrayLit, ObjectLit)):
            kind, et = self.container_info(ty)
            if not kind:
                raise Exception(f"container literal with non-container type {ty}")
            if kind == "array":
                self.w(f"WellArray* {s.name} = well_array_new(ctx, {self.container_ref_flag(et)});")
                for it in s.init.items:
                    self.emit_container_push(s.name, it, et)
            else:
                self.w(f"WellMap* {s.name} = well_map_new(ctx, {self.container_ref_flag(et)});")
                for k, v in s.init.pairs:
                    self.emit_map_set(s.name, f'well_string_lit("{c_escape(k)}")', v, et)
            self.w(f"well_track_owned(ctx, {s.name});")
            self.var_decl(s.name, ty)
            return
        if isinstance(s.init, New) and self.container_info(s.init.type.name)[0]:
            self.w(f"{self.ctype(ty)} {s.name} = {self.gen_expr(s.init)};")
            self.w(f"well_track_owned(ctx, {s.name});")
            self.var_decl(s.name, ty)
            return
        if self.is_ref(ty):
            if isinstance(s.init, New):
                var = s.name
                self.emit_new_into(var, ty, s.init)
                self.w(f"well_track_owned(ctx, {var});")
            else:
                self.w(f"{self.ctype(ty)} {s.name} = {self.gen_expr(s.init)};")
                if self.is_owned(s.init):
                    self.w(f"well_track_owned(ctx, {s.name});")
                else:
                    self.w(f"well_track(ctx, {s.name});")
            self.var_decl(s.name, ty)
        else:
            self.w(f"{self.ctype(ty)} {s.name} = {self.gen_expr(s.init)};")
            self.var_decl(s.name, ty)

    def emit_container_push(self, arr: str, val: Expr, et: str):
        if self.is_owned(val):
            tmp = f"_pv{self.next_id()}"
            self.w("{")
            self.ind += 1
            self.w(f"{self.ctype(et)} {tmp} = {self.gen_expr(val)};")
            self.w(f"well_array_push({arr}, {self.box_value(tmp, et)}, ctx);")
            self.w(f"well_release((void*){tmp});")
            self.ind -= 1
            self.w("}")
        else:
            self.w(f"well_array_push({arr}, {self.box_value(self.gen_expr(val), et)}, ctx);")

    def emit_linked_push(self, lst: str, val: Expr, et: str, head: bool):
        fn = "well_linked_push_head" if head else "well_linked_push_tail"
        if self.is_owned(val):
            tmp = f"_lv{self.next_id()}"
            self.w("{")
            self.ind += 1
            self.w(f"{self.ctype(et)} {tmp} = {self.gen_expr(val)};")
            self.w(f"{fn}({lst}, {self.box_value(tmp, et)}, ctx);")
            self.w(f"well_release((void*){tmp});")
            self.ind -= 1
            self.w("}")
        else:
            self.w(f"{fn}({lst}, {self.box_value(self.gen_expr(val), et)}, ctx);")

    def emit_array_set(self, arr: str, idx: str, val: Expr, et: str):
        if self.is_owned(val):
            tmp = f"_sv{self.next_id()}"
            self.w("{")
            self.ind += 1
            self.w(f"{self.ctype(et)} {tmp} = {self.gen_expr(val)};")
            self.w(f"well_array_set({arr}, (long long)({idx}), {self.box_value(tmp, et)}, ctx);")
            self.w(f"well_release((void*){tmp});")
            self.ind -= 1
            self.w("}")
        else:
            self.w(f"well_array_set({arr}, (long long)({idx}), {self.box_value(self.gen_expr(val), et)}, ctx);")

    def emit_map_set(self, m: str, key_code: str, val: Expr, et: str):
        if self.is_owned(val):
            tmp = f"_mv{self.next_id()}"
            self.w("{")
            self.ind += 1
            self.w(f"{self.ctype(et)} {tmp} = {self.gen_expr(val)};")
            self.w(f"well_map_set({m}, {key_code}, {self.box_value(tmp, et)}, ctx);")
            self.w(f"well_release((void*){tmp});")
            self.ind -= 1
            self.w("}")
        else:
            self.w(f"well_map_set({m}, {key_code}, {self.box_value(self.gen_expr(val), et)}, ctx);")

    def emit_new_into(self, var: str, ty: str, e: New):
        kind = self.type_kind[ty]
        oid = "ctx->oid"
        if kind == "event" and e.args:
            oid = f"(long long)({self.gen_expr(e.args[0])})"
        self.w(f"{self.ctype(ty)} {var} = new_{ty}(ctx, {oid});")
        for fname, val in e.init:
            self.emit_field_assign(var, ty, fname, val, event=(kind == "event"))

    def emit_field_assign(self, obj: str, ty: str, fname: str, val: Expr, event=False):
        ft = self.field_type(ty, fname)
        if self.is_ref(ft):
            code = self.gen_expr(val)
            owned = self.is_owned(val)
            self.w("{")
            self.ind += 1
            self.w(f"{self.ctype(ft)} _fv = {code};")
            if not event and ft != "String":
                self.w(f"well_check_value_oid((void*)({obj}), (void*)_fv, ctx);")
            self.w(f"well_set_ref((void*)({obj}), (void**)&({obj})->{fname}, (void*)_fv);")
            if owned:
                self.w("well_release((void*)_fv);")
            self.ind -= 1
            self.w("}")
        else:
            self.w(f"({obj})->{fname} = {self.gen_expr(val)};")

    def gen_assign(self, s: Assign):
        tgt = s.target
        if isinstance(tgt, Ident):
            ty = self.env.get(tgt.name)
            if ty is None:
                raise Exception(f"unknown variable {tgt.name}")
            if self.is_ref(ty):
                if isinstance(s.value, New):
                    tmp = f"__new_{tgt.name}"
                    self.w("{")
                    self.ind += 1
                    self.emit_new_into(tmp, ty, s.value)
                    self.w(f"well_ctx_replace(ctx, (void**)&{tgt.name}, (void*){tmp}, 1);")
                    self.ind -= 1
                    self.w("}")
                else:
                    code = self.gen_expr(s.value)
                    self.w("{")
                    self.ind += 1
                    self.w(f"{self.ctype(ty)} _v = {code};")
                    owned = 1 if self.is_owned(s.value) else 0
                    self.w(f"well_ctx_replace(ctx, (void**)&{tgt.name}, (void*)_v, {owned});")
                    self.ind -= 1
                    self.w("}")
            else:
                self.w(f"{tgt.name} = {self.gen_expr(s.value)};")
            return
        if isinstance(tgt, Index):
            ot = getattr(tgt.obj, "ty", None)
            kind, et = self.container_info(ot)
            obj = self.gen_expr(tgt.obj)
            idx = self.gen_expr(tgt.index)
            if kind == "array":
                self.emit_array_set(obj, idx, s.value, et)
            elif kind == "map":
                self.emit_map_set(obj, idx, s.value, et)
            else:
                raise Exception("index assignment on non-container")
            return
        if isinstance(tgt, Member):
            oty = getattr(tgt.obj, "ty", None)
            ft = self.field_type(oty, tgt.name) if oty else "int"
            if self.is_ref(ft):
                obj = self.gen_expr(tgt.obj)
                code = self.gen_expr(s.value)
                owned = self.is_owned(s.value)
                self.w("{")
                self.ind += 1
                self.w(f"{self.ctype(oty)} _t = {obj};")
                self.w('well_check_oid((void*)_t, ctx, "field write");')
                self.w(f"{self.ctype(ft)} _v = {code};")
                if ft != "String":
                    self.w("well_check_value_oid((void*)_t, (void*)_v, ctx);")
                self.w(f"well_set_ref((void*)_t, (void**)&_t->{tgt.name}, (void*)_v);")
                if owned:
                    self.w("well_release((void*)_v);")
                self.ind -= 1
                self.w("}")
            else:
                obj = self.gen_expr(tgt.obj)
                self.w("{")
                self.ind += 1
                self.w(f"{self.ctype(oty)} _t = {obj};")
                self.w('well_check_oid((void*)_t, ctx, "field write");')
                self.w(f"_t->{tgt.name} = {self.gen_expr(s.value)};")
                self.ind -= 1
                self.w("}")
            return
        raise Exception("bad assignment target")

    def gen_expr_stmt(self, s: ExprStmt):
        e = s.expr
        if isinstance(e, Call) and getattr(e, "builtin", None) == "print":
            self.w("well_print_begin();")
            for a in e.args:
                ty = getattr(a, "ty", "int")
                code = self.gen_expr(a)
                if ty == "String":
                    self.w(f"well_print_str({code});")
                elif ty == "bool":
                    self.w(f"well_print_bool({code});")
                elif ty in ("double", "float"):
                    self.w(f"well_print_double({code});")
                else:
                    self.w(f"well_print_int((long long)({code}));")
            self.w("well_print_nl();")
            self.w("well_print_end();")
            return
        if isinstance(e, Call) and getattr(e, "builtin", None) == "host_log":
            self.w("well_print_begin();")
            for a in e.args:
                ty = getattr(a, "ty", "int")
                code = self.gen_expr(a)
                if ty == "String":
                    self.w(f"well_log_str({code});")
                elif ty == "bool":
                    self.w(f"well_log_bool({code});")
                elif ty in ("double", "float"):
                    self.w(f"well_log_double({code});")
                else:
                    self.w(f"well_log_int((long long)({code}));")
            self.w("well_log_nl();")
            self.w("well_print_end();")
            return
        if isinstance(e, Call) and getattr(e, "method_op", None) == "array_push":
            self.emit_container_push(self.gen_expr(e.method_obj), e.args[0], e.elem_ty)
            return
        if isinstance(e, Call) and getattr(e, "method_op", None) in ("linked_pushHead", "linked_pushTail"):
            self.emit_linked_push(self.gen_expr(e.method_obj), e.args[0], e.elem_ty,
                                  e.method_op == "linked_pushHead")
            return
        if isinstance(e, Call) and getattr(e, "method_op", None) == "array_set":
            self.emit_array_set(self.gen_expr(e.method_obj), self.gen_expr(e.args[0]), e.args[1], e.elem_ty)
            return
        if isinstance(e, Call) and getattr(e, "method_op", None) == "map_set":
            self.emit_map_set(self.gen_expr(e.method_obj), self.gen_expr(e.args[0]), e.args[1], e.elem_ty)
            return
        if isinstance(e, Call) and getattr(e, "method_op", None) == "map_delete":
            self.w(f"well_map_delete({self.gen_expr(e.method_obj)}, {self.gen_expr(e.args[0])}, ctx);")
            return
        # emit / Model.set / owned call results need a frame-journaled temp
        if isinstance(e, Call) and getattr(e, "builtin", None) == "emit":
            ev = e.event_arg
            channel = self.gen_channel(getattr(e, "channel_arg", None))
            self.w("{")
            self.ind += 1
            if isinstance(ev, New) and ev.init:
                self.emit_new_into("_ev", ev.type.name, ev)
                self.w("well_track_owned(ctx, _ev);")
            else:
                self.w(f"{self.ctype(getattr(ev, 'ty', 'int'))} _ev = {self.gen_expr(ev)};")
                if self.is_owned(ev):
                    self.w("well_track_owned(ctx, _ev);")
                else:
                    self.w("well_track(ctx, _ev);")
            self.w(f"well_emit_event((void*)_ev, (long long)({channel}));")
            self.ind -= 1
            self.w("}")
            return
        if isinstance(e, Call) and getattr(e, "model_op", None) == "set" and self.is_owned(e.args[0]):
            self.w("{")
            self.ind += 1
            self.w(f"{self.ctype(getattr(e.args[0], 'ty', 'int'))} _mv = {self.gen_expr(e.args[0])};")
            self.w("well_track_owned(ctx, _mv);")
            self.w("well_model_set((void*)_mv, ctx);")
            self.ind -= 1
            self.w("}")
            return
        if self.is_owned(e) and isinstance(e, (New, Call)):
            self.w("{")
            self.ind += 1
            self.w(f"{self.ctype(getattr(e, 'ty', 'void'))} _res = {self.gen_expr(e)};")
            self.w("well_track_owned(ctx, _res);")
            self.ind -= 1
            self.w("}")
            return
        self.w(self.gen_expr(e) + ";")

    def gen_return(self, s: Return):
        if s.value is None:
            self.emit_return_cleanup()
            self.w("return;")
            return
        rt = self.current_ret
        if self.is_ref(rt):
            code = self.gen_expr(s.value)
            if self.is_owned(s.value):
                self.emit_return_cleanup()
                self.w(f"return {code};")
            else:
                self.w(f"well_retain((void*)({code}));")
                self.emit_return_cleanup()
                self.w(f"return {code};")
        else:
            code = self.gen_expr(s.value)
            self.emit_return_cleanup()
            self.w(f"return {code};")

    # ---------- declarations ----------
    def gen(self) -> str:
        self.w("/* Generated by wellc. DO NOT EDIT. */")
        self.w('#include "wellrt.h"')
        if self.p.uses_http:
            self.w('#include "well_http.h"')
        if self.p.uses_ws:
            self.w('#include "well_ws.h"')
        if self.p.uses_mysql:
            self.w('#include "well_mysql.h"')
        self.w("#include <stdio.h>")
        self.w("#include <stdlib.h>")
        self.w("")
        self.gen_enums_consts()
        self.gen_type_defs()
        self.gen_type_meta()
        self.gen_constructors()
        self.gen_destroyers()
        self.gen_prototypes()
        self.gen_http_routes()
        self.gen_ws_routes()
        self.gen_user_funcs()
        self.gen_controllers()
        self.gen_handlers()
        self.gen_main()
        return "\n".join(self.lines) + "\n"

    def gen_enums_consts(self):
        for name, en in self.p.enums.items():
            auto = 0
            for mn, mv in en["members"]:
                macro = self.enum_macro(name, mn)
                if en["type"] == "String":
                    val = mv if isinstance(mv, str) else mn
                    self.w(f'#define {macro} "{c_escape(val)}"')
                else:
                    if mv is None:
                        val = auto
                    else:
                        val = mv
                    self.w(f"#define {macro} {val}")
                    auto = val + 1
        for name, d in self.p.consts.items():
            ty = d.type.name
            if ty == "String":
                self.w(f'#define {name} well_string_lit("{c_escape(getattr(d.value, "value", ""))}")')
            else:
                self.w(f"#define {name} ({self.gen_expr(d.value)})")
        self.w("")

    def gen_type_defs(self):
        for name in self.type_ids:
            self.w(f"typedef struct {name} {name};")
        self.w("")
        for name, tid in self.type_ids.items():
            fields = self.p.structs[name]["fields"]
            self.w(f"struct {name} {{")
            self.ind += 1
            self.w("WellObj hdr;")
            for f in fields:
                self.w(f"{self.ctype(f.type.name)} {f.name};")
            self.ind -= 1
            self.w("};")
            self.w(f"#define T_{name} {tid}")
        self.w("")

    def gen_constructors(self):
        for name in self.type_ids:
            kind = self.type_kind[name]
            self.w(f"static {name}* new_{name}(WellCtx* ctx, long long oid);")
        self.w("")
        for name in self.type_ids:
            kind = self.type_kind[name]
            fields = self.p.structs[name]["fields"]
            self.w(f"static {name}* new_{name}(WellCtx* ctx, long long oid) {{")
            self.ind += 1
            self.w("(void)ctx;")
            if kind == "event":
                self.w("if (oid == 0) oid = well_id_util_get_id();")
            self.w(f"{name}* o = ({name}*)well_new(sizeof({name}), T_{name}, oid);")
            for f in fields:
                ty = f.type.name
                if f.default is not None:
                    val = f.default
                    if self.is_ref(ty):
                        self.w(f"well_set_ref((void*)o, (void**)&o->{f.name}, (void*)({self.gen_expr(val)}));")
                    else:
                        self.w(f"o->{f.name} = {self.gen_expr(val)};")
                elif ty == "String":
                    self.w(f'o->{f.name} = well_string_lit("");')
            if kind == "event":
                self.w("well_freeze((void*)o);")
            self.w("return o;")
            self.ind -= 1
            self.w("}")
        self.w("")

    def meta_field_info(self, ty: str):
        """Return (kind, type_id_expr, elem_kind, elem_type_id_expr)."""
        if ty == "int":
            return "WELL_FIELD_INT", "0", "0", "0"
        if ty == "long":
            return "WELL_FIELD_LONG", "0", "0", "0"
        if ty in ("float", "double"):
            return "WELL_FIELD_DOUBLE", "0", "0", "0"
        if ty == "bool":
            return "WELL_FIELD_BOOL", "0", "0", "0"
        if ty == "String":
            return "WELL_FIELD_STRING", "WELL_T_STRING", "0", "0"
        if self.is_container(ty):
            ckind, et = self.container_info(ty)
            cconst = {"array": "WELL_T_ARRAY", "map": "WELL_T_MAP",
                      "linked": "WELL_T_LINKED"}[ckind]
            fkind = {"array": "WELL_FIELD_ARRAY", "map": "WELL_FIELD_MAP",
                     "linked": "WELL_FIELD_LINKED"}[ckind]
            ek, etid, _, _ = self.meta_field_info(et)
            return fkind, cconst, ek, etid
        if ty in self.type_ids:
            return "WELL_FIELD_REF", f"T_{ty}", "0", "0"
        return "WELL_FIELD_INT", "0", "0", "0"

    def gen_type_meta(self):
        for name in self.type_ids:
            fields = self.p.structs[name]["fields"]
            if fields:
                self.w(f"static const WellFieldDesc fields_{name}[] = {{")
                self.ind += 1
                for f in fields:
                    kind, tid, ek, etid = self.meta_field_info(f.type.name)
                    self.w(f'{{"{f.name}", offsetof({name}, {f.name}), '
                           f'{kind}, {tid}, {ek}, {etid}}},')
                self.ind -= 1
                self.w("};")
            self.w(f"static const WellTypeDesc desc_{name} = {{")
            self.ind += 1
            self.w(f'"{name}", T_{name}, sizeof({name}), '
                   f'{1 if self.type_kind[name] == "event" else 0},')
            self.w(f'{len(fields)}, {"fields_" + name if fields else "NULL"}')
            self.ind -= 1
            self.w("};")
            self.w("")

    def gen_destroyers(self):
        for name in self.type_ids:
            fields = self.p.structs[name]["fields"]
            self.w(f"static void destroy_{name}(void* p) {{")
            self.ind += 1
            self.w(f"{name}* o = ({name}*)p;")
            for f in fields:
                if self.is_ref(f.type.name):
                    self.w(f"well_release((void*)o->{f.name});")
            self.w("well_obj_free(o);")
            self.ind -= 1
            self.w("}")
        self.w("")

    def gen_prototypes(self):
        for name, f in self.p.funcs.items():
            ret = self.ctype(f.ret.name) if f.ret else "void"
            params = ", ".join(["WellCtx* ctx"] + [f"{self.ctype(t.name)} {n}" for n, t in f.params])
            self.w(f"static {ret} {name}({params});")
        for name in self.p.controllers:
            self.w(f"static void {name}_ensure(WellCtx* ctx);")
            self.w(f"static void {name}_stop(WellCtx* ctx);")
            self.w(f"static void {name}_run(WellCtx* ctx);")
        for name, h in self.p.handlers.items():
            ev = self.p.handler_event[name].name
            self.w(f"static void {name}_onEvent(WellCtx* ctx, void* evp);")
        self.w("")

    def gen_http_routes(self):
        routes = getattr(self.p, "http_routes", [])
        for i, r in enumerate(routes):
            ev = r["event"]
            h = r["handler"]
            self.w(f"static void http_route_{i}(WellCtx* ctx, WellHttpRequest* req) {{")
            self.ind += 1
            self.w(f"{ev}* e = new_{ev}(ctx, req->id);")
            self.w("e->reqId = req->id;")
            for fn in ("method", "path", "query", "body"):
                self.w(f"well_set_ref((void*)e, (void**)&e->{fn}, (void*)req->{fn});")
            self.w(f"{h}_onEvent(ctx, (void*)e);")
            self.w("well_release((void*)e);")
            self.ind -= 1
            self.w("}")
            self.w("")

    def gen_ws_routes(self):
        routes = getattr(self.p, "ws_routes", [])
        for i, r in enumerate(routes):
            oe, me, ce = r["open_ev"], r["msg_ev"], r["close_ev"]
            oh, mh, ch = r["open"], r["msg"], r["close"]
            self.w(f"static void ws_open_{i}(WellCtx* ctx, WellWsClient* c) {{")
            self.ind += 1
            self.w(f"{oe}* e = new_{oe}(ctx, c->id);")
            self.w("e->clientId = c->id;")
            self.w("well_set_ref((void*)e, (void**)&e->path, (void*)c->path);")
            self.w("well_set_ref((void*)e, (void**)&e->query, (void*)c->query);")
            self.w(f"{oh}_onEvent(ctx, (void*)e);")
            self.w("well_release((void*)e);")
            self.ind -= 1
            self.w("}")
            self.w(f"static void ws_msg_{i}(WellCtx* ctx, WellWsClient* c, WellString* text) {{")
            self.ind += 1
            self.w(f"{me}* e = new_{me}(ctx, c->id);")
            self.w("e->clientId = c->id;")
            self.w("well_set_ref((void*)e, (void**)&e->text, (void*)text);")
            self.w(f"{mh}_onEvent(ctx, (void*)e);")
            self.w("well_release((void*)e);")
            self.ind -= 1
            self.w("}")
            self.w(f"static void ws_close_{i}(WellCtx* ctx, WellWsClient* c, int code, WellString* reason) {{")
            self.ind += 1
            self.w(f"{ce}* e = new_{ce}(ctx, c->id);")
            self.w("e->clientId = c->id;")
            self.w("e->code = code;")
            self.w("well_set_ref((void*)e, (void**)&e->reason, (void*)reason);")
            self.w(f"{ch}_onEvent(ctx, (void*)e);")
            self.w("well_release((void*)e);")
            self.ind -= 1
            self.w("}")
            self.w("")

    def gen_user_funcs(self):
        for name, f in self.p.funcs.items():
            ret = self.ctype(f.ret.name) if f.ret else "void"
            params = ", ".join(["WellCtx* ctx"] + [f"{self.ctype(t.name)} {n}" for n, t in f.params])
            self.w(f"static {ret} {name}({params}) {{")
            self.ind += 1
            self.w("(void)ctx;")
            self.env = {n: t.name for n, t in f.params}
            self.current_ret = f.ret.name if f.ret else "void"
            m = self.next_id()
            mk = f"_m{m}"
            self.w(f"int {mk} = well_ctx_mark(ctx);")
            self.push_scope(mk, False)
            self.gen_block(f.body)
            self.emit_scope_release(mk)
            self.pop_scope(mk)
            self.ind -= 1
            self.w("}")
            self.w("")

    def gen_controllers(self):
        for name, c in self.p.controllers.items():
            run = None
            for m in c.methods:
                if m.name == "run":
                    run = m
                    break
            self.w(f"static int {name}_started = 0;")
            self.w(f"static void {name}_ensure(WellCtx* ctx) {{")
            self.ind += 1
            self.w(f"if ({name}_started) return;")
            self.w(f"{name}_started = 1;")
            self.w(f"well_owner_push({self.controller_ids[name]});")
            self.w(f"{name}_run(ctx);")
            self.w("well_owner_pop();")
            self.ind -= 1
            self.w("}")
            self.w(f"static void {name}_stop(WellCtx* ctx) {{")
            self.ind += 1
            self.w("(void)ctx;")
            self.w(f"if (!{name}_started) return;")
            self.w(f"{name}_started = 0;")
            self.w(f"well_off_owner({self.controller_ids[name]});")
            self.ind -= 1
            self.w("}")
            self.w(f"static void {name}_run(WellCtx* ctx) {{")
            self.ind += 1
            self.env = {}
            self.current_ret = "void"
            m = self.next_id()
            mk = f"_m{m}"
            self.w(f"int {mk} = well_ctx_mark(ctx);")
            self.push_scope(mk, False)
            if run:
                self.gen_block(run.body)
            self.emit_scope_release(mk)
            self.pop_scope(mk)
            self.ind -= 1
            self.w("}")
            self.w("")

    def gen_handlers(self):
        for name, h in self.p.handlers.items():
            ev = self.p.handler_event[name].name
            mname = self.p.handler_method[name]
            m = None
            for mm in h.methods:
                if mm.name == mname:
                    m = mm
                    break
            self.w(f"static void {name}_onEvent(WellCtx* ctx, void* evp) {{")
            self.ind += 1
            self.w(f"{ev}* e = ({ev}*)evp;")
            self.w("long long oid = ctx->oid;")
            self.w("(void)e;")
            self.w("(void)oid;")
            self.env = {m.params[0][0]: ev, "oid": "long"}
            self.current_ret = "void"
            mmark = self.next_id()
            mk = f"_m{mmark}"
            self.w(f"int {mk} = well_ctx_mark(ctx);")
            self.push_scope(mk, False)
            if m:
                self.gen_block(m.body)
            self.emit_scope_release(mk)
            self.pop_scope(mk)
            self.ind -= 1
            self.w("}")
            self.w("")

    def gen_main(self):
        self.w("int main(void) {")
        self.ind += 1
        self.w("well_init();")
        if self.p.uses_http:
            self.w("well_module_register(&well_http_module);")
        if self.p.uses_ws:
            self.w("well_module_register(&well_ws_module);")
        if self.p.uses_mysql:
            self.w("well_module_register(&well_mysql_module);")
        self.w("well_module_init_all();")
        for name in self.type_ids:
            self.w(f"well_register_type(T_{name}, destroy_{name});")
            self.w(f"well_register_type_desc(&desc_{name});")
        if not self.entry:
            self.w('fprintf(stderr, "no entry controller\\n");')
            self.w("return 1;")
        else:
            self.w("{")
            self.ind += 1
            self.w("WellCtx ctx;")
            self.w("well_ctx_init(&ctx, 0);")
            self.w(f"{self.entry}_ensure(&ctx);")
            self.w("well_ctx_clear(&ctx);")
            self.ind -= 1
            self.w("}")
            self.w("well_run_loop();")
        self.w("well_shutdown();")
        self.w("return 0;")
        self.ind -= 1
        self.w("}")
