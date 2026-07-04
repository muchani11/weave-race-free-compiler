"""Source positions and human-friendly diagnostics.

Every error the compiler emits is a `Diagnostic`: a category, a primary
message, the source span it points at, and — crucially for this project — an
optional `help` that guides the programmer toward a race-free pattern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class Span:
    """A half-open [start, end) region of a single source line (1-based)."""
    line: int
    col: int
    length: int = 1

    @staticmethod
    def unknown() -> "Span":
        return Span(0, 0, 0)


class DiagnosticError(Exception):
    """Raised to abort a compilation phase with one fatal diagnostic."""

    def __init__(self, diag: "Diagnostic"):
        super().__init__(diag.message)
        self.diag = diag


@dataclass
class Diagnostic:
    kind: str            # e.g. "data-race", "use-after-move", "parse"
    message: str
    span: Span
    help: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    def render(self, source: str, filename: str = "<input>") -> str:
        lines = source.splitlines()
        out: List[str] = []
        out.append(f"error[{self.kind}]: {self.message}")
        if self.span.line and self.span.line <= len(lines):
            src_line = lines[self.span.line - 1]
            gutter = f"{self.span.line:>4} | "
            out.append(f"    --> {filename}:{self.span.line}:{self.span.col}")
            out.append(f"{gutter}{src_line}")
            caret_pad = " " * (len(gutter) + max(self.span.col - 1, 0))
            out.append(caret_pad + "^" * max(self.span.length, 1))
        for note in self.notes:
            out.append(f"    note: {note}")
        if self.help:
            for i, line in enumerate(self.help.splitlines()):
                prefix = "    help: " if i == 0 else "          "
                out.append(prefix + line)
        return "\n".join(out)
