"""Bounded interleaving model checker.

`RaceAnalyzer` proves race freedom from types and ownership, without
enumerating schedules. This is the complementary witness engine: it lowers each
thread to a sequence of atomic memory ops (with explicit lock acquire/release),
then runs a bounded DFS over all interleavings to either

  * produce a concrete schedule that races (for rejected programs), or
  * confirm no reachable schedule races, up to the exploration bound.

A lock ACQUIRE is only enabled when the lock is free, so two threads can never
both be inside a critical section. That's why the mutex pattern is safe, shown
operationally instead of asserted.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .checker import Access, RaceAnalyzer
from .diagnostics import Span

# An op is (kind, loc, span). kind in {ACQ, REL, R, W}. span may be None.
Op = Tuple[str, int, Optional[Span]]


@dataclass
class Witness:
    trace: List[str]         # schedule leading to the race
    racing_threads: Tuple[int, int]
    loc: int
    states_explored: int


@dataclass
class ExploreResult:
    witness: Optional[Witness]
    states_explored: int
    hit_bound: bool

    @property
    def race_free(self) -> bool:
        return self.witness is None


def lower(analyzer: RaceAnalyzer) -> Dict[int, List[Op]]:
    """Turn recorded accesses into per-thread op sequences.

    A synchronized access becomes a one-op critical section (ACQ, op, REL);
    an unsynchronized access is a bare memory op.
    """
    threads: Dict[int, List[Op]] = defaultdict(list)
    for a in analyzer.accesses:  # already in per-thread order
        kind = "W" if a.write else "R"
        if a.synchronized:
            threads[a.thread].append(("ACQ", a.loc, a.span))
            threads[a.thread].append((kind, a.loc, a.span))
            threads[a.thread].append(("REL", a.loc, a.span))
        else:
            threads[a.thread].append((kind, a.loc, a.span))
    return threads


def _loc_label(analyzer: RaceAnalyzer, loc: int) -> str:
    origin = analyzer.loc_origin.get(loc)
    return f"value#{loc}" + (f"@L{origin.line}" if origin else "")


def explore(analyzer: RaceAnalyzer, max_states: int = 200_000) -> ExploreResult:
    threads = lower(analyzer)
    tids = sorted(threads)
    if not tids:
        return ExploreResult(None, 0, False)

    concurrent = analyzer.concurrent

    def op_str(tid: int, op: Op) -> str:
        kind, loc, span = op
        verb = {"ACQ": "lock", "REL": "unlock", "R": "read", "W": "write"}[kind]
        at = f" (line {span.line})" if span else ""
        return f"T{tid}: {verb} {_loc_label(analyzer, loc)}{at}"

    start_pcs = tuple(0 for _ in tids)
    seen = set()
    explored = 0
    hit_bound = False

    # DFS stack: (pcs, locks(frozenset of (loc,owner)), trace)
    stack: List[Tuple[Tuple[int, ...], frozenset, List[str]]] = [
        (start_pcs, frozenset(), [])
    ]

    while stack:
        pcs, locks_fs, trace = stack.pop()
        state_key = (pcs, locks_fs)
        if state_key in seen:
            continue
        seen.add(state_key)
        explored += 1
        if explored > max_states:
            hit_bound = True
            break

        locks = dict(locks_fs)

        # Race: two concurrent threads both ready to touch the same location,
        # at least one writing, with no lock between them.
        ready_mem: List[Tuple[int, Op]] = []
        for idx, tid in enumerate(tids):
            pc = pcs[idx]
            ops = threads[tid]
            if pc < len(ops) and ops[pc][0] in ("R", "W"):
                ready_mem.append((tid, ops[pc]))
        for i in range(len(ready_mem)):
            for j in range(i + 1, len(ready_mem)):
                (ti, oi), (tj, oj) = ready_mem[i], ready_mem[j]
                if oi[1] != oj[1]:
                    continue
                if not (oi[0] == "W" or oj[0] == "W"):
                    continue
                if frozenset((ti, tj)) not in concurrent:
                    continue
                witness_trace = trace + [
                    "-- race point reached; both operations are enabled with "
                    "no lock held: --",
                    "    " + op_str(ti, oi),
                    "    " + op_str(tj, oj),
                ]
                return ExploreResult(
                    Witness(witness_trace, (ti, tj), oi[1], explored),
                    explored, hit_bound,
                )

        # Expand successors: one enabled op from any thread.
        for idx, tid in enumerate(tids):
            pc = pcs[idx]
            ops = threads[tid]
            if pc >= len(ops):
                continue
            kind, loc, span = ops[pc]
            new_locks = dict(locks)
            if kind == "ACQ":
                if locks.get(loc, None) not in (None, tid):
                    continue  # held by another thread
                new_locks[loc] = tid
            elif kind == "REL":
                if locks.get(loc) != tid:
                    continue
                new_locks.pop(loc, None)
            new_pcs = list(pcs)
            new_pcs[idx] = pc + 1
            stack.append((
                tuple(new_pcs),
                frozenset(new_locks.items()),
                trace + [op_str(tid, ops[pc])],
            ))

    return ExploreResult(None, explored, hit_bound)
