"""Hand-written lexer for Weave.

Produces a flat token stream with source spans. Kept deliberately small: the
interesting engineering in this project is the checker, not the front end.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .diagnostics import Diagnostic, DiagnosticError, Span

KEYWORDS = {
    "fn", "let", "mut", "spawn", "lock", "as", "join",
    "if", "else", "while", "return", "true", "false",
}

# Two-char operators must be tried before their one-char prefixes.
TWO_CHAR = {"==", "!=", "<=", ">=", "&&", "||", "->"}
ONE_CHAR = set("(){};,:=<>+-*/!&")


@dataclass
class Token:
    kind: str      # "kw", "ident", "int", "op", "eof"
    value: str
    span: Span

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Token({self.kind}, {self.value!r}, L{self.span.line}:{self.span.col})"


def lex(source: str) -> List[Token]:
    tokens: List[Token] = []
    line = 1
    col = 1
    i = 0
    n = len(source)

    def span(length: int) -> Span:
        return Span(line, col, length)

    while i < n:
        c = source[i]

        # Whitespace
        if c == "\n":
            line += 1
            col = 1
            i += 1
            continue
        if c in " \t\r":
            i += 1
            col += 1
            continue

        # Line comments
        if c == "/" and i + 1 < n and source[i + 1] == "/":
            while i < n and source[i] != "\n":
                i += 1
            continue

        # Identifiers / keywords
        if c.isalpha() or c == "_":
            start = i
            start_col = col
            while i < n and (source[i].isalnum() or source[i] == "_"):
                i += 1
                col += 1
            text = source[start:i]
            kind = "kw" if text in KEYWORDS else "ident"
            tokens.append(Token(kind, text, Span(line, start_col, len(text))))
            continue

        # Integer literals
        if c.isdigit():
            start = i
            start_col = col
            while i < n and source[i].isdigit():
                i += 1
                col += 1
            text = source[start:i]
            tokens.append(Token("int", text, Span(line, start_col, len(text))))
            continue

        # Two-char operators
        pair = source[i:i + 2]
        if pair in TWO_CHAR:
            tokens.append(Token("op", pair, span(2)))
            i += 2
            col += 2
            continue

        # One-char operators
        if c in ONE_CHAR:
            tokens.append(Token("op", c, span(1)))
            i += 1
            col += 1
            continue

        raise DiagnosticError(
            Diagnostic("lex", f"unexpected character {c!r}", span(1))
        )

    tokens.append(Token("eof", "", Span(line, col, 0)))
    return tokens
