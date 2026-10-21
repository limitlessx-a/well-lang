"""Recursive-descent parser for the Well MVP."""
from .lexer import Token
from .ast_nodes import *


class ParseError(Exception):
    pass


class Parser:
    def __init__(self, tokens: list[Token], filename: str = "<src>"):
        self.toks = tokens
        self.i = 0
        self.fn = filename

    # ---------- token helpers ----------
    def peek(self, k=0) -> Token:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else self.toks[-1]

    def at(self, value: str) -> bool:
        t = self.peek()
        return t.value == value and t.kind in ("op", "kw")

    def at_id(self) -> bool:
        return self.peek().kind == "id"

    def eat(self, value: str) -> Token:
        t = self.peek()
        if t.value != value or t.kind not in ("op", "kw"):
            self.err(f"expected {value!r}, got {t.value!r}")
        self.i += 1
        return t

    def eat_id(self) -> str:
        t = self.peek()
        if t.kind != "id":
            self.err(f"expected identifier, got {t.value!r}")
        self.i += 1
        return t.value

    def eat_kw(self, value: str) -> Token:
        t = self.peek()
        if t.kind != "kw" or t.value != value:
            self.err(f"expected keyword {value!r}, got {t.value!r}")
        self.i += 1
        return t

    def eat_any_kw(self, values) -> str:
        t = self.peek()
        if t.kind != "kw" or t.value not in values:
            self.err(f"expected one of {values}, got {t.value!r}")
        self.i += 1
        return t.value

    def err(self, msg: str):
        t = self.peek()
        raise ParseError(f"{self.fn}:{t.line}:{t.col}: {msg}")

    # ---------- types ----------
    def parse_type(self) -> TypeRef:
        t = self.peek()
        if t.kind == "id":
            name = t.value
            self.i += 1
        elif t.kind == "kw" and t.value in ("bool", "int", "long", "float", "double", "String", "void"):
            name = t.value
            self.i += 1
        else:
            self.err(f"expected type name, got {t.value!r}")
        args = []
        if self.at("<"):
            self.eat("<")
            args.append(self.parse_type())
            while self.at(","):
                self.eat(",")
                args.append(self.parse_type())
            self.eat(">")
            name = f"{name}<{','.join(a.name for a in args)}>"
        nullable = False
        if self.at("?"):
            self.eat("?")
            nullable = True
        return TypeRef(name, args, nullable)

    # ---------- program ----------
    def parse_program(self) -> Program:
        prog = Program()
        while self.peek().kind != "eof":
            if self.at("module"):
                self.eat("module")
                parts = [self.eat_id()]
                while self.at("."):
                    self.eat(".")
                    parts.append(self.eat_id())
                self.eat(";")
                prog.module = ".".join(parts)
            elif self.at("import"):
                self.eat("import")
                if self.peek().kind == "str":
                    path = self.peek().value
                    self.i += 1
                else:
                    parts = [self.eat_id()]
                    while self.at("."):
                        self.eat(".")
                        parts.append(self.eat_id())
                    path = ".".join(parts)
                self.eat(";")
                prog.imports.append(ImportDecl(path))
            elif self.at("entry"):
                self.eat("entry")
                prog.entry = self.eat_id()
                self.eat(";")
            elif self.at("const"):
                prog.decls.append(self.parse_const())
            elif self.at("enum"):
                prog.decls.append(self.parse_enum())
            elif self.at("struct"):
                prog.decls.append(self.parse_struct(False))
            elif self.at("event"):
                prog.decls.append(self.parse_struct(True))
            elif self.at("controller"):
                prog.decls.append(self.parse_controller())
            elif self.at("handler"):
                prog.decls.append(self.parse_handler())
            elif self.at("func"):
                prog.decls.append(self.parse_func())
            else:
                self.err(f"unexpected top-level token {self.peek().value!r}")
        return prog

    def parse_const(self) -> ConstDecl:
        self.eat("const")
        name = self.eat_id()
        self.eat(":")
        ty = self.parse_type()
        self.eat("=")
        val = self.parse_expr()
        self.eat(";")
        return ConstDecl(name, ty, val)

    def parse_enum(self) -> EnumDecl:
        self.eat("enum")
        name = self.eat_id()
        if self.at(":"):
            self.eat(":")
            self.parse_type()
        self.eat("{")
        members = []
        while not self.at("}"):
            mname = self.eat_id()
            value = None
            if self.at("="):
                self.eat("=")
                t = self.peek()
                if t.kind == "int":
                    value = int(t.value, 0)
                    self.i += 1
                elif t.kind == "str":
                    value = t.value
                    self.i += 1
                elif t.kind == "kw" and t.value in ("true", "false"):
                    value = t.value == "true"
                    self.i += 1
                else:
                    self.err("enum value must be int/string/bool literal")
            members.append((mname, value))
            if self.at(";"):
                self.eat(";")
            elif not self.at("}"):
                self.err("expected ';' or '}' in enum")
        self.eat("}")
        return EnumDecl(name, members)

    def parse_struct(self, is_event: bool) -> StructDecl:
        self.eat_any_kw(("struct", "event"))
        name = self.eat_id()
        base = None
        if self.at("extends"):
            self.eat("extends")
            base = self.eat_id()
        self.eat("{")
        fields = []
        while not self.at("}"):
            fname = self.eat_id()
            self.eat(":")
            ty = self.parse_type()
            default = None
            if self.at("="):
                self.eat("=")
                default = self.parse_expr()
            self.eat(";")
            fields.append(FieldDecl(fname, ty, default))
        self.eat("}")
        return StructDecl(name, base, fields, is_event)

    def parse_params(self) -> list[tuple[str, TypeRef]]:
        self.eat("(")
        params = []
        if not self.at(")"):
            while True:
                pname = self.eat_id()
                self.eat(":")
                ptype = self.parse_type()
                params.append((pname, ptype))
                if not self.at(","):
                    break
                self.eat(",")
        self.eat(")")
        return params

    def parse_opt_ret(self):
        if self.at(":"):
            self.eat(":")
            return self.parse_type()
        return None

    def parse_method(self) -> MethodDecl:
        name = self.eat_id()
        params = self.parse_params()
        ret = self.parse_opt_ret()
        body = self.parse_block()
        return MethodDecl(name, params, ret, body)

    def parse_controller(self) -> ControllerDecl:
        self.eat("controller")
        name = self.eat_id()
        base = None
        if self.at("extends"):
            self.eat("extends")
            base = self.eat_id()
        self.eat("{")
        methods = []
        while not self.at("}"):
            methods.append(self.parse_method())
        self.eat("}")
        return ControllerDecl(name, base, methods)

    def parse_handler(self) -> HandlerDecl:
        self.eat("handler")
        name = self.eat_id()
        base = None
        if self.at("extends"):
            self.eat("extends")
            base = self.eat_id()
        self.eat("{")
        methods = []
        while not self.at("}"):
            methods.append(self.parse_method())
        self.eat("}")
        return HandlerDecl(name, base, methods)

    def parse_func(self) -> FuncDecl:
        self.eat("func")
        name = self.eat_id()
        params = self.parse_params()
        ret = self.parse_opt_ret()
        body = self.parse_block()
        return FuncDecl(name, params, ret, body)

    # ---------- statements ----------
    def parse_block(self) -> Block:
        self.eat("{")
        stmts = []
        while not self.at("}"):
            stmts.append(self.parse_stmt())
        self.eat("}")
        return Block(stmts)

    def parse_var_decl(self, eat_semi=True) -> VarDecl:
        self.eat("var")
        name = self.eat_id()
        ty = None
        if self.at(":"):
            self.eat(":")
            ty = self.parse_type()
        self.eat("=")
        init = self.parse_expr()
        if eat_semi:
            self.eat(";")
        return VarDecl(name, ty, init)

    def parse_stmt(self) -> Stmt:
        t = self.peek()
        if t.kind == "kw":
            if t.value == "var":
                return self.parse_var_decl()
            if t.value == "if":
                self.eat("if")
                self.eat("(")
                cond = self.parse_expr()
                self.eat(")")
                then = self.parse_stmt()
                els = None
                if self.at("else"):
                    self.eat("else")
                    els = self.parse_stmt()
                return If(cond, then, els)
            if t.value == "while":
                self.eat("while")
                self.eat("(")
                cond = self.parse_expr()
                self.eat(")")
                return While(cond, self.parse_stmt())
            if t.value == "for":
                self.eat("for")
                self.eat("(")
                # for (var x of array) / for (var k in map)
                save = self.i
                if self.at("var"):
                    self.i += 1
                    vname = self.eat_id()
                    nt = self.peek()
                    if nt.value in ("of", "in"):
                        kind = nt.value
                        self.i += 1
                        iterable = self.parse_expr()
                        self.eat(")")
                        return ForEach(vname, kind, iterable, self.parse_stmt())
                self.i = save
                init = None
                if not self.at(";"):
                    init = self.parse_var_decl(False) if self.at("var") else self.parse_simple_stmt(False)
                self.eat(";")
                cond = None if self.at(";") else self.parse_expr()
                self.eat(";")
                step = None
                if not self.at(")"):
                    step = self.parse_simple_stmt(False)
                self.eat(")")
                return For(init, cond, step, self.parse_stmt())
            if t.value == "return":
                self.eat("return")
                val = None if self.at(";") else self.parse_expr()
                self.eat(";")
                return Return(val)
            if t.value == "break":
                self.eat("break")
                self.eat(";")
                return Break()
            if t.value == "continue":
                self.eat("continue")
                self.eat(";")
                return Continue()
        if self.at("{"):
            return self.parse_block()
        return self.parse_simple_stmt(True)

    def parse_simple_stmt(self, eat_semi=True) -> Stmt:
        expr = self.parse_expr()
        t = self.peek()
        if t.kind == "op" and t.value in ("=", "+=", "-="):
            op = t.value
            self.i += 1
            value = self.parse_expr()
            if not isinstance(expr, (Ident, Member, Index)):
                self.err("assignment target must be a variable, field, or index")
            if op != "=":
                value = Binary(op[0], expr, value)
            if eat_semi:
                self.eat(";")
            return Assign(expr, value)
        if eat_semi:
            self.eat(";")
        return ExprStmt(expr)

    # ---------- expressions ----------
    def parse_expr(self) -> Expr:
        return self.parse_ternary()

    def parse_ternary(self) -> Expr:
        cond = self.parse_coalesce()
        if self.at("?"):
            self.eat("?")
            a = self.parse_expr()
            self.eat(":")
            b = self.parse_expr()
            return Binary("?:", cond, Binary(":", a, b))
        return cond

    def parse_coalesce(self) -> Expr:
        e = self.parse_or()
        while self.at("??"):
            self.eat("??")
            e = Binary("??", e, self.parse_or())
        return e

    def parse_or(self) -> Expr:
        e = self.parse_and()
        while self.at("||"):
            self.eat("||")
            e = Binary("||", e, self.parse_and())
        return e

    def parse_and(self) -> Expr:
        e = self.parse_equality()
        while self.at("&&"):
            self.eat("&&")
            e = Binary("&&", e, self.parse_equality())
        return e

    def parse_equality(self) -> Expr:
        e = self.parse_rel()
        while self.at("==") or self.at("!="):
            op = self.peek().value
            self.i += 1
            e = Binary(op, e, self.parse_rel())
        return e

    def parse_rel(self) -> Expr:
        e = self.parse_add()
        while self.peek().value in ("<", "<=", ">", ">="):
            op = self.peek().value
            self.i += 1
            e = Binary(op, e, self.parse_add())
        return e

    def parse_add(self) -> Expr:
        e = self.parse_mul()
        while self.peek().value in ("+", "-"):
            op = self.peek().value
            self.i += 1
            e = Binary(op, e, self.parse_mul())
        return e

    def parse_mul(self) -> Expr:
        e = self.parse_unary()
        while self.peek().value in ("*", "/", "%"):
            op = self.peek().value
            self.i += 1
            e = Binary(op, e, self.parse_unary())
        return e

    def parse_unary(self) -> Expr:
        if self.peek().value in ("!", "-", "+", "~"):
            op = self.peek().value
            self.i += 1
            return Unary(op, self.parse_unary())
        return self.parse_postfix()

    def parse_postfix(self) -> Expr:
        e = self.parse_primary()
        while True:
            if self.at("."):
                self.eat(".")
                e = Member(e, self.eat_id())
            elif self.at("("):
                args = self.parse_args()
                e = Call(e, args)
            elif self.at("["):
                self.eat("[")
                idx = self.parse_expr()
                self.eat("]")
                e = Index(e, idx)
            else:
                break
        return e

    def parse_args(self) -> list[Expr]:
        self.eat("(")
        args = []
        if not self.at(")"):
            while True:
                args.append(self.parse_expr())
                if not self.at(","):
                    break
                self.eat(",")
        self.eat(")")
        return args

    def parse_primary(self) -> Expr:
        t = self.peek()
        if t.kind == "int":
            self.i += 1
            return IntLit(int(t.value, 0))
        if t.kind == "float":
            self.i += 1
            return FloatLit(float(t.value))
        if t.kind == "str":
            self.i += 1
            return StrLit(t.value)
        if t.kind == "kw" and t.value in ("true", "false"):
            self.i += 1
            return BoolLit(t.value == "true")
        if t.kind == "kw" and t.value == "null":
            self.i += 1
            return NullLit()
        if t.value == "[":
            self.eat("[")
            items = []
            if not self.at("]"):
                while True:
                    items.append(self.parse_expr())
                    if not self.at(","):
                        break
                    self.eat(",")
            self.eat("]")
            return ArrayLit(items)
        if t.value == "{":
            self.eat("{")
            pairs = []
            if not self.at("}"):
                while True:
                    k = self.peek()
                    if k.kind == "str":
                        self.i += 1
                        key = k.value
                    elif k.kind == "id":
                        self.i += 1
                        key = k.value
                    else:
                        self.err("object literal key must be string or identifier")
                    self.eat(":")
                    pairs.append((key, self.parse_expr()))
                    if not self.at(","):
                        break
                    self.eat(",")
            self.eat("}")
            return ObjectLit(pairs)
        if t.kind == "id":
            self.i += 1
            return Ident(t.value)
        if t.value == "new":
            self.eat("new")
            ty = self.parse_type()
            args = self.parse_args() if self.at("(") else []
            init = []
            if self.at("{"):
                self.eat("{")
                if not self.at("}"):
                    while True:
                        fname = self.eat_id()
                        self.eat(":")
                        init.append((fname, self.parse_expr()))
                        if not self.at(","):
                            break
                        self.eat(",")
                self.eat("}")
            return New(ty, args, init)
        if t.value == "(":
            self.eat("(")
            e = self.parse_expr()
            self.eat(")")
            return e
        self.err(f"unexpected token in expression: {t.value!r}")
