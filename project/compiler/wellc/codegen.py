"""Well -> C99 code generator (MVP)."""
from dataclasses import fields, is_dataclass
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


# ---------- C identifier mangling ----------
# Every Well identifier that becomes a C symbol gets a category prefix so that
# Well names such as `register`, `goto`, `ctx`, `main`, `printf`, `well_new` or
# `WellCtx` cannot collide with C keywords, libc or the runtime.  The names
# visible to Well code (type descriptor strings, serialized/JSON field names,
# diagnostics) always keep the original Well spelling.
def c_var(name: str) -> str:
    return "v_" + name


def c_field(name: str) -> str:
    return "f_" + name


def c_func(name: str) -> str:
    return "wf_" + name


def c_type(name: str) -> str:
    return "wt_" + name


def c_ctl(name: str) -> str:
    return "ctl_" + name


def c_handler(name: str) -> str:
    return "hd_" + name


def c_enum(name: str) -> str:
    return "E_" + name


def c_const(name: str) -> str:
    return "C_" + name


def c_static_field(owner: str, name: str) -> str:
    return "wst_" + owner + "__" + name



class CodeGen:
    def __init__(self, prog: Program):
        self.p = prog
        self.lines: list[str] = []
        self.ind = 0
        self.env: dict[str, str] = {}
        self.c_names: dict[str, str] = {}
        # Reference bindings that own a stable frame-journal slot (name ->
        # generated slot variable).  Assignments use the slot index instead of
        # searching the journal by old pointer address.
        self.journaled: dict[str, str] = {}
        self.current_ret = "void"
        self.type_ids: dict[str, int] = {}
        self.type_kind: dict[str, str] = {}
        self.controller_ids: dict[str, int] = {}
        self.scope_stack: list[tuple] = []
        self.temp_used = 0
        self.temp_mark: str | None = None
        self.tracked = 0
        self.early_marks: set[str] = set()
        self.continue_stack: list[str | None] = []
        self.stack_decls: set[int] = set()
        self.stack_vars: set[str] = set()
        # scope mark -> [(struct type, C storage)] for frame-allocated structs
        # with reference fields; destroyed at scope exit.
        self.stack_cleanup: dict[str, list[tuple[str, str]]] = {}
        self.stack_arrays: set[int] = set()
        self.stack_array_names: dict[str, int] = {}
        self.stack_array_storage: dict[str, str] = {}
        self.stack_maps: set[int] = set()
        self.stack_map_keys: dict[str, dict[str, int]] = {}
        self.stack_map_storage: dict[str, str] = {}
        self.direct_push_names: set[str] = set()
        self.direct_str_push_names: set[str] = set()
        self.direct_fresh_push_names: set[str] = set()
        self.direct_map_names: set[str] = set()
        self.direct_str_map_names: set[str] = set()
        self.direct_fresh_map_names: set[str] = set()
        self.direct_map_keys: dict[str, dict[str, int]] = {}
        self.assign_ids()
        self.find_stack_decls()
        self.find_stack_arrays()
        self.find_direct_push_arrays()
        self.find_direct_string_push_arrays()
        self.find_direct_fresh_push_arrays()
        self.find_direct_map_writes()
        self.find_direct_string_map_writes()
        self.find_direct_fresh_map_writes()
        self.find_stack_maps()
        self.entry = prog.entry
        self.ctx = "ctx"
        self.uid = 0

    def next_id(self) -> int:
        self.uid += 1
        return self.uid

    # ---------- helpers ----------
    def w(self, s=""):
        if "well_track" in s:
            self.tracked += 1
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

    # ---------- scalar stack allocation ----------
    def _stack_local_struct(self, ty: str) -> bool:
        """A struct that can live in the C frame without owning heap storage.

        Scalar fields are plain values; reference and container fields are
        allowed because the frame emits the type's destroy function at scope
        exit (which releases them and skips the free via WELL_FLAG_STACK).
        """
        decl = self.p.structs.get(ty)
        if not decl or self.type_kind.get(ty) == "event":
            return False
        for f in decl["fields"]:
            if f.default is not None:
                return False
        return True

    def _children(self, node):
        if node is None or not is_dataclass(node) or isinstance(node, type):
            return
        for f in fields(node):
            val = getattr(node, f.name)
            if isinstance(val, list):
                for x in val:
                    if isinstance(x, tuple):
                        for y in x:
                            if is_dataclass(y) and not isinstance(y, type):
                                yield y
                    elif is_dataclass(x) and not isinstance(x, type):
                        yield x
            elif is_dataclass(val) and not isinstance(val, type):
                yield val

    def _uses_only_fields(self, node, name: str) -> bool:
        """Conservative escape check: the object is only read/written via fields."""
        if node is None:
            return True
        if isinstance(node, Ident):
            return node.name != name
        if isinstance(node, Member):
            if isinstance(node.obj, Ident) and node.obj.name == name:
                return True
            return self._uses_only_fields(node.obj, name)
        if isinstance(node, Assign):
            t = node.target
            is_own_member = (isinstance(t, Member) and
                             isinstance(t.obj, Ident) and t.obj.name == name)
            if isinstance(t, Ident) and t.name == name:
                return False
            if not is_own_member and not self._uses_only_fields(t, name):
                return False
            return self._uses_only_fields(node.value, name)
        if isinstance(node, Call):
            if (isinstance(node.func, Member) and isinstance(node.func.obj, Ident)
                    and node.func.obj.name == name):
                return False
            if not self._uses_only_fields(node.func, name):
                return False
            return all(self._uses_only_fields(a, name) for a in node.args)
        for child in self._children(node):
            if not self._uses_only_fields(child, name):
                return False
        return True

    def find_stack_decls(self):
        """Find non-escaping ``var p = new Point()`` declarations for the C frame."""
        blocks: list = []

        def walk(node):
            if node is None:
                return
            if isinstance(node, Block):
                blocks.append(node)
            for child in self._children(node):
                walk(child)

        walk(self.p)

        def decl_count(node, name: str) -> int:
            n = 1 if isinstance(node, VarDecl) and node.name == name else 0
            if isinstance(node, ForEach) and node.var == name:
                n += 1
            for child in self._children(node):
                n += decl_count(child, name)
            return n

        for block in blocks:
            for stmt in block.stmts:
                if not isinstance(stmt, VarDecl) or not isinstance(stmt.init, New):
                    continue
                ty = stmt.init.type.name
                if not self._stack_local_struct(ty):
                    continue
                # Only when no nested/other declaration of this name is in
                # sight, so the name-based escape check cannot confuse the
                # declaration with a shadowed binding.
                if decl_count(block, stmt.name) != 1:
                    continue
                if self._uses_only_fields(block, stmt.name):
                    self.stack_decls.add(id(stmt))

    def _uses_only_array_reads(self, node, name: str, pushes=None) -> bool:
        """Array may only be indexed, or pushed with a scalar, without escaping."""
        if node is None:
            return True
        if isinstance(node, Ident):
            return node.name != name
        if isinstance(node, Index):
            if isinstance(node.obj, Ident) and node.obj.name == name:
                return self._uses_only_array_reads(node.index, name, pushes)
            return (self._uses_only_array_reads(node.obj, name, pushes) and
                    self._uses_only_array_reads(node.index, name, pushes))
        if isinstance(node, Member):
            if isinstance(node.obj, Ident) and node.obj.name == name:
                return False
            return self._uses_only_array_reads(node.obj, name, pushes)
        if isinstance(node, Assign):
            t = node.target
            if isinstance(t, Index) and isinstance(t.obj, Ident) and t.obj.name == name:
                # A write can grow the inline array to the heap; disallow so a
                # stack array can never own heap storage without a cleanup path.
                return False
            if isinstance(t, Ident) and t.name == name:
                return False
            return (self._uses_only_array_reads(t, name, pushes) and
                    self._uses_only_array_reads(node.value, name, pushes))
        if isinstance(node, Call):
            if (isinstance(node.func, Member) and isinstance(node.func.obj, Ident)
                    and node.func.obj.name == name):
                if (pushes is not None and
                        getattr(node, "method_op", None) == "array_push" and
                        len(node.args) == 1):
                    pushes.append(node)
                    return self._uses_only_array_reads(node.args[0], name, pushes)
                return False
            if not self._uses_only_array_reads(node.func, name, pushes):
                return False
            return all(self._uses_only_array_reads(a, name, pushes) for a in node.args)
        for child in self._children(node):
            if not self._uses_only_array_reads(child, name, pushes):
                return False
        return True

    def _contains_loop(self, node) -> bool:
        if isinstance(node, (For, While, ForEach)):
            return True
        for child in self._children(node):
            if self._contains_loop(child):
                return True
        return False

    def find_stack_arrays(self):
        counts: dict[str, int] = {}
        blocks: list = []

        def walk(node):
            if node is None:
                return
            if isinstance(node, Block):
                blocks.append(node)
            if isinstance(node, VarDecl):
                counts[node.name] = counts.get(node.name, 0) + 1
            for child in self._children(node):
                walk(child)

        walk(self.p)
        for block in blocks:
            for stmt in block.stmts:
                if not isinstance(stmt, VarDecl) or not isinstance(stmt.init, ArrayLit):
                    continue
                ty = stmt.type.name if stmt.type else None
                kind, et = self.container_info(ty or "")
                if (kind != "array" or len(stmt.init.items) > 4 or
                        counts.get(stmt.name, 0) != 1):
                    continue
                if self.is_ref(et):
                    # Only immortal string literals are safe without a release path.
                    if et != "String" or not all(isinstance(it, StrLit) for it in stmt.init.items):
                        continue
                pushes = []
                if (self._uses_only_array_reads(block, stmt.name, pushes) and
                        len(stmt.init.items) + len(pushes) <= 4 and
                        (not pushes or (not self.is_ref(et) and not self._contains_loop(block)))):
                    self.stack_arrays.add(id(stmt))

    def _array_pushes(self, node, name: str, pushes: list) -> bool:
        if node is None:
            return True
        if isinstance(node, Assign) and isinstance(node.target, Ident) and node.target.name == name:
            return False
        if isinstance(node, Call):
            if (getattr(node, "method_op", None) == "array_push" and
                    isinstance(node.method_obj, Ident) and node.method_obj.name == name):
                pushes.append(1)
            if not all(self._array_pushes(a, name, pushes) for a in node.args):
                return False
            return self._array_pushes(node.func, name, pushes)
        for child in self._children(node):
            if not self._array_pushes(child, name, pushes):
                return False
        return True

    def find_direct_push_arrays(self):
        counts: dict[str, int] = {}
        blocks: list = []

        def walk(node):
            if node is None:
                return
            if isinstance(node, Block):
                blocks.append(node)
            if isinstance(node, VarDecl):
                counts[node.name] = counts.get(node.name, 0) + 1
            for child in self._children(node):
                walk(child)

        walk(self.p)
        for block in blocks:
            if self._contains_loop(block):
                continue
            for stmt in block.stmts:
                if not isinstance(stmt, VarDecl) or not isinstance(stmt.init, ArrayLit):
                    continue
                ty = stmt.type.name if stmt.type else None
                kind, et = self.container_info(ty or "")
                if (kind != "array" or self.is_ref(et) or
                        len(stmt.init.items) > 4 or counts.get(stmt.name, 0) != 1):
                    continue
                pushes = []
                if (self._array_pushes(block, stmt.name, pushes) and
                        len(stmt.init.items) + len(pushes) <= 4):
                    self.direct_push_names.add(stmt.name)

    def _map_writes(self, node, name: str, writes: list) -> bool:
        if node is None:
            return True
        if isinstance(node, Assign):
            t = node.target
            if isinstance(t, Index) and isinstance(t.obj, Ident) and t.obj.name == name:
                if not isinstance(t.index, StrLit):
                    return False
                writes.append(t.index.value)
                return self._map_writes(node.value, name, writes)
            if isinstance(t, Ident) and t.name == name:
                return False
            return (self._map_writes(t, name, writes) and
                    self._map_writes(node.value, name, writes))
        for child in self._children(node):
            if not self._map_writes(child, name, writes):
                return False
        return True

    def _array_string_pushes(self, node, name: str, pushes: list) -> bool:
        if node is None:
            return True
        if isinstance(node, Assign) and isinstance(node.target, Ident) and node.target.name == name:
            return False
        if isinstance(node, Call):
            if (getattr(node, "method_op", None) == "array_push" and
                    isinstance(node.method_obj, Ident) and node.method_obj.name == name):
                if len(node.args) != 1 or not isinstance(node.args[0], StrLit):
                    return False
                pushes.append(1)
            if not all(self._array_string_pushes(a, name, pushes) for a in node.args):
                return False
            return self._array_string_pushes(node.func, name, pushes)
        for child in self._children(node):
            if not self._array_string_pushes(child, name, pushes):
                return False
        return True

    def find_direct_string_push_arrays(self):
        counts: dict[str, int] = {}
        blocks: list = []

        def walk(node):
            if node is None:
                return
            if isinstance(node, Block):
                blocks.append(node)
            if isinstance(node, VarDecl):
                counts[node.name] = counts.get(node.name, 0) + 1
            for child in self._children(node):
                walk(child)

        walk(self.p)
        for block in blocks:
            if self._contains_loop(block):
                continue
            for stmt in block.stmts:
                if not isinstance(stmt, VarDecl) or not isinstance(stmt.init, ArrayLit):
                    continue
                ty = stmt.type.name if stmt.type else None
                kind, et = self.container_info(ty or "")
                if (kind != "array" or et != "String" or
                        len(stmt.init.items) > 4 or counts.get(stmt.name, 0) != 1):
                    continue
                if not all(isinstance(it, StrLit) for it in stmt.init.items):
                    continue
                pushes = []
                if (self._array_string_pushes(block, stmt.name, pushes) and
                        len(stmt.init.items) + len(pushes) <= 4):
                    self.direct_str_push_names.add(stmt.name)

    def _map_string_writes(self, node, name: str, writes: list) -> bool:
        if node is None:
            return True
        if isinstance(node, Assign):
            t = node.target
            if isinstance(t, Index) and isinstance(t.obj, Ident) and t.obj.name == name:
                if not isinstance(t.index, StrLit) or not isinstance(node.value, StrLit):
                    return False
                writes.append(t.index.value)
                return True
            if isinstance(t, Ident) and t.name == name:
                return False
            return (self._map_string_writes(t, name, writes) and
                    self._map_string_writes(node.value, name, writes))
        for child in self._children(node):
            if not self._map_string_writes(child, name, writes):
                return False
        return True

    def find_direct_string_map_writes(self):
        counts: dict[str, int] = {}
        blocks: list = []

        def walk(node):
            if node is None:
                return
            if isinstance(node, Block):
                blocks.append(node)
            if isinstance(node, VarDecl):
                counts[node.name] = counts.get(node.name, 0) + 1
            for child in self._children(node):
                walk(child)

        walk(self.p)
        for block in blocks:
            if self._contains_loop(block):
                continue
            for stmt in block.stmts:
                if not isinstance(stmt, VarDecl) or not isinstance(stmt.init, ObjectLit):
                    continue
                ty = stmt.type.name if stmt.type else None
                kind, et = self.container_info(ty or "")
                if (kind != "map" or et != "String" or
                        len(stmt.init.pairs) > 4 or counts.get(stmt.name, 0) != 1):
                    continue
                if not all(isinstance(v, StrLit) for _k, v in stmt.init.pairs):
                    continue
                writes = []
                if (self._map_string_writes(block, stmt.name, writes) and
                        len(stmt.init.pairs) + len(writes) <= 4):
                    self.direct_str_map_names.add(stmt.name)

    def _array_fresh_pushes(self, node, name: str, pushes: list) -> bool:
        if node is None:
            return True
        if isinstance(node, Assign) and isinstance(node.target, Ident) and node.target.name == name:
            return False
        if isinstance(node, Call):
            if (getattr(node, "method_op", None) == "array_push" and
                    isinstance(node.method_obj, Ident) and node.method_obj.name == name):
                if len(node.args) != 1 or not isinstance(node.args[0], New):
                    return False
                pushes.append(1)
            if not all(self._array_fresh_pushes(a, name, pushes) for a in node.args):
                return False
            return self._array_fresh_pushes(node.func, name, pushes)
        for child in self._children(node):
            if not self._array_fresh_pushes(child, name, pushes):
                return False
        return True

    def find_direct_fresh_push_arrays(self):
        counts: dict[str, int] = {}
        blocks: list = []

        def walk(node):
            if node is None:
                return
            if isinstance(node, Block):
                blocks.append(node)
            if isinstance(node, VarDecl):
                counts[node.name] = counts.get(node.name, 0) + 1
            for child in self._children(node):
                walk(child)

        walk(self.p)
        for block in blocks:
            if self._contains_loop(block):
                continue
            for stmt in block.stmts:
                if not isinstance(stmt, VarDecl) or not isinstance(stmt.init, ArrayLit):
                    continue
                ty = stmt.type.name if stmt.type else None
                kind, et = self.container_info(ty or "")
                if (kind != "array" or not self.is_ref(et) or et in ("String", "Bytes") or
                        len(stmt.init.items) > 4 or counts.get(stmt.name, 0) != 1):
                    continue
                if not all(isinstance(it, New) for it in stmt.init.items):
                    continue
                pushes = []
                if (self._array_fresh_pushes(block, stmt.name, pushes) and
                        len(stmt.init.items) + len(pushes) <= 4):
                    self.direct_fresh_push_names.add(stmt.name)

    def _map_fresh_writes(self, node, name: str, writes: list) -> bool:
        if node is None:
            return True
        if isinstance(node, Assign):
            t = node.target
            if isinstance(t, Index) and isinstance(t.obj, Ident) and t.obj.name == name:
                if not isinstance(t.index, StrLit) or not isinstance(node.value, New):
                    return False
                writes.append(t.index.value)
                return True
            if isinstance(t, Ident) and t.name == name:
                return False
            return (self._map_fresh_writes(t, name, writes) and
                    self._map_fresh_writes(node.value, name, writes))
        for child in self._children(node):
            if not self._map_fresh_writes(child, name, writes):
                return False
        return True

    def find_direct_fresh_map_writes(self):
        counts: dict[str, int] = {}
        blocks: list = []

        def walk(node):
            if node is None:
                return
            if isinstance(node, Block):
                blocks.append(node)
            if isinstance(node, VarDecl):
                counts[node.name] = counts.get(node.name, 0) + 1
            for child in self._children(node):
                walk(child)

        walk(self.p)
        for block in blocks:
            if self._contains_loop(block):
                continue
            for stmt in block.stmts:
                if not isinstance(stmt, VarDecl) or not isinstance(stmt.init, ObjectLit):
                    continue
                ty = stmt.type.name if stmt.type else None
                kind, et = self.container_info(ty or "")
                if (kind != "map" or not self.is_ref(et) or et in ("String", "Bytes") or
                        len(stmt.init.pairs) > 4 or counts.get(stmt.name, 0) != 1):
                    continue
                if not all(isinstance(v, New) for _k, v in stmt.init.pairs):
                    continue
                writes = []
                if (self._map_fresh_writes(block, stmt.name, writes) and
                        len(stmt.init.pairs) + len(writes) <= 4):
                    self.direct_fresh_map_names.add(stmt.name)

    def find_direct_map_writes(self):
        counts: dict[str, int] = {}
        blocks: list = []

        def walk(node):
            if node is None:
                return
            if isinstance(node, Block):
                blocks.append(node)
            if isinstance(node, VarDecl):
                counts[node.name] = counts.get(node.name, 0) + 1
            for child in self._children(node):
                walk(child)

        walk(self.p)
        for block in blocks:
            if self._contains_loop(block):
                continue
            for stmt in block.stmts:
                if not isinstance(stmt, VarDecl) or not isinstance(stmt.init, ObjectLit):
                    continue
                ty = stmt.type.name if stmt.type else None
                kind, et = self.container_info(ty or "")
                if (kind != "map" or self.is_ref(et) or
                        len(stmt.init.pairs) > 4 or counts.get(stmt.name, 0) != 1):
                    continue
                writes = []
                if (self._map_writes(block, stmt.name, writes) and
                        len(stmt.init.pairs) + len(writes) <= 4):
                    self.direct_map_names.add(stmt.name)

    def _uses_only_map_reads(self, node, name: str, writes=None) -> bool:
        if node is None:
            return True
        if isinstance(node, Ident):
            return node.name != name
        if isinstance(node, Index):
            if isinstance(node.obj, Ident) and node.obj.name == name:
                return self._uses_only_map_reads(node.index, name, writes)
            return (self._uses_only_map_reads(node.obj, name, writes) and
                    self._uses_only_map_reads(node.index, name, writes))
        if isinstance(node, Member):
            if isinstance(node.obj, Ident) and node.obj.name == name:
                return False
            return self._uses_only_map_reads(node.obj, name, writes)
        if isinstance(node, Assign):
            t = node.target
            if (isinstance(t, Index) and isinstance(t.obj, Ident)
                    and t.obj.name == name):
                if writes is None or not isinstance(t.index, StrLit):
                    return False
                writes.append(t.index.value)
                return self._uses_only_map_reads(node.value, name, writes)
            if isinstance(t, Ident) and t.name == name:
                return False
            return (self._uses_only_map_reads(t, name, writes) and
                    self._uses_only_map_reads(node.value, name, writes))
        if isinstance(node, Call):
            if (isinstance(node.func, Member) and isinstance(node.func.obj, Ident)
                    and node.func.obj.name == name):
                return False
            if not self._uses_only_map_reads(node.func, name, writes):
                return False
            return all(self._uses_only_map_reads(a, name, writes) for a in node.args)
        for child in self._children(node):
            if not self._uses_only_map_reads(child, name, writes):
                return False
        return True

    def find_stack_maps(self):
        counts: dict[str, int] = {}
        blocks: list = []

        def walk(node):
            if node is None:
                return
            if isinstance(node, Block):
                blocks.append(node)
            if isinstance(node, VarDecl):
                counts[node.name] = counts.get(node.name, 0) + 1
            for child in self._children(node):
                walk(child)

        walk(self.p)
        for block in blocks:
            for stmt in block.stmts:
                if not isinstance(stmt, VarDecl) or not isinstance(stmt.init, ObjectLit):
                    continue
                ty = stmt.type.name if stmt.type else None
                kind, et = self.container_info(ty or "")
                if (kind != "map" or self.is_ref(et) or
                        len(stmt.init.pairs) > 4 or counts.get(stmt.name, 0) != 1):
                    continue
                writes = []
                if (self._uses_only_map_reads(block, stmt.name, writes) and
                        len(stmt.init.pairs) + len(writes) <= 4 and
                        (not writes or not self._contains_loop(block))):
                    self.stack_maps.add(id(stmt))

    def ctype(self, ty: str) -> str:
        if ty in ("int", "bool"):
            return "int"
        if ty == "long":
            return "long long"
        if ty in ("float", "double"):
            return "double"
        if ty == "String":
            return "WellString*"
        if ty == "Bytes":
            return "WellBytes*"
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
            return f"{c_type(ty)}*"
        return ty

    def is_ref(self, ty: str) -> bool:
        return ty in ("String", "Bytes") or ty in self.type_ids or self.is_container(ty)

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

    def gen_arg_temp(self, e) -> str:
        """Generate a ref argument; owned temporaries are held until frame end."""
        code = self.gen_expr(e)
        if self.is_owned(e) and self.is_ref(getattr(e, "ty", "void")):
            self.temp_used += 1
            return f"well_temp_hold((void*)({code}))"
        return code

    def gen_str_arg(self, e) -> str:
        """Generate a String argument, journaling owned temporaries.

        Most runtime entry points only borrow a ``WellString*`` for the duration
        of the call.  Passing a freshly built string (``a + b``, ``f()`` …)
        directly leaked it once per call; journaling it releases at the end of
        the enclosing scope instead.
        """
        if getattr(e, "ty", None) == "String":
            return self.gen_arg_temp(e)
        return self.gen_expr(e)

    def source_stmt(self, s) -> str | None:
        """`well_source_set` call for a statement, or None when unknown."""
        line = getattr(s, "line", None)
        src = getattr(s, "file", None)
        if line and src:
            return f'well_source_set("{c_escape(str(src))}", {line})'
        return None

    def expr_may_panic(self, e) -> bool:
        """Conservative: does evaluating `e` have a runtime panic path?"""
        if e is None:
            return False
        if isinstance(e, (Call, New)):
            return True
        if isinstance(e, Unary):
            return self.expr_may_panic(e.expr)
        if isinstance(e, Binary):
            if e.op in ("/", "%"):
                # A non-zero constant divisor cannot panic.  Treating every
                # division as panic-capable made hot loops emit a per-iteration
                # well_source_set() for e.g. `i % 1024` (two TLS writes).
                if isinstance(e.right, IntLit) and e.right.value != 0:
                    return self.expr_may_panic(e.left) or self.expr_may_panic(e.right)
                return True
            if e.op == "+" and (getattr(e.left, "ty", None) == "String" or
                                getattr(e.right, "ty", None) == "String"):
                return True
            return self.expr_may_panic(e.left) or self.expr_may_panic(e.right)
        if isinstance(e, Index):
            return self.expr_may_panic(e.obj) or self.expr_may_panic(e.index)
        if isinstance(e, Member):
            return self.expr_may_panic(e.obj)
        if isinstance(e, ArrayLit):
            return any(self.expr_may_panic(x) for x in e.items)
        if isinstance(e, ObjectLit):
            return any(self.expr_may_panic(v) for _, v in e.pairs)
        return False

    def stmt_may_panic(self, s) -> bool:
        if isinstance(s, VarDecl):
            return self.expr_may_panic(s.init)
        if isinstance(s, Assign):
            if isinstance(s.target, (Member, Index)):
                return True  # oid / ownership check on the write
            return self.expr_may_panic(s.value)
        if isinstance(s, ExprStmt):
            return self.expr_may_panic(s.expr)
        if isinstance(s, Return):
            return self.expr_may_panic(s.value)
        if isinstance(s, Switch):
            return self.expr_may_panic(s.expr)
        if isinstance(s, ForEach):
            return self.expr_may_panic(s.iter)
        if isinstance(s, (If, While, For)):
            return self.expr_may_panic(s.cond)
        return False

    def is_const_init(self, e) -> bool:
        """True when `e` is usable as a compile-time default value.

        Field defaults are emitted inside the generated constructor, so a call or
        container literal there would need its own allocation/cleanup path.  We
        require literals/consts/enum members (the checker already enforces the
        same rule for static fields) and otherwise report an internal error
        instead of emitting invalid C.
        """
        if isinstance(e, (IntLit, FloatLit, StrLit, BoolLit, NullLit)):
            return True
        if isinstance(e, Unary) and e.op in ("-", "+", "~"):
            return self.is_const_init(e.expr)
        if isinstance(e, Binary) and e.op in ("+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>"):
            return self.is_const_init(e.left) and self.is_const_init(e.right)
        if isinstance(e, Ident) and e.name in getattr(self.p, "consts", {}):
            return True
        if isinstance(e, Member) and getattr(e, "is_enum", False):
            return True
        return False

    def require_const_default(self, e, where: str):
        if not self.is_const_init(e):
            raise Exception(f"{where}: default value must be a compile-time literal (got {type(e).__name__})")

    def container_ref_flag(self, et: str) -> str:
        return "1" if self.is_ref(et) else "0"

    def container_new_call(self, kind: str, et: str) -> str:
        # String/Bytes elements have no outbound references, so an Array/Map of
        # them cannot form a cycle: keep it out of the oid registry.
        fn = f"well_{kind}_new_flat" if et in ("String", "Bytes") else f"well_{kind}_new"
        return f"{fn}(ctx, {self.container_ref_flag(et)})"

    def sort_kind_code(self, et: str) -> str:
        if et in ("int", "bool"):
            return "WELL_FIELD_INT"
        if et == "long":
            return "WELL_FIELD_LONG"
        if et in ("float", "double"):
            return "WELL_FIELD_DOUBLE"
        return "WELL_FIELD_STRING"

    def json_elem_kind(self, et: str) -> str:
        if et == "int":
            return "WELL_FIELD_INT"
        if et == "long":
            return "WELL_FIELD_LONG"
        if et == "bool":
            return "WELL_FIELD_BOOL"
        if et in ("float", "double"):
            return "WELL_FIELD_DOUBLE"
        if et == "String":
            return "WELL_FIELD_STRING"
        return "WELL_FIELD_REF"

    def json_elem_type_id(self, et: str) -> str:
        return f"T_{c_type(et)}" if et in self.type_ids else "0"

    def elem_kind_code(self, et: str) -> str:
        """Runtime element compare kind for Array.indexOf/includes."""
        if et in ("int", "long", "bool"):
            return "0"
        if et in ("float", "double"):
            return "1"
        if et == "String":
            return "3"
        return "2"

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
        if isinstance(e, Call) and getattr(e, "cache_op", None) == "get":
            return True
        if isinstance(e, Call) and getattr(e, "builtin", None) in ("json_parse", "json_try_parse", "json_stringify", "file_read", "host_env", "mysql_query_json", "mysql_query_json_pool", "str_from_int", "str_from_codepoint"):
            return True
        if isinstance(e, Binary) and e.op == "+" and getattr(e, "ty", None) == "String":
            return True
        if isinstance(e, Call) and (getattr(e, "call_user", None) or getattr(e, "call_ns", None)):
            return getattr(e, "ty", "void") not in ("void",) and self.is_ref(getattr(e, "ty"))
        if isinstance(e, Call) and getattr(e, "method_op", None) == "array_pop":
            return self.is_ref(getattr(e, "ty", ""))
        if isinstance(e, Call) and getattr(e, "method_op", None) in ("array_slice", "array_concat", "array_join", "map_keys", "map_values"):
            return True
        if isinstance(e, Call) and getattr(e, "method_op", None) == "array_shift":
            return self.is_ref(getattr(e, "ty", ""))
        if isinstance(e, Index) and self.is_owned(e.obj) and self.is_ref(getattr(e, "ty", "")):
            return True
        if isinstance(e, Call) and getattr(e, "method_op", None) in ("linked_popHead", "linked_popTail"):
            return self.is_ref(getattr(e, "ty", ""))
        if isinstance(e, Call) and getattr(e, "method_op", None) in ("str_substring", "str_trim", "str_split", "str_to_lower", "str_to_upper", "str_replace"):
            return True
        if isinstance(e, Call) and getattr(e, "method_op", None) == "bytes_to_string":
            return True
        if isinstance(e, Call) and getattr(e, "builtin", None) == "str_from_byte":
            return True
        if isinstance(e, Binary) and e.op == "??":
            return self.is_owned(e.left) or self.is_owned(e.right)
        if isinstance(e, Member):
            if isinstance(e.obj, Ident) and e.obj.name in getattr(self.p, "statics", {}):
                return False
            if getattr(e, "is_enum", False):
                return False
            if not self.is_owned(e.obj):
                # Borrowed base: `x.field` is still borrowed (no retain emitted).
                return False
            oty = getattr(e.obj, "ty", None)
            fty = self.field_type(oty, e.name) if oty else None
            return fty is not None and self.is_ref(fty)
        return False

    def _unstable_ref(self, e: Expr) -> bool:
        """Borrowed reference whose owner may be released during a call.

        Locals/parameters own an edge for the frame and string literals are
        immortal, so they stay valid.  Field/index reads borrow from a
        container, static or base object that a later argument could mutate.
        """
        if isinstance(e, (Ident, StrLit, New)):
            return False
        if isinstance(e, (Member, Index)):
            return True
        return not self.is_owned(e)

    def enum_macro(self, enum: str, member: str) -> str:
        return f"{c_enum(enum)}_{member}"
    def gen_enum_helpers(self):
        for name, en in getattr(self.p, "enums", {}).items():
            members = en["members"]
            helper = c_enum(name)
            if en["type"] == "String":
                self.w(f"static WellString *{helper}__toString(WellString *v) {{")
                self.ind += 1
                self.w('if (!v) return well_string_lit("");')
                for mn, val in members:
                    self.w(f'if (well_string_eq(v, well_string_lit("{c_escape(str(val))}"))) return well_string_lit("{c_escape(mn)}");')
                self.w('return well_string_lit("");')
                self.ind -= 1
                self.w("}")
            else:
                self.w(f"static int {helper}__fromInt(long long v) {{")
                self.ind += 1
                self.w("switch (v) {")
                self.ind += 1
                for mn, val in members:
                    self.w(f"case {val}: return {val};")
                self.w("default: return 0;")
                self.ind -= 1
                self.w("}")
                self.ind -= 1
                self.w("}")
                self.w(f"static WellString *{helper}__toString(long long v) {{")
                self.ind += 1
                self.w("switch (v) {")
                self.ind += 1
                for mn, val in members:
                    self.w(f'case {val}: return well_string_lit("{c_escape(mn)}");')
                self.w('default: return well_string_lit("");')
                self.ind -= 1
                self.w("}")
                self.ind -= 1
                self.w("}")
            self.w("")

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
        self.c_names[name] = c_var(name)

    def bind_ref(self, name: str, cname: str, owned: int):
        """Give a reference binding a stable frame-journal slot.

        ``owned`` transfers an existing RC edge (fresh object); otherwise the
        frame retains the borrowed value so the binding survives mutation of
        the container/static it came from.  Every later assignment to this
        variable updates the same journal entry (``well_ctx_replace_at``), so
        aliases that hold the same address can never be confused with this
        binding (v3 journal-slots lesson).
        """
        slot = f"_js_{cname}"
        self.journaled[name] = slot
        self.w(f"int {slot} = well_ctx_bind(ctx, (void**)&{cname}, {owned});")
        self.tracked += 1

    def assigned_names(self, node) -> set[str]:
        """Names assigned (or compound-assigned) anywhere under ``node``."""
        out: set[str] = set()
        stack = [node]
        while stack:
            cur = stack.pop()
            if isinstance(cur, Assign) and isinstance(cur.target, Ident):
                out.add(cur.target.name)
            stack.extend(self._children(cur))
        return out

    def _returns_only_param(self, body, name: str) -> bool:
        """True for a body that is exactly ``return <name>;``.

        Such a callee cannot invalidate its borrowed input before the return
        path acquires the returned edge, so it needs no parameter journal slot.
        This keeps the identity-forward hot path free of RC traffic (the v3
        slot implementation measured a 16.4% regression without this carve-out).
        """
        if body is None or len(body.stmts) != 1:
            return False
        s = body.stmts[0]
        return (isinstance(s, Return) and isinstance(s.value, Ident)
                and s.value.name == name)

    def set_params_env(self, params):
        """Bind a function's parameters in the type and C-name environments."""
        self.env = {n: t.name for n, t in params}
        self.c_names = {n: c_var(n) for n, _t in params}
        self.journaled = {}

    def bind_params(self, params, body):
        """Journal reference parameters at function entry.

        The callee takes its own RC edge, so a borrowed argument stays valid
        even if the callee replaces the container/static it came from, and a
        reassignment inside the callee updates the parameter's own slot rather
        than a caller binding that happens to hold the same address.
        """
        for n, t in params:
            if not self.is_ref(t.name):
                continue
            if self._returns_only_param(body, n):
                continue
            self.bind_ref(n, self.c_names[n], 0)

    # ---------- expression ----------
    def gen_expr(self, e: Expr) -> str:
        if isinstance(e, IntLit):
            return str(e.value)
        if isinstance(e, FloatLit):
            return repr(float(e.value))
        if isinstance(e, StrLit):
            lit = c_escape(e.value)
            return ('({ static WellString *_ws_lit; '
                    'well_string_lit_slot(&_ws_lit, "' + lit + '"); })')
        if isinstance(e, BoolLit):
            return "1" if e.value else "0"
        if isinstance(e, NullLit):
            return "((void*)0)"
        if isinstance(e, Ident):
            if getattr(e, "raw_c", None):
                # Internal synthetic node: code is already a C expression.
                return e.raw_c
            if e.name in getattr(self.p, "consts", {}):
                return c_const(e.name)
            return self.c_names.get(e.name) or c_var(e.name)
        if isinstance(e, Member):
            if isinstance(e.obj, Ident) and e.obj.name in getattr(self.p, "statics", {}):
                return c_static_field(e.obj.name, e.name)
            if getattr(e, "is_enum", False) and isinstance(e.obj, Ident):
                return self.enum_macro(e.obj.name, e.name)
            if getattr(e, "str_len", False):
                if self.is_owned(e.obj):
                    return f"well_str_len_take({self.gen_expr(e.obj)})"
                return f"well_string_len({self.gen_expr(e.obj)})"
            if getattr(e, "container_len", None) == "array":
                if self.is_owned(e.obj):
                    return f"well_array_len_take({self.gen_expr(e.obj)})"
                return f"well_array_len({self.gen_expr(e.obj)})"
            if getattr(e, "container_len", None) == "map":
                return f"well_map_len({self.gen_expr(e.obj)})"
            if getattr(e, "container_len", None) == "linked":
                return f"well_linked_len({self.gen_expr(e.obj)})"
            if getattr(e, "container_len", None) == "bytes":
                if self.is_owned(e.obj):
                    return f"well_bytes_len_take({self.gen_expr(e.obj)})"
                return f"well_bytes_len({self.gen_expr(e.obj)})"
            if self.is_owned(e.obj):
                # `new X().field` / `Model.get(...).field` used to leak the base
                # object: hold it in a temporary, copy/retain the field, release it.
                oty = getattr(e.obj, "ty", None)
                fty = self.field_type(oty, e.name) if oty else "int"
                ct = self.ctype(oty) if oty else "void*"
                tmp = f"_mo{self.next_id()}"
                if self.is_ref(fty):
                    return (f"({{ {ct} {tmp} = {self.gen_expr(e.obj)}; "
                            f"{self.ctype(fty)} _mv = {tmp}->{c_field(e.name)}; "
                            f"well_retain((void*)_mv); well_release((void*){tmp}); _mv; }})")
                return (f"({{ {ct} {tmp} = {self.gen_expr(e.obj)}; "
                        f"{self.ctype(fty)} _mv = {tmp}->{c_field(e.name)}; "
                        f"well_release((void*){tmp}); _mv; }})")
            return f"({self.gen_expr(e.obj)})->{c_field(e.name)}"
        if isinstance(e, Index):
            ot = getattr(e.obj, "ty", None)
            if getattr(e, "str_index", False):
                if self.is_owned(e.obj):
                    return (f"well_str_byte_at_take({self.gen_expr(e.obj)}, "
                            f"(long long)({self.gen_expr(e.index)}))")
                return f"well_str_byte_at({self.gen_expr(e.obj)}, (long long)({self.gen_expr(e.index)}))"
            if getattr(e, "bytes_index", False):
                if self.is_owned(e.obj):
                    return (f"well_bytes_get_take({self.gen_expr(e.obj)}, "
                            f"(long long)({self.gen_expr(e.index)}))")
                return f"well_bytes_get({self.gen_expr(e.obj)}, (long long)({self.gen_expr(e.index)}))"
            kind, et = self.container_info(ot)
            obj = self.gen_expr(e.obj)
            idx = self.gen_str_arg(e.index) if kind == "map" else self.gen_expr(e.index)
            if (kind == "array" and isinstance(e.obj, Ident)
                    and e.obj.name in self.stack_array_names
                    and isinstance(e.index, IntLit)):
                n = self.stack_array_names[e.obj.name]
                ci = e.index.value
                if 0 <= ci < n and et in ("int", "long", "float", "double"):
                    field = "d" if et in ("float", "double") else "i"
                    return f"(({self.ctype(et)})(({obj})->items[{ci}].{field}))"
                if 0 <= ci < n and et == "String":
                    return f"((WellString*)(({obj})->items[{ci}].p))"
            if (kind == "map" and isinstance(e.obj, Ident)
                    and e.obj.name in self.stack_map_keys
                    and isinstance(e.index, StrLit)
                    and e.index.value in self.stack_map_keys[e.obj.name]
                    and et in ("int", "long", "float", "double")):
                ci = self.stack_map_keys[e.obj.name][e.index.value]
                field = "d" if et in ("float", "double") else "i"
                return f"(({self.ctype(et)})(({obj})->vals[{ci}].{field}))"
            if kind == "array":
                if self.is_owned(e.obj):
                    val = f"well_array_get_take({obj}, (long long)({idx}))"
                else:
                    val = f"well_array_get_fast({obj}, (long long)({idx}))"
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
            if getattr(e, "bytes_new", False):
                return "well_bytes_new(ctx)"
            kind, et = self.container_info(e.type.name)
            if kind:
                return self.container_new_call(kind, et)
            if e.init:
                raise Exception("new-with-init used outside supported statement context")
            oid = "ctx->oid"
            if self.type_kind.get(e.type.name) == "event" and e.args:
                oid = f"(long long)({self.gen_expr(e.args[0])})"
            return f"new_{c_type(e.type.name)}(ctx, {oid})"
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
            lo = 1 if self.is_owned(e.left) else 0
            ro = 1 if self.is_owned(e.right) else 0
            if lo or ro:
                return (f"well_string_concat_take({self.gen_expr(e.left)}, {lo}, "
                        f"{self.gen_expr(e.right)}, {ro})")
            return f"well_string_concat({self.gen_expr(e.left)}, {self.gen_expr(e.right)})"
        if e.op in ("==", "!=") and (lt == "String" or getattr(e.right, "ty", None) == "String"):
            lo = 1 if self.is_owned(e.left) else 0
            ro = 1 if self.is_owned(e.right) else 0
            if lo or ro:
                eq = (f"well_str_eq_take({self.gen_expr(e.left)}, {lo}, "
                      f"{self.gen_expr(e.right)}, {ro})")
            else:
                eq = f"well_string_eq({self.gen_expr(e.left)}, {self.gen_expr(e.right)})"
            return eq if e.op == "==" else f"(!{eq})"
        if e.op in ("/", "%") and getattr(e, "ty", None) in ("int", "long"):
            # Constant non-zero divisors (other than -1) cannot raise the
            # divide-by-zero / LLONG_MIN/-1 panic, so emit native C arithmetic
            # and let the C compiler fold it (e.g. `% 1024` -> `& 1023`).
            if isinstance(e.right, IntLit) and e.right.value not in (0, -1):
                cty = self.ctype(e.ty)
                return (f"(({cty})((long long)({self.gen_expr(e.left)}) {e.op} "
                        f"(long long)({e.right.value})))")
            fn = "well_div_checked" if e.op == "/" else "well_mod_checked"
            cty = self.ctype(e.ty)
            return (f"(({cty}){fn}((long long)({self.gen_expr(e.left)}), "
                    f"(long long)({self.gen_expr(e.right)})))")
        return f"({self.gen_expr(e.left)} {e.op} {self.gen_expr(e.right)})"

    def gen_call_expr(self, e: Call) -> str:
        if getattr(e, "builtin", None) == "mysql_connect":
            a = [self.gen_str_arg(x) for x in e.args]
            return (f"well_mysql_connect(well_string_cstr({a[0]}), (int)({a[1]}), "
                    f"well_string_cstr({a[2]}), well_string_cstr({a[3]}), well_string_cstr({a[4]}))")
        if getattr(e, "builtin", None) == "mysql_exec":
            return f"well_mysql_exec(well_string_cstr({self.gen_str_arg(e.args[0])}))"
        if getattr(e, "builtin", None) == "mysql_query_json":
            return f"well_mysql_query_json(well_string_cstr({self.gen_str_arg(e.args[0])}))"
        if getattr(e, "builtin", None) == "mysql_close":
            return "well_mysql_close()"
        if getattr(e, "builtin", None) == "mysql_pool":
            a = [self.gen_str_arg(x) for x in e.args]
            return (f"well_mysql_pool_configure(well_string_cstr({a[0]}), (int)({a[1]}), "
                    f"well_string_cstr({a[2]}), well_string_cstr({a[3]}), well_string_cstr({a[4]}), "
                    f"(int)({a[5]}), (int)({a[6]}))")
        if getattr(e, "builtin", None) == "mysql_exec_pool":
            return f"well_mysql_pool_exec_global(well_string_cstr({self.gen_str_arg(e.args[0])}))"
        if getattr(e, "builtin", None) == "mysql_query_json_pool":
            return f"well_mysql_pool_query_json_global(well_string_cstr({self.gen_str_arg(e.args[0])}))"
        if getattr(e, "builtin", None) == "mysql_close_pool":
            return "well_mysql_pool_close_global()"
        if getattr(e, "builtin", None) == "ws_route":
            n = e.route_index
            return ("well_ws_route(well_string_cstr(" + self.gen_str_arg(e.args[0]) + "), "
                    f"ws_open_{n}, ws_msg_{n}, ws_close_{n})")
        if getattr(e, "builtin", None) == "ws_listen":
            return f"well_ws_listen((int)({self.gen_expr(e.args[0])}))"
        if getattr(e, "builtin", None) == "ws_stop":
            return "well_ws_stop()"
        if getattr(e, "builtin", None) == "ws_send":
            return (f"well_ws_send((long long)({self.gen_expr(e.args[0])}), "
                    f"well_string_cstr({self.gen_str_arg(e.args[1])}))")
        if getattr(e, "builtin", None) == "ws_close":
            return (f"well_ws_close((long long)({self.gen_expr(e.args[0])}), "
                    f"(int)({self.gen_expr(e.args[1])}), "
                    f"well_string_cstr({self.gen_str_arg(e.args[2])}))")
        if getattr(e, "builtin", None) == "http_route":
            return (f"well_http_route(well_string_cstr({self.gen_str_arg(e.args[0])}), "
                    f"well_string_cstr({self.gen_str_arg(e.args[1])}), http_route_{e.route_index})")
        if getattr(e, "builtin", None) == "http_listen":
            return f"well_http_listen((int)({self.gen_expr(e.args[0])}))"
        if getattr(e, "builtin", None) == "http_stop":
            return "well_http_stop()"
        if getattr(e, "builtin", None) == "http_respond":
            return (f"well_http_respond((long long)({self.gen_expr(e.args[0])}), "
                    f"(int)({self.gen_expr(e.args[1])}), well_string_cstr({self.gen_arg_temp(e.args[2])}))")
        if getattr(e, "builtin", None) == "file_read":
            return f"well_file_read(well_string_cstr({self.gen_str_arg(e.args[0])}))"
        if getattr(e, "builtin", None) == "file_write":
            return (f"well_file_write(well_string_cstr({self.gen_arg_temp(e.args[0])}), "
                    f"well_string_cstr({self.gen_arg_temp(e.args[1])}))")
        if getattr(e, "builtin", None) == "file_exists":
            return f"well_file_exists(well_string_cstr({self.gen_str_arg(e.args[0])}))"
        if getattr(e, "builtin", None) == "file_remove":
            return f"well_file_remove(well_string_cstr({self.gen_str_arg(e.args[0])}))"
        if getattr(e, "builtin", None) == "host_now":
            return "well_host_now_ms()"
        if getattr(e, "builtin", None) == "host_mono_us":
            return "well_host_mono_us()"
        if getattr(e, "builtin", None) == "host_env":
            return f"well_host_env(well_string_cstr({self.gen_str_arg(e.args[0])}))"
        if getattr(e, "enum_op", None) == "from_int":
            return f"{c_enum(e.enum_name)}__fromInt((long long)({self.gen_expr(e.args[0])}))"
        if getattr(e, "enum_op", None) == "to_string":
            return f"{c_enum(e.enum_name)}__toString({self.gen_expr(e.args[0])})"
        if getattr(e, "builtin", None) == "json_parse":
            ty = e.json_type
            arg = self.gen_str_arg(e.args[1])
            return f"(({c_type(ty)}*)well_json_parse(T_{c_type(ty)}, well_string_cstr({arg}), ctx->oid))"
        if getattr(e, "builtin", None) == "json_stringify":
            ind = (f"(int)({self.gen_expr(e.json_indent)})"
                   if getattr(e, "json_indent", None) is not None else "0")
            arg = self.gen_expr(e.args[0])
            kind = getattr(e, "json_str_kind", "struct")
            if kind == "struct":
                return f"well_json_stringify((void*)({arg}), {ind})"
            if kind in ("int", "long"):
                return f"well_json_stringify_long((long long)({arg}), {ind})"
            if kind == "double":
                return f"well_json_stringify_double((double)({arg}), {ind})"
            if kind == "bool":
                return f"well_json_stringify_bool((int)({arg}), {ind})"
            if kind == "String":
                fn = "well_json_stringify_str_take" if self.is_owned(e.args[0]) else "well_json_stringify_str"
                return f"{fn}({arg}, {ind})"
            et = getattr(e, "json_elem_ty", "String")
            ek = self.json_elem_kind(et)
            tid = self.json_elem_type_id(et)
            if kind == "array":
                return f"well_json_stringify_array((WellArray*)({arg}), {ek}, {tid}, {ind})"
            if kind == "map":
                return f"well_json_stringify_map((WellMap*)({arg}), {ek}, {tid}, {ind})"
            if kind == "linked":
                return f"well_json_stringify_linked((WellLinked*)({arg}), {ek}, {tid}, {ind})"
            return f"well_json_stringify((void*)({arg}), {ind})"
        if getattr(e, "builtin", None) == "json_try_parse":
            ty = e.json_type
            arg = self.gen_arg_temp(e.args[1])
            return f"(({c_type(ty)}*)well_json_try_parse(T_{c_type(ty)}, well_string_cstr({arg}), ctx->oid))"
        if getattr(e, "builtin", None) == "idutil_get":
            return "well_id_util_get_id()"
        if getattr(e, "builtin", None) in ("timer_once", "timer_loop"):
            callee = "well_timer_once" if e.builtin == "timer_once" else "well_timer_loop"
            return f"{callee}((long long)({self.gen_expr(e.args[0])}), timer_fire_{e.timer_index})"
        if getattr(e, "builtin", None) == "timer_cancel":
            return f"well_timer_cancel((long long)({self.gen_expr(e.args[0])}))"
        if getattr(e, "builtin", None) == "str_from_byte":
            return f"well_str_from_byte((int)({self.gen_expr(e.args[0])}))"
        if getattr(e, "builtin", None) == "str_from_int":
            return f"well_str_from_int((long long)({self.gen_expr(e.args[0])}))"
        if getattr(e, "builtin", None) == "str_from_codepoint":
            return f"well_string_from_code_point((long long)({self.gen_expr(e.args[0])}))"
        if getattr(e, "method_op", None):
            op = e.method_op
            obj = self.gen_expr(e.method_obj)
            take = "_take" if self.is_owned(e.method_obj) else ""
            if op == "array_push":
                if (isinstance(e.method_obj, Ident)
                        and e.method_obj.name in self.stack_array_storage):
                    st = self.stack_array_storage[e.method_obj.name]
                    return (f"(({st}.items[{st}.len++] = "
                            f"{self.box_value(self.gen_expr(e.args[0]), e.elem_ty)}), (void)0)")
                if (isinstance(e.method_obj, Ident)
                        and e.method_obj.name in self.direct_push_names):
                    return (f"(({obj}->items[{obj}->len++] = "
                            f"{self.box_value(self.gen_expr(e.args[0]), e.elem_ty)}), (void)0)")
                if (isinstance(e.method_obj, Ident)
                        and e.method_obj.name in self.direct_str_push_names):
                    return (f"(({obj}->items[{obj}->len++].p = "
                            f"(void*){self.gen_expr(e.args[0])}), (void)0)")
                if (isinstance(e.method_obj, Ident)
                        and e.method_obj.name in self.direct_fresh_push_names):
                    return (f"(({obj}->items[{obj}->len++].p = "
                            f"(void*){self.gen_expr(e.args[0])}), (void)0)")
                return f"well_array_push({obj}, {self.box_value(self.gen_expr(e.args[0]), e.elem_ty)}, ctx)"
            if op == "array_pop":
                return self.unbox_value(f"well_array_pop({obj}, ctx)", e.elem_ty)
            if op == "array_get":
                return self.unbox_value(f"well_array_get({obj}, (long long)({self.gen_expr(e.args[0])}))", e.elem_ty)
            if op == "array_set":
                return f"well_array_set({obj}, (long long)({self.gen_expr(e.args[0])}), {self.box_value(self.gen_expr(e.args[1]), e.elem_ty)}, ctx)"
            if op == "array_slice":
                return (f"well_array_slice{take}({obj}, (long long)({self.gen_expr(e.args[0])}), "
                        f"(long long)({self.gen_expr(e.args[1])}), ctx)")
            if op == "array_concat":
                return f"well_array_concat{take}({obj}, {self.gen_expr(e.args[0])}, ctx)"
            if op == "array_join":
                return f"well_array_join{take}({obj}, {self.gen_str_arg(e.args[0])})"
            if op == "array_index_of":
                return (f"well_array_index_of{take}({obj}, "
                        f"{self.box_value(self.gen_str_arg(e.args[0]), e.elem_ty)}, "
                        f"{self.elem_kind_code(e.elem_ty)})")
            if op == "array_includes":
                return (f"well_array_includes{take}({obj}, "
                        f"{self.box_value(self.gen_str_arg(e.args[0]), e.elem_ty)}, "
                        f"{self.elem_kind_code(e.elem_ty)})")
            if op == "array_sort":
                return f"well_array_sort({obj}, {self.sort_kind_code(e.elem_ty)}, ctx)"
            if op == "array_reverse":
                return f"well_array_reverse({obj}, ctx)"
            if op == "array_clear":
                return f"well_array_clear({obj}, ctx)"
            if op == "array_shift":
                return self.unbox_value(f"well_array_shift({obj}, ctx)", e.elem_ty)
            if op == "array_unshift":
                return (f"well_array_unshift({obj}, "
                        f"{self.box_value(self.gen_expr(e.args[0]), e.elem_ty)}, ctx)")
            if op == "map_keys":
                return f"well_map_keys({obj}, ctx)"
            if op == "map_values":
                return f"well_map_values({obj}, ctx)"
            if op == "map_clear":
                return f"well_map_clear({obj}, ctx)"
            if op == "map_has":
                return f"well_map_has({obj}, {self.gen_str_arg(e.args[0])})"
            if op == "map_get":
                return self.unbox_value(f"well_map_get({obj}, {self.gen_str_arg(e.args[0])})", e.elem_ty)
            if op == "map_set":
                return f"well_map_set({obj}, {self.gen_str_arg(e.args[0])}, {self.box_value(self.gen_expr(e.args[1]), e.elem_ty)}, ctx)"
            if op == "map_delete":
                return f"well_map_delete({obj}, {self.gen_str_arg(e.args[0])}, ctx)"
            if op == "str_code_point_at":
                if self.is_owned(e.method_obj):
                    return (f"well_str_code_point_at_take({obj}, "
                            f"(long long)({self.gen_expr(e.args[0])}))")
                return f"well_str_code_point_at({obj}, (long long)({self.gen_expr(e.args[0])}))"
            if op == "str_byte_at":
                return f"well_str_byte_at{take}({obj}, (long long)({self.gen_expr(e.args[0])}))"
            if op == "str_substring":
                return (f"well_str_substring{take}({obj}, (long long)({self.gen_expr(e.args[0])}), "
                        f"(long long)({self.gen_expr(e.args[1])}))")
            if op == "str_index_of":
                return f"well_str_index_of{take}({obj}, {self.gen_str_arg(e.args[0])})"
            if op == "str_index_of_from":
                return (f"well_str_index_of_from{take}({obj}, {self.gen_str_arg(e.args[0])}, "
                        f"(long long)({self.gen_expr(e.args[1])}))")
            if op == "str_contains":
                return f"well_str_contains{take}({obj}, {self.gen_str_arg(e.args[0])})"
            if op == "str_starts_with":
                return f"well_str_starts_with{take}({obj}, {self.gen_str_arg(e.args[0])})"
            if op == "str_ends_with":
                return f"well_str_ends_with{take}({obj}, {self.gen_str_arg(e.args[0])})"
            if op == "str_replace":
                return (f"well_str_replace{take}({obj}, {self.gen_arg_temp(e.args[0])}, "
                        f"{self.gen_arg_temp(e.args[1])})")
            if op == "str_trim":
                return f"well_str_trim{take}({obj})"
            if op == "str_to_lower":
                return f"well_str_to_lower{take}({obj})"
            if op == "str_to_upper":
                return f"well_str_to_upper{take}({obj})"
            if op == "str_split":
                return f"well_str_split{take}({obj}, {self.gen_str_arg(e.args[0])}, ctx)"
            if op == "bytes_push":
                return f"well_bytes_push({obj}, (int)({self.gen_expr(e.args[0])}), ctx)"
            if op == "bytes_get":
                return f"well_bytes_get({obj}, (long long)({self.gen_expr(e.args[0])}))"
            if op == "bytes_set":
                return (f"well_bytes_set({obj}, (long long)({self.gen_expr(e.args[0])}), "
                        f"(int)({self.gen_expr(e.args[1])}), ctx)")
            if op == "bytes_append":
                return f"well_bytes_append({obj}, {self.gen_expr(e.args[0])}, ctx)"
            if op == "bytes_append_str":
                return f"well_bytes_append_str({obj}, {self.gen_arg_temp(e.args[0])}, ctx)"
            if op == "bytes_clear":
                return f"well_bytes_clear({obj}, ctx)"
            if op == "bytes_to_string":
                return f"well_bytes_to_string({obj})"
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
        if getattr(e, "cache_op", None) == "get":
            return f"(({self.ctype(e.cache_type)})well_cache_get_ctx(T_{c_type(e.cache_type)}, (long long)({self.gen_expr(e.args[1])}), ctx))"
        if getattr(e, "cache_op", None) == "set":
            return f"well_cache_set_ctx((void*)({self.gen_arg_temp(e.args[0])}), ctx)"
        if getattr(e, "cache_op", None) == "remove_all":
            return "well_cache_remove_ctx(ctx)"
        if getattr(e, "model_op", None) == "get":
            return f"(({self.ctype(e.model_type)})well_model_get_ctx(T_{c_type(e.model_type)}, (long long)({self.gen_expr(e.args[1])}), ctx))"
        if getattr(e, "model_op", None) == "peek":
            return f"(({self.ctype(e.model_type)})well_model_peek_ctx(T_{c_type(e.model_type)}, (long long)({self.gen_expr(e.args[1])}), ctx))"
        if getattr(e, "model_op", None) == "findByOid":
            return f"(({self.ctype(e.model_type)})well_model_get_ctx(T_{c_type(e.model_type)}, (long long)({self.gen_expr(e.args[1])}), ctx))"
        if getattr(e, "model_op", None) == "remove":
            return f"well_model_remove(T_{c_type(e.model_type)}, (long long)({self.gen_expr(e.args[1])}), ctx)"
        if getattr(e, "model_op", None) == "set":
            return f"well_model_set((void*)({self.gen_arg_temp(e.args[0])}), ctx)"
        if getattr(e, "builtin", None) == "emit":
            channel = self.gen_channel(getattr(e, "channel_arg", None))
            return f"well_emit_event((void*)({self.gen_arg_temp(e.event_arg)}), (long long)({channel}))"
        if getattr(e, "builtin", None) in ("on", "once", "off"):
            h = e.handler_name
            fn = f"{c_handler(h)}_onEvent"
            channel = self.gen_channel(getattr(e, "channel_arg", None))
            if e.builtin == "off":
                return f"well_off_type(T_{c_type(e.event_type)}, {fn}, (long long)({channel}))"
            once = "1" if e.builtin == "once" else "0"
            return f"well_on_type(T_{c_type(e.event_type)}, {fn}, {once}, (long long)({channel}))"
        if getattr(e, "builtin", None) == "start":
            return f"{c_ctl(e.controller_name)}_ensure(ctx)"
        if getattr(e, "builtin", None) == "stop":
            return f"{c_ctl(e.controller_name)}_stop(ctx)"
        if getattr(e, "builtin", None) in ("print", "host_log"):
            raise Exception("print/Host.log is only supported as a statement")
        if getattr(e, "call_ns", None):
            ns, fname = e.call_ns
            params = None
            if ns in self.p.namespaces and fname in self.p.namespaces[ns]:
                params = self.p.namespaces[ns][fname].params
            elif ns in getattr(self.p, "statics", {}) and fname in self.p.statics[ns]["funcs"]:
                params = self.p.statics[ns]["funcs"][fname].params
            return self.gen_call_ordered(e, c_func(f"{ns}__{fname}"), params)
        if getattr(e, "call_user", None):
            f = self.p.funcs.get(e.call_user)
            return self.gen_call_ordered(e, c_func(e.call_user), f.params if f else None)
        raise Exception("unsupported call")

    def assert_codegen_type(self, target: str, e, where: str):
        """Defense in depth: refuse to emit C for a checker-level type mismatch.

        The checker is supposed to reject these; if a future checker regression
        lets one through, this turns silent C type confusion (pointer-as-int,
        int-as-pointer) into a clear internal error instead of UB.
        """
        vt = getattr(e, "ty", None)
        if not vt or vt == "null" or target == vt:
            return
        scalar = ("int", "long", "double", "float", "bool")
        if target in scalar and vt in scalar:
            return
        if self.is_ref(target) and self.is_ref(vt):
            return
        raise Exception(f"internal compiler type mismatch in {where}: {vt} -> {target}")

    def gen_call_ordered(self, e, fn_name: str, params=None) -> str:
        """Call ``fn_name`` with arguments evaluated left-to-right.

        C does not specify argument evaluation order (TCC evaluates left-to-right,
        GCC right-to-left), so the same Well program could observe different side
        effects depending on WELL_CC. Lifting each argument into a temporary
        inside a statement expression makes the order explicit and portable.
        """
        if not e.args:
            return f"{fn_name}(ctx)"
        decls, parts, owned = [], [], []
        for i, a in enumerate(e.args):
            if params is not None and i < len(params):
                self.assert_codegen_type(params[i][1].name, a,
                                        f"argument {i + 1} of {fn_name}")
            ty = getattr(a, "ty", None) or "int"
            if ty == "void":
                ty = "int"
            cty = self.ctype(ty)
            tmp = f"_ag{self.next_id()}"
            decls.append(f"{cty} {tmp} = ({cty})({self.gen_expr(a)});")
            parts.append(tmp)
            if self.is_owned(a) and self.is_ref(getattr(a, "ty", "")):
                owned.append(tmp)
            elif self.is_ref(getattr(a, "ty", "")) and self._unstable_ref(a):
                # A borrowed value reached through a field/index/static can be
                # released by the evaluation of a *later* argument (e.g.
                # f(State.item, resetSource())).  Take an edge for the call and
                # park it on the temp journal (which a panic unwind resets).
                decls.append(f"well_retain((void*){tmp});")
                self.temp_used += 1
                decls.append(f"well_temp_hold((void*){tmp});")
        call = f"{fn_name}({', '.join(['ctx'] + parts)})"
        rt = getattr(e, "ty", None) or "void"
        rel = " ".join(f"well_release((void*){t});" for t in owned)
        if rt == "void":
            return f"({{ {' '.join(decls)} {call}; {rel} }})"
        ct = self.ctype(rt)
        tmp = f"_rs{self.next_id()}"
        return f"({{ {' '.join(decls)} {ct} {tmp} = ({ct})({call}); {rel} {tmp}; }})"

    # ---------- statements ----------
    def gen_scoped(self, fn):
        """Run ``fn`` with a block-scoped copy of ``env``.

        Block/loop/if bodies may declare variables that shadow outer ones;
        the C scopes already handle the storage, but the code generator's
        type environment must be restored as well or a later assignment to
        the outer variable is generated with the inner shadow's type.
        """
        saved = dict(self.env)
        saved_names = dict(self.c_names)
        saved_slots = dict(self.journaled)
        try:
            fn()
        finally:
            self.env = saved
            self.c_names = saved_names
            self.journaled = saved_slots

    def gen_block(self, b: Block):
        for s in b.stmts:
            self.gen_stmt(s)

    def push_scope(self, mark: str, is_loop: bool = False, kind: str = None):
        # Every language scope also gets its own temp-journal mark so temporaries
        # created inside a loop/block are released when that scope exits instead
        # of piling up until the end of the function.  The mark defaults to 0 and
        # is upgraded to well_temp_mark() only if the body actually holds temps.
        tmark = f"_tm{self.next_id()}"
        self.w(f"int {tmark} = 0;")
        idx = len(self.lines) - 1
        self.scope_stack.append((mark, kind or ("loop" if is_loop else "block"),
                                 tmark, idx, self.temp_used, self.tracked))

    def pop_scope(self, mark: str):
        if self.scope_stack and self.scope_stack[-1][0] == mark:
            _mark, _kind, tmark, idx, used_before, _tracked = self.scope_stack.pop()
            if self.temp_used > used_before:
                self.lines[idx] = self.lines[idx].replace(
                    f"int {tmark} = 0;", f"int {tmark} = well_temp_mark();")
                self.w(f"well_temp_release_to({tmark});")

    def emit_scope_release(self, mark: str, final: bool = False):
        # Frame-allocated structs with reference fields release them here; this
        # runs on the normal path and on every early return/break/continue path
        # that leaves the scope.
        for ty, st in reversed(self.stack_cleanup.get(mark, [])):
            self.w(f"destroy_{c_type(ty)}((void*)&{st});")
        before = None
        for m, _kind, _tmark, _idx, _used, tracked_before in reversed(self.scope_stack):
            if m == mark:
                before = tracked_before
                break
        if before is None:
            self.w(f"well_ctx_release_to(ctx, {mark});")
            return
        if not final:
            # An early return/break/continue path uses this mark.  Never turn
            # it into a plain 0 later, or that path would release from 0.
            self.early_marks.add(mark)
        has_tracks = self.tracked > before
        if not has_tracks:
            if final and mark not in self.early_marks:
                for i in range(len(self.lines) - 1, -1, -1):
                    if f"int {mark} = well_ctx_mark(ctx);" in self.lines[i]:
                        self.lines[i] = self.lines[i].replace(
                            f"int {mark} = well_ctx_mark(ctx);",
                            f"int {mark} = 0;")
                        break
            return
        self.w(f"well_ctx_release_to(ctx, {mark});")

    def emit_return_cleanup(self):
        if self.temp_mark:
            self.w(f"well_temp_release_to({self.temp_mark});")
        for mark, _kind, _tmark, _idx, _used, _tracked in reversed(self.scope_stack):
            self.emit_scope_release(mark, final=False)

    def emit_break_cleanup(self, is_continue: bool = False):
        """Release scopes on the way out of break/continue.

        `break` leaves the nearest breakable scope (loop *or* switch); `continue`
        additionally leaves a switch and unwinds to the nearest loop.
        """
        for mark, kind, tmark, _idx, _used, _tracked in reversed(self.scope_stack):
            self.emit_scope_release(mark, final=False)
            self.w(f"well_temp_release_to({tmark});")
            if is_continue and kind == "loop":
                break
            if not is_continue and kind in ("loop", "switch"):
                break

    def gen_case_body(self, body: Block):
        """Generate one switch-case body inside its own release scope.

        Without a per-case mark a reference declared in one case lived until the
        whole switch/function ended; the C braces already isolate storage, so the
        journal mark is the missing half.
        """
        cm = f"_m{self.next_id()}"
        self.w(f"int {cm} = well_ctx_mark(ctx);")
        self.push_scope(cm, kind="block")
        self.gen_scoped(lambda: self.gen_block(body))
        self.emit_scope_release(cm, final=True)
        self.pop_scope(cm)

    def _stack_safe_stmt(self, s) -> bool:
        if isinstance(s, VarDecl):
            return id(s) in self.stack_decls or id(s) in self.stack_arrays or id(s) in self.stack_maps
        if isinstance(s, ExprStmt) and isinstance(s.expr, Call):
            c = s.expr
            return (getattr(c, "method_op", None) == "array_push" and
                    isinstance(c.method_obj, Ident) and
                    (c.method_obj.name in self.stack_array_storage or
                     c.method_obj.name in self.direct_push_names or
                     c.method_obj.name in self.direct_str_push_names or
                     c.method_obj.name in self.direct_fresh_push_names))
        if isinstance(s, Assign) and isinstance(s.target, Index):
            obj = s.target.obj
            return (isinstance(obj, Ident) and
                    (obj.name in self.stack_map_storage or
                     obj.name in self.direct_map_names or
                     obj.name in self.direct_str_map_names or
                     obj.name in self.direct_fresh_map_names))
        if isinstance(s, Assign) and isinstance(s.target, Member):
            obj = s.target.obj
            return isinstance(obj, Ident) and obj.name in self.stack_vars
        return False

    def _self_sourced_panic(self, s) -> bool:
        """True when every panic this statement can raise already carries its
        own source position (well_check_oid_at), so the pre-statement
        well_source_set() can be skipped."""
        if isinstance(s, Assign) and isinstance(s.target, Member) \
                and isinstance(s.target.obj, Ident):
            return not self.expr_may_panic(s.value)
        return False

    def gen_stmt(self, s: Stmt):
        line = getattr(s, "line", None)
        src = getattr(s, "file", None)
        if line and src and self.stmt_may_panic(s) and not self._stack_safe_stmt(s) \
                and not self._self_sourced_panic(s):
            self.w(f'well_source_set(\"{c_escape(str(src))}\", {line});')
        self._gen_stmt(s)

    def _gen_stmt(self, s: Stmt):
        if isinstance(s, VarDecl):
            self.gen_var_decl(s)
        elif isinstance(s, Assign):
            self.gen_assign(s)
        elif isinstance(s, ExprStmt):
            self.gen_expr_stmt(s)
        elif isinstance(s, If):
            self.w(f"if ({self.gen_expr(s.cond)}) {{")
            self.ind += 1
            im = f"_m{self.next_id()}"
            self.w(f"int {im} = well_ctx_mark(ctx);")
            self.push_scope(im, kind="block")
            self.gen_scoped(lambda: self.gen_stmt(s.then))
            self.emit_scope_release(im, final=True)
            self.pop_scope(im)
            self.ind -= 1
            if s.els:
                self.w("} else {")
                self.ind += 1
                em = f"_m{self.next_id()}"
                self.w(f"int {em} = well_ctx_mark(ctx);")
                self.push_scope(em, kind="block")
                self.gen_scoped(lambda: self.gen_stmt(s.els))
                self.emit_scope_release(em, final=True)
                self.pop_scope(em)
                self.ind -= 1
            self.w("}")
        elif isinstance(s, Switch):
            ct = getattr(s.expr, "ty", None)
            expr = self.gen_expr(s.expr)
            if ct == "String":
                owned = self.is_owned(s.expr)
                # `do { ... } while (0)` makes `break` inside a case leave the
                # switch (not the enclosing loop); mirrors the int-switch codegen.
                self.w("do {")
                self.ind += 1
                swm = f"_m{self.next_id()}"
                self.w(f"int {swm} = well_ctx_mark(ctx);")
                self.push_scope(swm, kind="switch")
                if owned:
                    self.w(f"WellString *_sw = {expr};")
                    self.w("well_track_owned(ctx, _sw);")
                    expr = "_sw"
                first = True
                for val, body in s.cases:
                    cond = f'well_string_eq({expr}, {self.gen_expr(val)})'
                    if first:
                        self.w(f"if ({cond}) {{")
                        first = False
                    else:
                        self.w(f"}} else if ({cond}) {{")
                    self.ind += 1
                    self.gen_case_body(body)
                    self.ind -= 1
                if s.default:
                    if first:
                        self.w("{")
                        first = False
                    else:
                        self.w("} else {")
                    self.ind += 1
                    self.gen_case_body(s.default)
                    self.ind -= 1
                if not first:
                    self.w("}")
                else:
                    self.w(f"(void)({expr});")
                self.emit_scope_release(swm, final=True)
                self.pop_scope(swm)
                self.ind -= 1
                self.w("} while (0);")
            else:
                swm = f"_m{self.next_id()}"
                self.w(f"int {swm} = well_ctx_mark(ctx);")
                self.push_scope(swm, kind="switch")
                self.w(f"switch ((long long)({expr})) {{")
                self.ind += 1
                for val, body in s.cases:
                    self.w(f"case (long long)({self.gen_expr(val)}): {{")
                    self.ind += 1
                    self.gen_case_body(body)
                    self.ind -= 1
                    self.w("} break;")
                if s.default:
                    self.w("default: {")
                    self.ind += 1
                    self.gen_case_body(s.default)
                    self.ind -= 1
                    self.w("} break;")
                self.ind -= 1
                self.w("}")
                self.emit_scope_release(swm, final=True)
                self.pop_scope(swm)
        elif isinstance(s, While):
            cond = self.gen_expr(s.cond)
            sc = self.source_stmt(s) if self.expr_may_panic(s.cond) else None
            if sc:
                # Re-set the source location before *every* condition evaluation:
                # otherwise a panic in the condition of iteration 2+ reports the
                # line of the last executed body statement instead of the loop.
                self.w(f"while (({sc}, ({cond}))) {{")
            else:
                self.w(f"while ({cond}) {{")
            self.ind += 1
            m = self.next_id()
            mk = f"_m{m}"
            self.w(f"int {mk} = well_ctx_mark(ctx);")
            self.push_scope(mk, True)
            self.continue_stack.append(None)
            self.gen_scoped(lambda: self.gen_stmt(s.body))
            self.continue_stack.pop()
            self.emit_scope_release(mk, final=True)
            self.pop_scope(mk)
            self.ind -= 1
            self.w("}")
        elif isinstance(s, ForEach):
            it = self.gen_expr(s.iter)
            kind, et = self.container_info(getattr(s.iter, "ty", None) or "")
            n = self.next_id()
            saved = dict(self.env)
            saved_slots = dict(self.journaled)
            if not isinstance(s.iter, (Ident, Member)):
                # Evaluate the iterated expression once, before the loop: it used
                # to be re-evaluated for every length check and element access
                # (repeated side effects and one leaked container per call).
                cty = {"array": "WellArray*", "linked": "WellLinked*"}.get(kind, "WellMap*")
                iname = f"_it{n}"
                self.w(f"{cty} {iname} = {it};")
                if self.is_owned(s.iter):
                    self.temp_used += 1
                    self.w(f"well_temp_hold((void*){iname});")
                it = iname
            if kind == "array":
                self.w(f"for (long long _i{n} = 0; _i{n} < well_array_len({it}); _i{n}++) {{")
            elif kind == "linked":
                self.w(f"for (WellLinkedNode* _n{n} = well_linked_first({it}); _n{n}; _n{n} = well_linked_next(_n{n})) {{")
            else:
                self.w(f"for (long long _i{n} = 0; _i{n} < well_map_len({it}); _i{n}++) {{")
            self.ind += 1
            # The per-iteration journal mark is taken *before* the loop binding
            # so the binding's edge is released at the end of each iteration.
            m = self.next_id()
            mk = f"_m{m}"
            self.w(f"int {mk} = well_ctx_mark(ctx);")
            self.push_scope(mk, True)
            if kind == "array":
                self.w(f"{self.ctype(et)} {c_var(s.var)} = {self.unbox_value(f'well_array_get_fast({it}, _i{n})', et)};")
                self.var_decl(s.var, et)
                if self.is_ref(et):
                    self.bind_ref(s.var, c_var(s.var), 0)
            elif kind == "linked":
                self.w(f"{self.ctype(et)} {c_var(s.var)} = {self.unbox_value(f'well_linked_value(_n{n})', et)};")
                self.var_decl(s.var, et)
                if self.is_ref(et):
                    self.bind_ref(s.var, c_var(s.var), 0)
            else:
                self.w(f"WellString* {c_var(s.var)} = well_map_key_at({it}, _i{n});")
                self.var_decl(s.var, "String")
                self.bind_ref(s.var, c_var(s.var), 0)
            self.continue_stack.append(None)
            self.gen_scoped(lambda: self.gen_stmt(s.body))
            self.continue_stack.pop()
            self.emit_scope_release(mk, final=True)
            self.pop_scope(mk)
            self.ind -= 1
            self.w("}")
            self.env = saved
            self.journaled = saved_slots
        elif isinstance(s, For):
            saved = dict(self.env)
            saved_slots = dict(self.journaled)
            self.w("{")
            self.ind += 1
            if s.init:
                self.gen_stmt(s.init)
            raw_cond = self.gen_expr(s.cond) if s.cond else "1"
            sc = self.source_stmt(s) if self.expr_may_panic(s.cond) else None
            cond = f"({sc}, ({raw_cond}))" if sc else raw_cond
            self.w(f"while ({cond}) {{")
            self.ind += 1
            m = self.next_id()
            mk = f"_m{m}"
            self.w(f"int {mk} = well_ctx_mark(ctx);")
            self.push_scope(mk, True)
            cont = f"_cont{m}"
            self.continue_stack.append(cont)
            self.gen_scoped(lambda: self.gen_stmt(s.body))
            self.continue_stack.pop()
            self.emit_scope_release(mk, final=True)
            self.w(f"{cont}: ;")
            self.pop_scope(mk)
            if s.step:
                self.gen_stmt(s.step)
            self.ind -= 1
            self.w("}")
            self.ind -= 1
            self.w("}")
            self.env = saved
            self.journaled = saved_slots
        elif isinstance(s, Return):
            self.gen_return(s)
        elif isinstance(s, Break):
            self.emit_break_cleanup()
            self.w("break;")
        elif isinstance(s, Continue):
            self.emit_break_cleanup(is_continue=True)
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
            self.gen_scoped(lambda: self.gen_block(s))
            self.emit_scope_release(mk, final=True)
            self.pop_scope(mk)
            self.ind -= 1
            self.w("}")
        else:
            raise Exception(f"unsupported stmt {type(s).__name__}")

    def gen_var_decl(self, s: VarDecl):
        ty = getattr(s, "ty", None) or (s.type.name if s.type else None)
        if not ty:
            raise Exception(f"cannot determine type for {s.name}")
        cname = c_var(s.name)
        self.assert_codegen_type(ty, s.init, f"var {s.name}")
        if id(s) in self.stack_maps:
            # Non-escaping scalar map literal: header + 4 inline entries live in
            # the C frame.  Keys are immortal string literals, values are scalar.
            kind, et = self.container_info(ty)
            st = f"_st{self.next_id()}"
            self.w(f"WellMap {st};")
            self.w(f"{st}.keys = {st}.inline_keys;")
            self.w(f"{st}.vals = {st}.inline_vals;")
            self.w(f"{st}.slots = {st}.inline_slots;")
            self.w(f"{st}.len = 0;")
            self.w(f"{st}.cap = 4;")
            self.w(f"{st}.slot_cap = 0;")
            self.w(f"{st}.is_ref = 0;")
            for k, v in s.init.pairs:
                slot = f"_ws_lit{self.next_id()}"
                self.w(f'{st}.keys[{st}.len] = ({{ static WellString *{slot}; '
                       f'well_string_lit_slot(&{slot}, "{c_escape(k)}"); }});')
                self.w(f"{st}.vals[{st}.len] = {self.box_value(self.gen_expr(v), et)};")
                self.w(f"{st}.len++;")
            self.w(f"WellMap* {cname} = &{st};")
            self.stack_map_keys[s.name] = {k: i for i, (k, _v) in enumerate(s.init.pairs)}
            self.stack_map_storage[s.name] = st
            if (s.name in self.direct_map_names or
                    s.name in self.direct_str_map_names or
                    s.name in self.direct_fresh_map_names):
                self.direct_map_keys[s.name] = {k: i for i, (k, _v) in enumerate(s.init.pairs)}
            self.var_decl(s.name, ty)
            return
        if id(s) in self.stack_arrays:
            # Non-escaping scalar array literal: keep header + 4 slots in the
            # C frame.  No heap, no RC, no registry.
            kind, et = self.container_info(ty)
            st = f"_st{self.next_id()}"
            self.w(f"WellArray {st};")
            self.w(f"{st}.items = {st}.inline_items;")
            self.w(f"{st}.len = 0;")
            self.w(f"{st}.cap = 4;")
            self.w(f"{st}.is_ref = 0;")
            for it in s.init.items:
                self.w(f"{st}.items[{st}.len++] = {self.box_value(self.gen_expr(it), et)};")
            self.w(f"WellArray* {cname} = &{st};")
            self.stack_array_names[s.name] = len(s.init.items)
            self.stack_array_storage[s.name] = st
            self.var_decl(s.name, ty)
            return
        if id(s) in self.stack_decls:
            # Non-escaping struct: allocate in the C frame.  No allocation, no
            # registry; field accesses still work through the pointer and
            # well_check_oid sees the right oid/type.  Reference fields are
            # released by the generated destroy at scope exit.
            ct = c_type(ty)
            st = f"_st{self.next_id()}"
            self.w(f"{ct} {st};")
            self.w(f"memset(&{st}, 0, sizeof({st}));")
            self.w(f"{st}.hdr.oid = ctx ? ctx->oid : 0;")
            self.w(f"{st}.hdr.type_id = T_{ct};")
            self.w(f"{st}.hdr.rc = 1;")
            self.w(f"{st}.hdr.flags |= WELL_FLAG_STACK;")
            self.w(f"{self.ctype(ty)} {cname} = &{st};")
            decl = self.p.structs.get(ty) or {"fields": []}
            has_ref = any(self.is_ref(f.type.name) for f in decl["fields"])
            if has_ref and self.scope_stack:
                self.stack_cleanup.setdefault(self.scope_stack[-1][0], []).append((ty, st))
            self.stack_vars.add(s.name)
            self.var_decl(s.name, ty)
            return
        if isinstance(s.init, (ArrayLit, ObjectLit)):
            kind, et = self.container_info(ty)
            if not kind:
                raise Exception(f"container literal with non-container type {ty}")
            if kind == "array":
                self.w(f"WellArray* {cname} = {self.container_new_call('array', et)};")
                immut = (et == "String" and
                         all(isinstance(it, StrLit) for it in s.init.items))
                fresh_refs = (self.is_ref(et) and s.init.items and
                              all(isinstance(it, New) for it in s.init.items))
                if (not self.is_ref(et) or immut) and len(s.init.items) <= 4:
                    for idx, it in enumerate(s.init.items):
                        self.w(f"{cname}->items[{idx}] = "
                               f"{self.box_value(self.gen_expr(it), et)};")
                    self.w(f"{cname}->len = {len(s.init.items)};")
                elif fresh_refs and len(s.init.items) <= 4:
                    # Fresh objects transfer ownership into the array; no retain.
                    for idx, it in enumerate(s.init.items):
                        self.w(f"{cname}->items[{idx}].p = (void*){self.gen_expr(it)};")
                    self.w(f"{cname}->len = {len(s.init.items)};")
                else:
                    for it in s.init.items:
                        self.emit_container_push(cname, it, et)
            else:
                self.w(f"WellMap* {cname} = {self.container_new_call('map', et)};")
                if (s.name in self.direct_map_names or
                        s.name in self.direct_str_map_names or
                        s.name in self.direct_fresh_map_names):
                    self.direct_map_keys[s.name] = {
                        k: i for i, (k, _v) in enumerate(s.init.pairs)}
                immut = (et == "String" and
                         all(isinstance(v, StrLit) for _k, v in s.init.pairs))
                fresh_refs = (self.is_ref(et) and s.init.pairs and
                              all(isinstance(v, New) for _k, v in s.init.pairs))
                if (not self.is_ref(et) or immut) and len(s.init.pairs) <= 4:
                    for idx, (k, v) in enumerate(s.init.pairs):
                        slot = f"_ws_lit{self.next_id()}"
                        self.w(f'{cname}->keys[{idx}] = ({{ static WellString *{slot}; '
                               f'well_string_lit_slot(&{slot}, "{c_escape(k)}"); }});')
                        self.w(f"{cname}->vals[{idx}] = "
                               f"{self.box_value(self.gen_expr(v), et)};")
                    self.w(f"{cname}->len = {len(s.init.pairs)};")
                elif fresh_refs and len(s.init.pairs) <= 4:
                    for idx, (k, v) in enumerate(s.init.pairs):
                        slot = f"_ws_lit{self.next_id()}"
                        self.w(f'{cname}->keys[{idx}] = ({{ static WellString *{slot}; '
                               f'well_string_lit_slot(&{slot}, "{c_escape(k)}"); }});')
                        self.w(f"{cname}->vals[{idx}].p = (void*){self.gen_expr(v)};")
                    self.w(f"{cname}->len = {len(s.init.pairs)};")
                else:
                    for k, v in s.init.pairs:
                        self.emit_map_set(cname, f'well_string_lit("{c_escape(k)}")', v, et)
            self.bind_ref(s.name, cname, 1)
            self.var_decl(s.name, ty)
            return
        if isinstance(s.init, New) and (self.container_info(s.init.type.name)[0] or getattr(s.init, "bytes_new", False)):
            self.w(f"{self.ctype(ty)} {cname} = {self.gen_expr(s.init)};")
            self.bind_ref(s.name, cname, 1)
            self.var_decl(s.name, ty)
            return
        if self.is_ref(ty):
            if isinstance(s.init, New):
                self.emit_new_into(cname, ty, s.init)
                self.bind_ref(s.name, cname, 1)
            else:
                self.w(f"{self.ctype(ty)} {cname} = {self.gen_expr(s.init)};")
                if self.is_owned(s.init):
                    self.bind_ref(s.name, cname, 1)
                else:
                    self.bind_ref(s.name, cname, 0)
            self.var_decl(s.name, ty)
        else:
            self.w(f"{self.ctype(ty)} {cname} = {self.gen_expr(s.init)};")
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
        ct = c_type(ty)
        self.w(f"{ct}* {var} = new_{ct}(ctx, {oid});")
        for fname, val in e.init:
            self.emit_field_assign(var, ty, fname, val, event=(kind == "event"))

    def emit_field_assign(self, obj: str, ty: str, fname: str, val: Expr, event=False):
        ft = self.field_type(ty, fname)
        cf = c_field(fname)
        if self.is_ref(ft):
            code = self.gen_expr(val)
            owned = self.is_owned(val)
            self.w("{")
            self.ind += 1
            self.w(f"{self.ctype(ft)} _fv = {code};")
            if not event and ft != "String":
                self.w(f"well_check_value_oid((void*)({obj}), (void*)_fv, ctx);")
            self.w(f"well_set_ref((void*)({obj}), (void**)&({obj})->{cf}, (void*)_fv);")
            if owned:
                self.w("well_release((void*)_fv);")
            self.ind -= 1
            self.w("}")
        else:
            self.w(f"({obj})->{cf} = {self.gen_expr(val)};")

    def _raw_expr(self, code: str, ty: str):
        """Synthetic Ident node that emits ``code`` verbatim (internal only)."""
        node = Ident("_raw")
        node.raw_c = code
        node.ty = ty
        return node

    def _hoist_assign_target(self, tgt):
        """Evaluate a Member/Index assignment target base (and index) exactly once.

        Returns ``(kind, extra_decls, restore)`` where ``kind`` is the target
        class, ``extra_decls`` is used for the index temp and ``restore`` undoes
        the temporary AST rewrite.  The caller must release the base temp when
        the base expression was owned.
        """
        oty = getattr(tgt.obj, "ty", None)
        base = f"_cb{self.next_id()}"
        self.w(f"{self.ctype(oty)} {base} = {self.gen_expr(tgt.obj)};")
        saved_obj = tgt.obj
        tgt.obj = self._raw_expr(base, oty)
        saved_idx = None
        if isinstance(tgt, Index):
            # Keep the key's declared type so a String map key still goes
            # through the string-arg journal helper on replay.
            idx_ty = getattr(tgt.index, "ty", None) or "long"
            idx = f"_ci{self.next_id()}"
            self.w(f"{self.ctype(idx_ty)} {idx} = {self.gen_expr(tgt.index)};")
            saved_idx = tgt.index
            tgt.index = self._raw_expr(idx, idx_ty)

        def restore():
            tgt.obj = saved_obj
            if saved_idx is not None:
                tgt.index = saved_idx

        return base, restore

    def gen_compound_assign(self, s) -> bool:
        """`x += y` / `x -= y` with Member/Index targets evaluated once.

        The parser rewrites the compound forms to ``x = x + y``; for a target
        like ``arr[idx()]`` that would evaluate ``idx()`` twice.  Hoist the base
        and index, then replay the ordinary assignment against the hoisted
        values so the read and the write share one evaluation.
        """
        tgt, val = s.target, s.value
        if not (isinstance(val, Binary) and val.op in ("+", "-") and val.left is tgt):
            return False
        if not isinstance(tgt, (Index, Member)):
            # Ident targets have no repeated side effect: the plain rewrite is
            # already single-evaluation.
            return False
        owned_base = self.is_owned(tgt.obj)
        self.w("{")
        self.ind += 1
        base, restore = self._hoist_assign_target(tgt)
        try:
            self.gen_assign(s, _replay=True)
        finally:
            restore()
        if owned_base:
            self.w(f"well_release((void*){base});")
        self.ind -= 1
        self.w("}")
        return True

    def gen_assign(self, s: Assign, _replay: bool = False):
        if not _replay and self.gen_compound_assign(s):
            return
        tgt = s.target
        if isinstance(tgt, Member) and isinstance(tgt.obj, Ident) and \
           tgt.obj.name in getattr(self.p, "statics", {}):
            sname = tgt.obj.name
            st = self.p.statics[sname]
            ftype = st["fields"][tgt.name].type.name
            fname = f'"{sname}.{tgt.name}"'
            cstat = c_static_field(sname, tgt.name)
            # read-only check first: RHS must not run (and allocate) when the
            # write is rejected
            self.w(f"well_static_check_write(ctx, {fname});")
            if isinstance(s.value, (ArrayLit, ObjectLit)):
                kind, et = self.container_info(ftype)
                if not kind:
                    raise Exception(f"container literal with non-container type {ftype}")
                tmp = f"_lit{self.next_id()}"
                self.w("{")
                self.ind += 1
                if kind == "array":
                    self.w(f"WellArray* {tmp} = {self.container_new_call('array', et)};")
                    for it in s.value.items:
                        self.emit_container_push(tmp, it, et)
                else:
                    self.w(f"WellMap* {tmp} = {self.container_new_call('map', et)};")
                    for k, v in s.value.pairs:
                        self.emit_map_set(tmp, f'well_string_lit("{c_escape(k)}")', v, et)
                self.w(f"well_static_set_ref(ctx, (void**)&{cstat}, (void*){tmp}, 1, {fname});")
                self.ind -= 1
                self.w("}")
                return
            code = self.gen_expr(s.value)
            if self.is_ref(ftype):
                owned = 1 if self.is_owned(s.value) else 0
                self.w(f"well_static_set_ref(ctx, (void**)&{cstat}, (void*)({code}), {owned}, {fname});")
            else:
                self.w(f"{cstat} = {code};")
            return
        if isinstance(tgt, Ident):
            ty = self.env.get(tgt.name)
            if ty is None:
                raise Exception(f"unknown variable {tgt.name}")
            self.assert_codegen_type(ty, s.value, f"assignment to {tgt.name}")
            cname = self.c_names.get(tgt.name) or c_var(tgt.name)
            if self.is_ref(ty):
                slot = self.journaled.get(tgt.name)
                replace = (f"well_ctx_replace_at(ctx, {slot}, "
                           f"(void**)&{cname}, (void*)%s, %d);" if slot
                           else f"well_ctx_replace(ctx, (void**)&{cname}, (void*)%s, %d);")
                if isinstance(s.value, New):
                    tmp = f"__new_{tgt.name}"
                    self.w("{")
                    self.ind += 1
                    self.emit_new_into(tmp, ty, s.value)
                    self.w(replace % (tmp, 1))
                    self.ind -= 1
                    self.w("}")
                else:
                    code = self.gen_expr(s.value)
                    self.w("{")
                    self.ind += 1
                    self.w(f"{self.ctype(ty)} _v = {code};")
                    owned = 1 if self.is_owned(s.value) else 0
                    self.w(replace % ("_v", owned))
                    self.ind -= 1
                    self.w("}")
            else:
                self.w(f"{cname} = {self.gen_expr(s.value)};")
            return
        if isinstance(tgt, Index):
            ot = getattr(tgt.obj, "ty", None)
            kind, et = self.container_info(ot)
            obj = self.gen_expr(tgt.obj)
            idx = self.gen_str_arg(tgt.index) if kind == "map" else self.gen_expr(tgt.index)
            if kind == "array":
                self.emit_array_set(obj, idx, s.value, et)
            elif kind == "map":
                if (isinstance(tgt.obj, Ident) and
                        tgt.obj.name in self.direct_fresh_map_names and
                        isinstance(tgt.index, StrLit)):
                    name = tgt.obj.name
                    keys = self.direct_map_keys.setdefault(name, {})
                    key = tgt.index.value
                    if key in keys:
                        ci = keys[key]
                    else:
                        ci = len(keys)
                        slot = f"_ws_lit{self.next_id()}"
                        self.w(f'{obj}->keys[{ci}] = ({{ static WellString *{slot}; '
                               f'well_string_lit_slot(&{slot}, "{c_escape(key)}"); }});')
                        self.w(f"{obj}->len++;")
                        keys[key] = ci
                    self.w(f"{obj}->vals[{ci}].p = "
                           f"(void*){self.gen_expr(s.value)};")
                elif (isinstance(tgt.obj, Ident) and
                        tgt.obj.name in self.direct_str_map_names and
                        isinstance(tgt.index, StrLit)):
                    name = tgt.obj.name
                    keys = self.direct_map_keys.setdefault(name, {})
                    key = tgt.index.value
                    if key in keys:
                        ci = keys[key]
                    else:
                        ci = len(keys)
                        slot = f"_ws_lit{self.next_id()}"
                        self.w(f'{obj}->keys[{ci}] = ({{ static WellString *{slot}; '
                               f'well_string_lit_slot(&{slot}, "{c_escape(key)}"); }});')
                        self.w(f"{obj}->len++;")
                        keys[key] = ci
                    self.w(f"{obj}->vals[{ci}].p = "
                           f"(void*){self.gen_expr(s.value)};")
                elif (isinstance(tgt.obj, Ident) and
                        tgt.obj.name in self.direct_map_names and
                        isinstance(tgt.index, StrLit)):
                    name = tgt.obj.name
                    keys = self.direct_map_keys.setdefault(name, {})
                    key = tgt.index.value
                    if key in keys:
                        ci = keys[key]
                    else:
                        ci = len(keys)
                        slot = f"_ws_lit{self.next_id()}"
                        self.w(f'{obj}->keys[{ci}] = ({{ static WellString *{slot}; '
                               f'well_string_lit_slot(&{slot}, "{c_escape(key)}"); }});')
                        self.w(f"{obj}->len++;")
                        keys[key] = ci
                    self.w(f"{obj}->vals[{ci}] = "
                           f"{self.box_value(self.gen_expr(s.value), et)};")
                elif (isinstance(tgt.obj, Ident) and
                        tgt.obj.name in self.stack_map_storage and
                        isinstance(tgt.index, StrLit)):
                    name = tgt.obj.name
                    st = self.stack_map_storage[name]
                    keys = self.stack_map_keys[name]
                    key = tgt.index.value
                    if key in keys:
                        ci = keys[key]
                    else:
                        ci = len(keys)
                        slot = f"_ws_lit{self.next_id()}"
                        self.w(f'{st}.keys[{ci}] = ({{ static WellString *{slot}; '
                               f'well_string_lit_slot(&{slot}, "{c_escape(key)}"); }});')
                        self.w(f"{st}.len++;")
                        keys[key] = ci
                    self.w(f"{st}.vals[{ci}] = "
                           f"{self.box_value(self.gen_expr(s.value), et)};")
                else:
                    self.emit_map_set(obj, idx, s.value, et)
            else:
                raise Exception("index assignment on non-container")
            return
        if isinstance(tgt, Member):
            oty = getattr(tgt.obj, "ty", None)
            ft = self.field_type(oty, tgt.name) if oty else "int"
            self.assert_codegen_type(ft, s.value, f"field write {tgt.name}")
            src_chk = ('well_check_oid_at((void*)_t, ctx, "field write", '
                       f'"{c_escape(str(getattr(s, "file", "") or ""))}", '
                       f'{int(getattr(s, "line", 0) or 0)});')
            if self.is_ref(ft):
                obj = self.gen_expr(tgt.obj)
                code = self.gen_expr(s.value)
                owned = self.is_owned(s.value)
                self.w("{")
                self.ind += 1
                self.w(f"{self.ctype(oty)} _t = {obj};")
                self.w(src_chk)
                self.w(f"{self.ctype(ft)} _v = {code};")
                if ft != "String":
                    self.w("well_check_value_oid((void*)_t, (void*)_v, ctx);")
                self.w(f"well_set_ref((void*)_t, (void**)&_t->{c_field(tgt.name)}, (void*)_v);")
                if owned:
                    self.w("well_release((void*)_v);")
                if self.is_owned(tgt.obj):
                    self.w("well_release((void*)_t);")
                self.ind -= 1
                self.w("}")
            else:
                obj = self.gen_expr(tgt.obj)
                if isinstance(tgt.obj, Ident) and tgt.obj.name in self.stack_vars:
                    self.w(f"{obj}->{c_field(tgt.name)} = {self.gen_expr(s.value)};")
                    return
                self.w("{")
                self.ind += 1
                self.w(f"{self.ctype(oty)} _t = {obj};")
                self.w(src_chk)
                self.w(f"_t->{c_field(tgt.name)} = {self.gen_expr(s.value)};")
                if self.is_owned(tgt.obj):
                    self.w("well_release((void*)_t);")
                self.ind -= 1
                self.w("}")
            return
        raise Exception("bad assignment target")

    def emit_print_str(self, code: str, node, fn: str):
        if not self.is_owned(node):
            self.w(f"{fn}({code});")
            return
        tmp = f"_ps{self.next_id()}"
        self.w("{")
        self.ind += 1
        self.w(f"WellString* {tmp} = {code};")
        self.w(f"{fn}({tmp});")
        self.w(f"well_release((void*){tmp});")
        self.ind -= 1
        self.w("}")

    def gen_expr_stmt(self, s: ExprStmt):
        e = s.expr
        if isinstance(e, Call) and getattr(e, "builtin", None) == "print":
            self.w("well_print_begin();")
            for a in e.args:
                ty = getattr(a, "ty", "int")
                code = self.gen_expr(a)
                if ty == "String":
                    self.emit_print_str(code, a, "well_print_str")
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
                    self.emit_print_str(code, a, "well_log_str")
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
            if (isinstance(e.method_obj, Ident)
                    and e.method_obj.name in self.stack_array_storage):
                st = self.stack_array_storage[e.method_obj.name]
                self.w(f"{st}.items[{st}.len++] = "
                       f"{self.box_value(self.gen_expr(e.args[0]), e.elem_ty)};")
            elif (isinstance(e.method_obj, Ident)
                    and e.method_obj.name in self.direct_push_names):
                obj = self.gen_expr(e.method_obj)
                self.w(f"{obj}->items[{obj}->len++] = "
                       f"{self.box_value(self.gen_expr(e.args[0]), e.elem_ty)};")
            elif (isinstance(e.method_obj, Ident)
                    and e.method_obj.name in self.direct_str_push_names):
                obj = self.gen_expr(e.method_obj)
                self.w(f"{obj}->items[{obj}->len++].p = "
                       f"(void*){self.gen_expr(e.args[0])};")
            elif (isinstance(e.method_obj, Ident)
                    and e.method_obj.name in self.direct_fresh_push_names):
                obj = self.gen_expr(e.method_obj)
                self.w(f"{obj}->items[{obj}->len++].p = "
                       f"(void*){self.gen_expr(e.args[0])};")
            else:
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
        # Evaluate the result *before* releasing locals: `return a.value` must
        # read the object while it is still alive, and a scalar result may be
        # computed from a binding the cleanup is about to release.
        tmp = f"_rv{self.next_id()}"
        self.w("{")
        self.ind += 1
        if self.is_ref(rt):
            self.w(f"{self.ctype(rt)} {tmp} = {self.gen_expr(s.value)};")
            if not self.is_owned(s.value):
                self.w(f"well_retain((void*){tmp});")
        else:
            self.w(f"{self.ctype(rt)} {tmp} = {self.gen_expr(s.value)};")
        self.emit_return_cleanup()
        self.w(f"return {tmp};")
        self.ind -= 1
        self.w("}")

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
        self.w("#include <string.h>")
        self.w("")
        self.gen_enums_consts()
        self.gen_enum_helpers()
        self.gen_type_defs()
        self.gen_type_meta()
        self.gen_constructors()
        self.gen_destroyers()
        self.gen_static_decls()
        self.gen_prototypes()
        self.gen_http_routes()
        self.gen_ws_routes()
        self.gen_timer_wrappers()
        self.gen_user_funcs()
        self.gen_namespaces()
        self.gen_static_funcs()
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
                    # Enum members are values, not C literals: a bare "red" is
                    # a char* and crashes the moment it is used as a String.
                    self.w(f'#define {macro} well_string_lit("{c_escape(val)}")')
                else:
                    if mv is None:
                        val = auto
                    else:
                        val = mv
                    self.w(f"#define {macro} {val}")
                    auto = val + 1
        for name, d in self.p.consts.items():
            ty = d.type.name
            if ty == "String" and isinstance(d.value, StrLit):
                self.w(f'#define {c_const(name)} well_string_lit("{c_escape(d.value.value)}")')
            else:
                # String consts may also be another const or a String enum
                # member; those must go through gen_expr (a plain `"..."` or
                # `getattr(..., "value", "")` would emit an empty/native C
                # string instead of a WellString*).
                self.w(f"#define {c_const(name)} ({self.gen_expr(d.value)})")
        self.w("")

    def gen_type_defs(self):
        for name in self.type_ids:
            self.w(f"typedef struct {c_type(name)} {c_type(name)};")
        self.w("")
        for name, tid in self.type_ids.items():
            fields = self.p.structs[name]["fields"]
            self.w(f"struct {c_type(name)} {{")
            self.ind += 1
            self.w("WellObj hdr;")
            for f in fields:
                self.w(f"{self.ctype(f.type.name)} {c_field(f.name)};")
            self.ind -= 1
            self.w("};")
            self.w(f"#define T_{c_type(name)} {tid}")
        self.w("")

    def gen_constructors(self):
        for name in self.type_ids:
            ct = c_type(name)
            self.w(f"static {ct}* new_{ct}(WellCtx* ctx, long long oid);")
        self.w("")
        for name in self.type_ids:
            kind = self.type_kind[name]
            ct = c_type(name)
            fields = self.p.structs[name]["fields"]
            # Plain scalar structs cannot form cycles: allocate outside the oid
            # registry and initialize fields explicitly instead of memset + add.
            plain = kind != "event" and all(not self.is_ref(f.type.name) for f in fields)
            self.w(f"static {ct}* new_{ct}(WellCtx* ctx, long long oid) {{")
            self.ind += 1
            self.w("(void)ctx;")
            if kind == "event":
                self.w("if (oid == 0) oid = well_id_util_get_id();")
            alloc = "well_new_uninit_unregistered" if plain else "well_new"
            self.w(f"{ct}* o = ({ct}*){alloc}(sizeof({ct}), T_{ct}, oid);")
            for f in fields:
                ty = f.type.name
                if f.default is not None:
                    val = f.default
                    self.require_const_default(val, f"field default {name}.{f.name}")
                    if self.is_ref(ty):
                        self.w(f"well_set_ref((void*)o, (void**)&o->{c_field(f.name)}, (void*)({self.gen_expr(val)}));")
                    else:
                        self.w(f"o->{c_field(f.name)} = {self.gen_expr(val)};")
                elif ty == "String":
                    self.w(f'o->{c_field(f.name)} = well_string_lit("");')
                elif plain:
                    self.w(f"o->{c_field(f.name)} = 0;")
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
            return "WELL_FIELD_REF", f"T_{c_type(ty)}", "0", "0"
        return "WELL_FIELD_INT", "0", "0", "0"

    def gen_type_meta(self):
        for name in self.type_ids:
            ct = c_type(name)
            fields = self.p.structs[name]["fields"]
            if fields:
                self.w(f"static const WellFieldDesc fields_{ct}[] = {{")
                self.ind += 1
                for f in fields:
                    kind, tid, ek, etid = self.meta_field_info(f.type.name)
                    # `f.name` stays the Well spelling: it is the serialized /
                    # JSON field name and part of the public runtime metadata.
                    self.w(f'{{"{f.name}", offsetof({ct}, {c_field(f.name)}), '
                           f'{kind}, {tid}, {ek}, {etid}}},')
                self.ind -= 1
                self.w("};")
            self.w(f"static const WellTypeDesc desc_{ct} = {{")
            self.ind += 1
            self.w(f'"{name}", T_{ct}, sizeof({ct}), '
                   f'{1 if self.type_kind[name] == "event" else 0},')
            self.w(f'{len(fields)}, {"fields_" + ct if fields else "NULL"}')
            self.ind -= 1
            self.w("};")
            self.w("")

    def gen_destroyers(self):
        for name in self.type_ids:
            ct = c_type(name)
            fields = self.p.structs[name]["fields"]
            self.w(f"static void destroy_{ct}(void* p) {{")
            self.ind += 1
            self.w(f"{ct}* o = ({ct}*)p;")
            for f in fields:
                if self.is_ref(f.type.name):
                    self.w(f"well_release((void*)o->{c_field(f.name)});")
            self.w("well_obj_free(o);")
            self.ind -= 1
            self.w("}")
        self.w("")

    def gen_prototypes(self):
        for name, f in self.p.funcs.items():
            ret = self.ctype(f.ret.name) if f.ret else "void"
            params = ", ".join(["WellCtx* ctx"] + [f"{self.ctype(t.name)} {c_var(n)}" for n, t in f.params])
            self.w(f"static {ret} {c_func(name)}({params});")
        for sname, st in getattr(self.p, "statics", {}).items():
            for fname, f in st["funcs"].items():
                ret = self.ctype(f.ret.name) if f.ret else "void"
                params = ", ".join(["WellCtx* ctx"] + [f"{self.ctype(t.name)} {c_var(n)}" for n, t in f.params])
                self.w(f"static {ret} {c_func(sname + '__' + fname)}({params});")
        for ns, funcs in getattr(self.p, "namespaces", {}).items():
            for fname, f in funcs.items():
                ret = self.ctype(f.ret.name) if f.ret else "void"
                params = ", ".join(["WellCtx* ctx"] + [f"{self.ctype(t.name)} {c_var(n)}" for n, t in f.params])
                self.w(f"static {ret} {c_func(ns + '__' + fname)}({params});")
        for name in self.p.controllers:
            self.w(f"static void {c_ctl(name)}_ensure(WellCtx* ctx);")
            self.w(f"static void {c_ctl(name)}_stop(WellCtx* ctx);")
            self.w(f"static void {c_ctl(name)}_run(WellCtx* ctx);")
        for name, h in self.p.handlers.items():
            self.w(f"static void {c_handler(name)}_onEvent(WellCtx* ctx, void* evp);")
        self.w("")

    def _event_param_written(self, node, name: str) -> bool:
        """True when the handler assigns to a field of the event parameter.

        A frame-allocated event borrows its String fields (no retain), so a
        write through the parameter must keep the heap path where the event
        owns its edges.
        """
        if isinstance(node, Assign):
            t = node.target
            while isinstance(t, (Member, Index)):
                t = t.obj
            if isinstance(t, Ident) and t.name == name:
                return True
        for child in self._children(node):
            if self._event_param_written(child, name):
                return True
        return False

    def _stack_event_ok(self, handler_name: str) -> bool:
        """True when a route handler only reads fields of its event parameter.

        In that case the route wrapper can build the event in the C frame: the
        parameter never escapes, so the event does not need a heap allocation
        or retained String fields (the message/request owns them for the whole
        call).  A handler that passes the event itself anywhere (call argument,
        assignment, container/Model store) or writes one of its fields fails
        this check and keeps the heap path.
        """
        mname = getattr(self.p, "handler_method", {}).get(handler_name)
        h = getattr(self.p, "handlers", {}).get(handler_name)
        if not mname or not h:
            return False
        for m in h.methods:
            if m.name == mname and m.params:
                pname = m.params[0][0]
                if self._event_param_written(m.body, pname):
                    return False
                return self._uses_only_fields(m.body, pname)
        return False

    def gen_http_routes(self):
        routes = getattr(self.p, "http_routes", [])
        for i, r in enumerate(routes):
            ev = r["event"]
            h = r["handler"]
            ct = c_type(ev)
            self.w(f"static void http_route_{i}(WellCtx* ctx, WellHttpRequest* req) {{")
            self.ind += 1
            # Events are immutable and only reference immutable Strings, so the
            # route wrapper can skip the generic constructor's empty defaults
            # and the per-field graph edges; retain the request Strings directly.
            # The event is unregistered: it is created and released on the
            # connection thread before anyone can look it up by oid (V4 perf).
            if self._stack_event_ok(h):
                # Non-escaping handler: build the event in the C frame and
                # borrow the request strings (no alloc, no retain/release).
                self.w(f"{ct} e;")
                self.w("memset(&e, 0, sizeof(e));")
                self.w("e.hdr.oid = req->id;")
                self.w(f"e.hdr.type_id = T_{ct};")
                self.w("e.hdr.rc = 1;")
                self.w("e.hdr.flags = WELL_FLAG_STACK;")
                self.w(f"e.{c_field('reqId')} = req->id;")
                for fn in ("method", "path", "query", "body"):
                    self.w(f"e.{c_field(fn)} = req->{fn};")
                self.w("well_freeze((void*)&e);")
                self.w(f"{c_handler(h)}_onEvent(ctx, (void*)&e);")
            else:
                self.w(f"{ct}* e = ({ct}*)well_new_zero_unregistered(sizeof({ct}), T_{ct}, req->id);")
                self.w(f"e->{c_field('reqId')} = req->id;")
                for fn in ("method", "path", "query", "body"):
                    f = c_field(fn)
                    self.w(f"e->{f} = req->{fn};")
                    self.w(f"well_retain((void*)e->{f});")
                self.w("well_freeze((void*)e);")
                self.w(f"{c_handler(h)}_onEvent(ctx, (void*)e);")
                self.w("well_release((void*)e);")
            self.ind -= 1
            self.w("}")
            self.w("")

    def _event_has_defaults(self, ev: str) -> bool:
        d = self.p.structs.get(ev)
        return bool(d) and any(f.default is not None for f in d["fields"])

    def gen_ws_routes(self):
        routes = getattr(self.p, "ws_routes", [])
        for i, r in enumerate(routes):
            oe, me, ce = r["open_ev"], r["msg_ev"], r["close_ev"]
            oh, mh, ch = r["open"], r["msg"], r["close"]
            oct_, mct, cct = c_type(oe), c_type(me), c_type(ce)
            open_stack = self._stack_event_ok(oh) and not self._event_has_defaults(oe)
            msg_stack = self._stack_event_ok(mh) and not self._event_has_defaults(me)
            close_stack = self._stack_event_ok(ch) and not self._event_has_defaults(ce)
            self.w(f"static void ws_open_{i}(WellCtx* ctx, WellWsClient* c) {{")
            self.ind += 1
            if open_stack:
                self.w(f"{oct_} e;")
                self.w("memset(&e, 0, sizeof(e));")
                self.w("e.hdr.oid = c->id;")
                self.w(f"e.hdr.type_id = T_{oct_};")
                self.w("e.hdr.rc = 1;")
                self.w("e.hdr.flags = WELL_FLAG_STACK;")
                self.w(f"e.{c_field('clientId')} = c->id;")
                self.w(f"e.{c_field('path')} = c->path;")
                self.w(f"e.{c_field('query')} = c->query;")
                self.w(f"{c_handler(oh)}_onEvent(ctx, (void*)&e);")
            else:
                self.w(f"{oct_}* e = new_{oct_}(ctx, c->id);")
                self.w(f"e->{c_field('clientId')} = c->id;")
                self.w(f"well_set_ref((void*)e, (void**)&e->{c_field('path')}, (void*)c->path);")
                self.w(f"well_set_ref((void*)e, (void**)&e->{c_field('query')}, (void*)c->query);")
                self.w(f"{c_handler(oh)}_onEvent(ctx, (void*)e);")
                self.w("well_release((void*)e);")
            self.ind -= 1
            self.w("}")
            self.w(f"static void ws_msg_{i}(WellCtx* ctx, WellWsClient* c, WellString* text) {{")
            self.ind += 1
            if msg_stack:
                self.w(f"{mct} e;")
                self.w("memset(&e, 0, sizeof(e));")
                self.w("e.hdr.oid = c->id;")
                self.w(f"e.hdr.type_id = T_{mct};")
                self.w("e.hdr.rc = 1;")
                self.w("e.hdr.flags = WELL_FLAG_STACK;")
                self.w(f"e.{c_field('clientId')} = c->id;")
                self.w(f"e.{c_field('text')} = text;")
                self.w(f"{c_handler(mh)}_onEvent(ctx, (void*)&e);")
            else:
                self.w(f"{mct}* e = new_{mct}(ctx, c->id);")
                self.w(f"e->{c_field('clientId')} = c->id;")
                self.w(f"well_set_ref((void*)e, (void**)&e->{c_field('text')}, (void*)text);")
                self.w(f"{c_handler(mh)}_onEvent(ctx, (void*)e);")
                self.w("well_release((void*)e);")
            self.ind -= 1
            self.w("}")
            self.w(f"static void ws_close_{i}(WellCtx* ctx, WellWsClient* c, int code, WellString* reason) {{")
            self.ind += 1
            if close_stack:
                self.w(f"{cct} e;")
                self.w("memset(&e, 0, sizeof(e));")
                self.w("e.hdr.oid = c->id;")
                self.w(f"e.hdr.type_id = T_{cct};")
                self.w("e.hdr.rc = 1;")
                self.w("e.hdr.flags = WELL_FLAG_STACK;")
                self.w(f"e.{c_field('clientId')} = c->id;")
                self.w(f"e.{c_field('code')} = code;")
                self.w(f"e.{c_field('reason')} = reason;")
                self.w(f"{c_handler(ch)}_onEvent(ctx, (void*)&e);")
            else:
                self.w(f"{cct}* e = new_{cct}(ctx, c->id);")
                self.w(f"e->{c_field('clientId')} = c->id;")
                self.w(f"e->{c_field('code')} = code;")
                self.w(f"well_set_ref((void*)e, (void**)&e->{c_field('reason')}, (void*)reason);")
                self.w(f"{c_handler(ch)}_onEvent(ctx, (void*)e);")
                self.w("well_release((void*)e);")
            self.ind -= 1
            self.w("}")
            self.w("")

    def begin_frame_temp(self):
        tid = self.next_id()
        name = f"_tm{tid}"
        self.w(f"int {name} = well_temp_mark();")
        old = self.temp_mark
        self.temp_mark = name
        return name, old

    def end_frame_temp(self, name: str, old):
        self.w(f"well_temp_release_to({name});")
        self.temp_mark = old

    def gen_timer_wrappers(self):
        for i, c in enumerate(getattr(self.p, "timer_calls", [])):
            ev = c["event"]
            h = c["handler"]
            ct = c_type(ev)
            fields = set()
            if ev in self.p.structs:
                fields = {f.name for f in self.p.structs[ev]["fields"]}
            self.w(f"static void timer_fire_{i}(WellCtx* ctx, void* arg) {{")
            self.ind += 1
            self.w("WellTimerInfo* ti = (WellTimerInfo*)arg;")
            self.w(f"{ct}* e = new_{ct}(ctx, ti->id);")
            if "id" in fields:
                self.w(f"e->{c_field('id')} = ti->id;")
            if "interval" in fields:
                self.w(f"e->{c_field('interval')} = ti->interval;")
            if "fires" in fields:
                self.w(f"e->{c_field('fires')} = ti->fires;")
            self.w(f"{c_handler(h)}_onEvent(ctx, (void*)e);")
            self.w("well_release((void*)e);")
            self.ind -= 1
            self.w("}")
            self.w("")

    def gen_user_funcs(self):
        for name, f in self.p.funcs.items():
            ret = self.ctype(f.ret.name) if f.ret else "void"
            params = ", ".join(["WellCtx* ctx"] + [f"{self.ctype(t.name)} {c_var(n)}" for n, t in f.params])
            self.w(f"static {ret} {c_func(name)}({params}) {{")
            self.ind += 1
            self.w("(void)ctx;")
            self.set_params_env(f.params)
            self.current_ret = f.ret.name if f.ret else "void"
            m = self.next_id()
            mk = f"_m{m}"
            self.w(f"int {mk} = well_ctx_mark(ctx);")
            tm, old_tm = self.begin_frame_temp()
            self.push_scope(mk, False)
            self.bind_params(f.params, f.body)
            self.gen_block(f.body)
            self.emit_scope_release(mk, final=True)
            self.pop_scope(mk)
            self.end_frame_temp(tm, old_tm)
            self.ind -= 1
            self.w("}")
            self.w("")

    def gen_namespaces(self):
        for sname, st in getattr(self.p, "statics", {}).items():
            for fname, f in st["funcs"].items():
                ret = self.ctype(f.ret.name) if f.ret else "void"
                params = ", ".join(["WellCtx* ctx"] + [f"{self.ctype(t.name)} {c_var(n)}" for n, t in f.params])
                self.w(f"static {ret} {c_func(sname + '__' + fname)}({params});")
        for ns, funcs in getattr(self.p, "namespaces", {}).items():
            for fname, f in funcs.items():
                ret = self.ctype(f.ret.name) if f.ret else "void"
                params = ", ".join(["WellCtx* ctx"] + [f"{self.ctype(t.name)} {c_var(n)}" for n, t in f.params])
                self.w(f"static {ret} {c_func(ns + '__' + fname)}({params}) {{")
                self.ind += 1
                self.w("(void)ctx;")
                self.set_params_env(f.params)
                self.current_ret = f.ret.name if f.ret else "void"
                m = self.next_id()
                mk = f"_m{m}"
                self.w(f"int {mk} = well_ctx_mark(ctx);")
                tm, old_tm = self.begin_frame_temp()
                self.push_scope(mk, False)
                self.bind_params(f.params, f.body)
                self.gen_block(f.body)
                self.emit_scope_release(mk, final=True)
                self.pop_scope(mk)
                self.end_frame_temp(tm, old_tm)
                self.ind -= 1
                self.w("}")
                self.w("")

    def gen_static_funcs(self):
        for sname, st in getattr(self.p, "statics", {}).items():
            for fname, f in st["funcs"].items():
                ret = self.ctype(f.ret.name) if f.ret else "void"
                params = ", ".join(["WellCtx* ctx"] + [f"{self.ctype(t.name)} {c_var(n)}" for n, t in f.params])
                self.w(f"static {ret} {c_func(sname + '__' + fname)}({params}) {{")
                self.ind += 1
                self.w("(void)ctx;")
                self.set_params_env(f.params)
                self.current_ret = f.ret.name if f.ret else "void"
                m = self.next_id()
                mk = f"_m{m}"
                self.w(f"int {mk} = well_ctx_mark(ctx);")
                tm, old_tm = self.begin_frame_temp()
                self.push_scope(mk, False)
                self.bind_params(f.params, f.body)
                self.gen_block(f.body)
                self.emit_scope_release(mk, final=True)
                self.pop_scope(mk)
                self.end_frame_temp(tm, old_tm)
                self.ind -= 1
                self.w("}")
                self.w("")

    def gen_static_decls(self):
        statics = getattr(self.p, "statics", {})
        if not statics:
            return
        for sname, st in statics.items():
            for fname, f in st["fields"].items():
                cty = self.ctype(f.type.name)
                cstat = c_static_field(sname, fname)
                if self.is_ref(f.type.name):
                    self.w(f"static {cty} {cstat} = NULL;")
                elif f.default is not None:
                    self.require_const_default(f.default, f"static {sname}.{fname} default")
                    self.w(f"static {cty} {cstat} = {self.gen_expr(f.default)};")
                else:
                    zero = "0" if cty != "double" else "0.0"
                    if cty == "int" or cty == "long long":
                        zero = "0"
                    self.w(f"static {cty} {cstat} = {zero};")
        self.w("")
        self.w("static void well_statics_init(void) {")
        self.ind += 1
        need_ctx = False
        for sname, st in statics.items():
            for fname, f in st["fields"].items():
                d = f.default
                if self.is_ref(f.type.name) and d is not None and not isinstance(d, (StrLit, NullLit)):
                    need_ctx = True
        if need_ctx:
            self.w("WellCtx _static_ctx;")
            self.w("well_ctx_init(&_static_ctx, well_static_owner_oid());")
            self.w("WellCtx* ctx = &_static_ctx;")
        for sname, st in statics.items():
            for fname, f in st["fields"].items():
                d = f.default
                if not self.is_ref(f.type.name) or d is None:
                    continue
                cstat = c_static_field(sname, fname)
                if isinstance(d, StrLit):
                    self.w(f"{cstat} = well_string_lit(\"{c_escape(d.value)}\");")
                elif isinstance(d, NullLit):
                    continue
                elif isinstance(d, ArrayLit):
                    kind, et = self.container_info(f.type.name)
                    tmp = f"_sf{self.next_id()}"
                    self.w(f"WellArray* {tmp} = {self.container_new_call('array', et)};")
                    for it in d.items:
                        self.emit_container_push(tmp, it, et)
                    self.w(f"{cstat} = {tmp};")
                elif isinstance(d, ObjectLit):
                    kind, et = self.container_info(f.type.name)
                    tmp = f"_sf{self.next_id()}"
                    self.w(f"WellMap* {tmp} = {self.container_new_call('map', et)};")
                    for k, v in d.pairs:
                        self.emit_map_set(tmp, f'well_string_lit("{c_escape(k)}")', v, et)
                    self.w(f"{cstat} = {tmp};")
                else:
                    code = self.gen_expr(d)
                    self.w(f"{cstat} = {code};")
                    if isinstance(d, Ident):
                        self.w(f"if ({cstat}) well_retain((void*){cstat});")
        if need_ctx:
            self.w("well_ctx_clear(ctx);")
        self.ind -= 1
        self.w("}")
        self.w("")
        self.w("static void well_statics_shutdown(void) {")
        self.ind += 1
        for sname, st in statics.items():
            for fname, f in st["fields"].items():
                if self.is_ref(f.type.name):
                    cstat = c_static_field(sname, fname)
                    self.w(f"if ({cstat}) {{ well_release((void*){cstat}); {cstat} = NULL; }}")
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
            ctl = c_ctl(name)
            self.w(f"static int {ctl}_started = 0;")
            self.w(f"static void {ctl}_ensure(WellCtx* ctx) {{")
            self.ind += 1
            self.w(f"if ({ctl}_started) return;")
            self.w(f"{ctl}_started = 1;")
            self.w(f"well_owner_push({self.controller_ids[name]});")
            self.w(f"{ctl}_run(ctx);")
            self.w("well_owner_pop();")
            self.ind -= 1
            self.w("}")
            self.w(f"static void {ctl}_stop(WellCtx* ctx) {{")
            self.ind += 1
            self.w("(void)ctx;")
            self.w(f"if (!{ctl}_started) return;")
            self.w(f"{ctl}_started = 0;")
            self.w(f"well_off_owner({self.controller_ids[name]});")
            self.ind -= 1
            self.w("}")
            self.w(f"static void {ctl}_run(WellCtx* ctx) {{")
            self.ind += 1
            self.env = {}
            self.c_names = {}
            self.journaled = {}
            self.current_ret = "void"
            m = self.next_id()
            mk = f"_m{m}"
            self.w(f"int {mk} = well_ctx_mark(ctx);")
            tm, old_tm = self.begin_frame_temp()
            self.push_scope(mk, False)
            if run:
                self.gen_block(run.body)
            self.emit_scope_release(mk, final=True)
            self.pop_scope(mk)
            self.end_frame_temp(tm, old_tm)
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
            pname = m.params[0][0]
            cpname = c_var(pname)
            ct = c_type(ev)
            self.w(f"static void {c_handler(name)}_onEvent(WellCtx* ctx, void* evp) {{")
            self.ind += 1
            self.w(f"{ct}* {cpname} = ({ct}*)evp;")
            self.w("long long oid = ctx->oid;")
            self.w(f"(void){cpname};")
            self.w("(void)oid;")
            self.env = {pname: ev, "oid": "long"}
            self.c_names = {pname: cpname, "oid": "oid"}
            self.journaled = {}
            self.current_ret = "void"
            mmark = self.next_id()
            mk = f"_m{mmark}"
            self.w(f"int {mk} = well_ctx_mark(ctx);")
            tm, old_tm = self.begin_frame_temp()
            self.push_scope(mk, False)
            # The scheduler already pins the event payload for the duration of
            # the handler, so the parameter only needs its own slot when the
            # body reassigns it (otherwise the old pointer-scan could rewrite a
            # different binding's journal entry).
            if m and pname in self.assigned_names(m.body):
                self.bind_ref(pname, cpname, 0)
            if m:
                self.gen_block(m.body)
            self.emit_scope_release(mk, final=True)
            self.pop_scope(mk)
            self.end_frame_temp(tm, old_tm)
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
            ct = c_type(name)
            self.w(f"well_register_type(T_{ct}, destroy_{ct});")
            self.w(f"well_register_type_desc(&desc_{ct});")
        if not self.entry:
            self.w('fprintf(stderr, "no entry controller\\n");')
            self.w("return 1;")
        else:
            self.w("{")
            self.ind += 1
            self.w("WellCtx ctx;")
            self.w("well_ctx_init(&ctx, 0);")
            self.w("well_static_owner_set(ctx.oid);")
            if getattr(self.p, "statics", {}):
                self.w("well_statics_init();")
            self.w(f"{c_ctl(self.entry)}_ensure(&ctx);")
            self.w("well_ctx_clear(&ctx);")
            self.ind -= 1
            self.w("}")
            self.w("well_run_loop();")
        if getattr(self.p, "statics", {}):
            self.w("well_statics_shutdown();")
        self.w("well_shutdown();")
        self.w("return 0;")
        self.ind -= 1
        self.w("}")
