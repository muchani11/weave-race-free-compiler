"""`weave` command-line interface.

    weave check <file.wv>        # prove race freedom; exit 1 if it can't
    weave check <file.wv> -v     # also run the interleaving model checker
    weave explain <file.wv>      # show a concrete racy schedule, if any

The exit code is the contract: 0 means "compiled, provably race-free".
"""
from __future__ import annotations

import argparse
import sys
from typing import List

from . import compile_source
from .checker import RaceAnalyzer
from .diagnostics import Diagnostic
from .interleave import explore
from .parser import parse

GREEN = "\033[32m"
RED = "\033[31m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _color(s: str, code: str, enabled: bool) -> str:
    return f"{code}{s}{RESET}" if enabled else s


def _read(path: str) -> str:
    with open(path, "r") as f:
        return f.read()


def _print_diags(diags: List[Diagnostic], source: str, path: str, color: bool):
    for d in diags:
        print(d.render(source, path))
        print()


def cmd_check(args) -> int:
    source = _read(args.file)
    color = sys.stdout.isatty() and not args.no_color
    result = compile_source(source)

    if not result.ok:
        _print_diags(result.diagnostics, source, args.file, color)
        n = len(result.diagnostics)
        print(_color(f"✗ rejected: {n} problem(s) — this code could race.",
                     RED, color))
        return 1

    # Optional second opinion from the interleaving model checker.
    verify_note = ""
    if args.verify and result.program is not None:
        analyzer = RaceAnalyzer()
        analyzer.analyze(result.program)
        res = explore(analyzer)
        if res.witness is not None:
            # Should never happen if both engines agree; surface loudly.
            print(_color("internal disagreement: model checker found a race "
                         "the type system missed:", RED, color))
            for line in res.witness.trace:
                print("   ", line)
            return 2
        bound = " (bounded)" if res.hit_bound else ""
        verify_note = (f"\n{DIM if color else ''}  model checker explored "
                       f"{res.states_explored} interleavings{bound}: "
                       f"no racing schedule exists.{RESET if color else ''}")

    print(_color("✓ compiled: no data race is possible.", GREEN, color)
          + verify_note)
    return 0


def cmd_explain(args) -> int:
    source = _read(args.file)
    color = sys.stdout.isatty() and not args.no_color
    result = compile_source(source)

    if result.program is None:
        _print_diags(result.diagnostics, source, args.file, color)
        return 1

    analyzer = RaceAnalyzer()
    analyzer.analyze(result.program)
    res = explore(analyzer)

    if res.witness is None:
        msg = "no racing interleaving found"
        if res.hit_bound:
            msg += f" within {res.states_explored} explored states"
        else:
            msg += (f" across all {res.states_explored} reachable "
                    f"interleavings")
        print(_color(f"✓ {msg}.", GREEN, color))
        return 0

    w = res.witness
    print(_color(f"✗ data race between thread {w.racing_threads[0]} and "
                 f"thread {w.racing_threads[1]}:", RED, color))
    print(_color("  a schedule that triggers it:", BOLD, color))
    for step in w.trace:
        print("   ", step)
    print()
    print(_color("  found after exploring "
                 f"{w.states_explored} interleaving states.", DIM, color))
    return 1


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-color", action="store_true",
                        help="disable ANSI color")

    p = argparse.ArgumentParser(
        prog="weave",
        parents=[common],
        description="Compile Weave code only if it is provably data-race free.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", parents=[common],
                       help="type/ownership check + race proof")
    c.add_argument("file")
    c.add_argument("-v", "--verify", action="store_true",
                   help="cross-check with the interleaving model checker")
    c.set_defaults(func=cmd_check)

    e = sub.add_parser("explain", parents=[common],
                       help="show a concrete racy schedule, if any")
    e.add_argument("file")
    e.set_defaults(func=cmd_explain)

    return p


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        print(f"weave: cannot open {e.filename!r}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
