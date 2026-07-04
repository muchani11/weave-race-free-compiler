"""Recursive-descent parser with a Pratt expression sub-parser."""
from __future__ import annotations

from typing import List, Optional

from . import ast
from .diagnostics import Diagnostic, DiagnosticError, Span
from .lexer import Token, lex

# Binary operator precedence (higher binds tighter).
PRECEDENCE = {
    "||": 1, "&&": 2,
    "==": 3, "!=": 3, "<": 3, "<=": 3, ">": 3, ">=": 3,
    "+": 4, "-": 4,
    "*": 5, "/": 5,
}


class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    # -- token helpers ------------------------------------------------------
    @property
    def cur(self) -> Token:
        return self.tokens[self.pos]

    def at(self, kind: str, value: Optional[str] = None) -> bool:
        t = self.cur
        return t.kind == kind and (value is None or t.value == value)

    def advance(self) -> Token:
        t = self.tokens[self.pos]
        if t.kind != "eof":
            self.pos += 1
        return t

    def expect(self, kind: str, value: Optional[str] = None) -> Token:
        if not self.at(kind, value):
            want = value if value is not None else kind
            raise DiagnosticError(Diagnostic(
                "parse", f"expected {want!r} but found {self.cur.value!r}",
                self.cur.span,
            ))
        return self.advance()

    def eat_op(self, value: str) -> bool:
        if self.at("op", value):
            self.advance()
            return True
        return False

    # -- top level ----------------------------------------------------------
    def parse_program(self) -> ast.Program:
        prog = ast.Program()
        while not self.at("eof"):
            prog.fns.append(self.parse_fn())
        return prog

    def parse_fn(self) -> ast.Fn:
        start = self.expect("kw", "fn").span
        name = self.expect("ident").value
        self.expect("op", "(")
        params: List[ast.Param] = []
        if not self.at("op", ")"):
            while True:
                p = self.expect("ident")
                params.append(ast.Param(p.value, p.span))
                if not self.eat_op(","):
                    break
        self.expect("op", ")")
        # Optional `-> type` is accepted and ignored (types are inferred).
        if self.eat_op("->"):
            self.expect("ident")
        body = self.parse_block()
        return ast.Fn(name, params, body, start)

    def parse_block(self) -> ast.Block:
        start = self.expect("op", "{").span
        stmts: List[ast.Stmt] = []
        while not self.at("op", "}") and not self.at("eof"):
            stmts.append(self.parse_stmt())
        self.expect("op", "}")
        return ast.Block(stmts, start)

    # -- statements ---------------------------------------------------------
    def parse_stmt(self) -> ast.Stmt:
        if self.at("kw", "let"):
            return self.parse_let()
        if self.at("kw", "return"):
            span = self.advance().span
            value = None
            if not self.at("op", ";"):
                value = self.parse_expr()
            self.expect("op", ";")
            return ast.Return(span, value)
        if self.at("kw", "if"):
            return self.parse_if()
        if self.at("kw", "while"):
            span = self.advance().span
            cond = self.parse_expr()
            body = self.parse_block()
            return ast.While(span, cond, body)
        if self.at("kw", "lock"):
            return self.parse_lock()
        if self.at("kw", "join"):
            span = self.advance().span
            self.expect("op", "(")
            handle = self.parse_expr()
            self.expect("op", ")")
            self.expect("op", ";")
            return ast.Join(span, handle)

        # Assignment `name = expr;` vs bare expression statement.
        if self.at("ident") and self.tokens[self.pos + 1].value == "=" \
                and self.tokens[self.pos + 1].kind == "op":
            name_tok = self.advance()
            self.expect("op", "=")
            value = self.parse_expr()
            self.expect("op", ";")
            return ast.Assign(name_tok.span, name_tok.value, value)

        expr = self.parse_expr()
        self.expect("op", ";")
        return ast.ExprStmt(expr.span, expr)

    def parse_let(self) -> ast.Let:
        span = self.expect("kw", "let").span
        mutable = self.at("kw", "mut")
        if mutable:
            self.advance()
        name = self.expect("ident").value
        # Optional `: type` annotation is accepted and ignored.
        if self.eat_op(":"):
            self.expect("ident")
        self.expect("op", "=")
        value = self.parse_expr()
        self.expect("op", ";")
        return ast.Let(span, name, mutable, value)

    def parse_if(self) -> ast.If:
        span = self.expect("kw", "if").span
        cond = self.parse_expr()
        then_block = self.parse_block()
        else_block = None
        if self.at("kw", "else"):
            self.advance()
            else_block = self.parse_block()
        return ast.If(span, cond, then_block, else_block)

    def parse_lock(self) -> ast.Lock:
        span = self.expect("kw", "lock").span
        target = self.parse_expr()
        self.expect("kw", "as")
        guard = self.expect("ident").value
        body = self.parse_block()
        return ast.Lock(span, target, guard, body)

    # -- expressions (Pratt) ------------------------------------------------
    def parse_expr(self, min_prec: int = 0) -> ast.Expr:
        left = self.parse_unary()
        while self.cur.kind == "op" and self.cur.value in PRECEDENCE:
            op = self.cur.value
            prec = PRECEDENCE[op]
            if prec < min_prec:
                break
            op_span = self.advance().span
            right = self.parse_expr(prec + 1)
            left = ast.Binary(op_span, op, left, right)
        return left

    def parse_unary(self) -> ast.Expr:
        if self.at("op", "!") or self.at("op", "-"):
            tok = self.advance()
            operand = self.parse_unary()
            return ast.Unary(tok.span, tok.value, operand)
        return self.parse_primary()

    def parse_primary(self) -> ast.Expr:
        t = self.cur
        if t.kind == "int":
            self.advance()
            return ast.IntLit(t.span, int(t.value))
        if self.at("kw", "true") or self.at("kw", "false"):
            self.advance()
            return ast.BoolLit(t.span, t.value == "true")
        if self.at("kw", "spawn"):
            self.advance()
            body = self.parse_block()
            return ast.SpawnExpr(t.span, body)
        if t.kind == "ident":
            self.advance()
            if self.at("op", "("):
                self.advance()
                args: List[ast.Expr] = []
                if not self.at("op", ")"):
                    while True:
                        args.append(self.parse_expr())
                        if not self.eat_op(","):
                            break
                self.expect("op", ")")
                return ast.Call(t.span, t.value, args)
            return ast.Name(t.span, t.value)
        if self.eat_op("("):
            inner = self.parse_expr()
            self.expect("op", ")")
            return inner
        raise DiagnosticError(Diagnostic(
            "parse", f"unexpected token {t.value!r}", t.span,
        ))


def parse(source: str) -> ast.Program:
    return Parser(lex(source)).parse_program()
