"""Well AST nodes (MVP)."""
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------- types ----------
@dataclass
class TypeRef:
    name: str
    args: list["TypeRef"] = field(default_factory=list)
    nullable: bool = False


# ---------- expressions ----------
class Expr:
    pass


@dataclass
class IntLit(Expr):
    value: int


@dataclass
class FloatLit(Expr):
    value: float


@dataclass
class StrLit(Expr):
    value: str


@dataclass
class BoolLit(Expr):
    value: bool


@dataclass
class NullLit(Expr):
    pass


@dataclass
class Ident(Expr):
    name: str


@dataclass
class ArrayLit(Expr):
    items: list[Expr]


@dataclass
class ObjectLit(Expr):
    pairs: list[tuple[str, Expr]]


@dataclass
class Index(Expr):
    obj: Expr
    index: Expr


@dataclass
class Member(Expr):
    obj: Expr
    name: str


@dataclass
class Call(Expr):
    func: Expr
    args: list[Expr]


@dataclass
class New(Expr):
    type: TypeRef
    args: list[Expr] = field(default_factory=list)
    init: list[tuple[str, Expr]] = field(default_factory=list)  # field: value


@dataclass
class Unary(Expr):
    op: str
    expr: Expr


@dataclass
class Binary(Expr):
    op: str
    left: Expr
    right: Expr


# ---------- statements ----------
class Stmt:
    pass


@dataclass
class VarDecl(Stmt):
    name: str
    type: Optional[TypeRef]
    init: Expr


@dataclass
class Assign(Stmt):
    target: Expr   # Ident or Member
    value: Expr


@dataclass
class ExprStmt(Stmt):
    expr: Expr


@dataclass
class If(Stmt):
    cond: Expr
    then: Stmt
    els: Optional[Stmt]


@dataclass
class While(Stmt):
    cond: Expr
    body: Stmt


@dataclass
class For(Stmt):
    init: Optional[Stmt]
    cond: Optional[Expr]
    step: Optional[Stmt]
    body: Stmt


@dataclass
class ForEach(Stmt):
    var: str
    kind: str          # "of" (array) / "in" (map keys)
    iter: Expr
    body: Stmt


@dataclass
class Return(Stmt):
    value: Optional[Expr]


@dataclass
class Break(Stmt):
    pass


@dataclass
class Continue(Stmt):
    pass


@dataclass
class Block(Stmt):
    stmts: list[Stmt]


# ---------- declarations ----------
@dataclass
class FieldDecl:
    name: str
    type: TypeRef
    default: Optional[Expr] = None


@dataclass
class ConstDecl:
    name: str
    type: TypeRef
    value: Expr


@dataclass
class EnumDecl:
    name: str
    members: list[tuple[str, Optional[Any]]]  # (name, int|str|None)


@dataclass
class StructDecl:
    name: str
    base: Optional[str]
    fields: list[FieldDecl]
    is_event: bool = False


@dataclass
class MethodDecl:
    name: str
    params: list[tuple[str, TypeRef]]
    ret: Optional[TypeRef]
    body: Block


@dataclass
class ControllerDecl:
    name: str
    base: Optional[str]
    methods: list[MethodDecl]


@dataclass
class HandlerDecl:
    name: str
    base: Optional[str]
    methods: list[MethodDecl]


@dataclass
class FuncDecl:
    name: str
    params: list[tuple[str, TypeRef]]
    ret: Optional[TypeRef]
    body: Block


@dataclass
class ImportDecl:
    path: str


@dataclass
class Program:
    module: Optional[str] = None
    imports: list[ImportDecl] = field(default_factory=list)
    entry: Optional[str] = None
    decls: list[Any] = field(default_factory=list)
    uses_http: bool = False
    uses_ws: bool = False
    uses_mysql: bool = False

    # lookup helpers filled by checker
    structs: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)
    enums: dict = field(default_factory=dict)
    consts: dict = field(default_factory=dict)
    funcs: dict = field(default_factory=dict)
    controllers: dict = field(default_factory=dict)
    handlers: dict = field(default_factory=dict)
