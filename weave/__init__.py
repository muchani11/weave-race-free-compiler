"""Weave: a compiler front end that proves data-race freedom.

Public API:
    parse(source)            -> ast.Program
    check_program(program)   -> CheckResult
    compile_source(source)   -> CompileResult   (parse + check in one call)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from . import ast
from .checker import CheckResult, check_program
from .diagnostics import Diagnostic, DiagnosticError
from .parser import parse

__all__ = [
    "ast", "parse", "check_program", "CheckResult",
    "Diagnostic", "compile_source", "CompileResult",
]


@dataclass
class CompileResult:
    ok: bool
    diagnostics: List[Diagnostic] = field(default_factory=list)
    program: "ast.Program | None" = None


def compile_source(source: str) -> CompileResult:
    """Parse and check a source string. User errors (including lex/parse
    failures) come back as diagnostics; this never raises for them."""
    try:
        program = parse(source)
    except DiagnosticError as e:
        return CompileResult(False, [e.diag], None)
    result = check_program(program)
    return CompileResult(result.ok, result.diagnostics, program)
