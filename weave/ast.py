"""Weave AST.

Nodes are plain dataclasses carrying a `span` for diagnostics. Expressions and
statements are kept separate. Concurrency primitives (`spawn`, `lock`, `join`)
are first-class so the checker can reason about them structurally.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .diagnostics import Span


# ---- Expressions ----------------------------------------------------------

@dataclass
class Expr:
    span: Span


@dataclass
class IntLit(Expr):
    value: int


@dataclass
class BoolLit(Expr):
    value: bool


@dataclass
class Name(Expr):
    ident: str


@dataclass
class Unary(Expr):
    op: str
    operand: Expr


@dataclass
class Binary(Expr):
    op: str
    left: Expr
    right: Expr


@dataclass
class Call(Expr):
    """Function / builtin call: mutex(x), cell(x), share(m), alias(c),
    get(c), set(c, v), load(s), print(x), or a user fn."""
    callee: str
    args: List[Expr]


@dataclass
class SpawnExpr(Expr):
    """`spawn { ... }` — evaluates to a thread Handle."""
    body: "Block"


# ---- Statements -----------------------------------------------------------

@dataclass
class Stmt:
    span: Span


@dataclass
class Let(Stmt):
    name: str
    mutable: bool
    value: Expr


@dataclass
class Assign(Stmt):
    name: str
    value: Expr


@dataclass
class ExprStmt(Stmt):
    expr: Expr


@dataclass
class Return(Stmt):
    value: Optional[Expr]


@dataclass
class If(Stmt):
    cond: Expr
    then_block: "Block"
    else_block: Optional["Block"]


@dataclass
class While(Stmt):
    cond: Expr
    body: "Block"


@dataclass
class Lock(Stmt):
    """`lock <target> as <guard> { body }` — exclusive access to the guarded
    value for the duration of `body`, bound to `guard`."""
    target: Expr
    guard: str
    body: "Block"


@dataclass
class Join(Stmt):
    """`join(handle);` — waits for a spawned thread to finish."""
    handle: Expr


@dataclass
class Block:
    stmts: List[Stmt]
    span: Span


# ---- Top level ------------------------------------------------------------

@dataclass
class Param:
    name: str
    span: Span


@dataclass
class Fn:
    name: str
    params: List[Param]
    body: Block
    span: Span


@dataclass
class Program:
    fns: List[Fn] = field(default_factory=list)
