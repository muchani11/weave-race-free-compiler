"""The Weave checker: proves data-race freedom (or explains why it can't).

Two cooperating analyses run over each function:

  A. Type + ownership/borrow checking (`OwnershipChecker`)
     - infers types, validates built-ins
     - tracks move semantics for non-`Copy` values and reports use-after-move
     - enforces mutability (`mut`) for writes

  B. Thread-escape race analysis (`RaceAnalyzer`)
     - assigns every heap object a location id
     - builds the spawn/join thread tree and a "can-run-concurrently" relation
     - flags any location touched by two concurrent threads with >=1 write and
       no common lock held  ->  a data race

The type system is designed so that the ONLY way to share mutable state across
threads without a race is `mutex` + `share` + `lock`. `alias` deliberately
exposes an *unsynchronized* shared handle (like `Rc`): legal single-threaded,
rejected the moment two concurrent threads touch it. That mirrors how real
ownership systems make races unrepresentable rather than merely unlikely.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from . import ast
from .diagnostics import Diagnostic, Span

# ---- Types ----------------------------------------------------------------

INT = "int"
BOOL = "bool"
UNIT = "unit"
CELL = "cell"            # Cell<int>: owned, mutable, non-Copy
SHARED_CELL = "sharedcell"   # Arc<Cell>: shared, UNSYNCHRONIZED (footgun)
MUTEX = "mutex"          # Mutex<int>: owned, non-Copy
SHARED = "shared"        # Arc<Mutex<int>>: shared, synchronized
HANDLE = "handle"        # thread handle from spawn
UNKNOWN = "unknown"

COPY_TYPES = {INT, BOOL, UNIT, UNKNOWN}
# Handles that are cloned (not moved) when captured by a thread:
CLONEABLE = {SHARED_CELL, SHARED}


def is_copy(ty: str) -> bool:
    return ty in COPY_TYPES


# ===========================================================================
# Analysis A: types + ownership / borrow / move
# ===========================================================================

@dataclass
class VarInfo:
    ty: str
    mutable: bool
    moved_at: Optional[Span] = None  # None => still live


class OwnershipChecker:
    def __init__(self, program: ast.Program):
        self.program = program
        self.fns: Dict[str, ast.Fn] = {f.name: f for f in program.fns}
        self.diags: List[Diagnostic] = []

    def check(self) -> List[Diagnostic]:
        for fn in self.program.fns:
            env: Dict[str, VarInfo] = {
                p.name: VarInfo(UNKNOWN, True) for p in fn.params
            }
            self.check_block(fn.body, env)
        return self.diags

    def err(self, kind: str, msg: str, span: Span,
            help: Optional[str] = None, notes: Optional[List[str]] = None):
        self.diags.append(Diagnostic(kind, msg, span, help, notes or []))

    # -- statements ---------------------------------------------------------
    def check_block(self, block: ast.Block, env: Dict[str, VarInfo]):
        for stmt in block.stmts:
            self.check_stmt(stmt, env)

    def check_stmt(self, stmt: ast.Stmt, env: Dict[str, VarInfo]):
        if isinstance(stmt, ast.Let):
            ty = self.eval_expr(stmt.value, env)
            env[stmt.name] = VarInfo(ty, stmt.mutable)
        elif isinstance(stmt, ast.Assign):
            info = env.get(stmt.name)
            if info is None:
                self.err("name", f"assignment to undefined variable {stmt.name!r}",
                         stmt.span)
                self.eval_expr(stmt.value, env)
                return
            if not info.mutable:
                self.err("mutability",
                         f"cannot assign to immutable variable {stmt.name!r}",
                         stmt.span,
                         help=f"declare it with `let mut {stmt.name} = ...`")
            self.eval_expr(stmt.value, env)
            info.moved_at = None  # reassignment revives the binding
        elif isinstance(stmt, ast.ExprStmt):
            self.eval_expr(stmt.expr, env)
        elif isinstance(stmt, ast.Return):
            if stmt.value is not None:
                self.eval_expr(stmt.value, env)
        elif isinstance(stmt, ast.If):
            self.eval_expr(stmt.cond, env)
            self.check_block(stmt.then_block, dict(env))
            if stmt.else_block:
                self.check_block(stmt.else_block, dict(env))
        elif isinstance(stmt, ast.While):
            self.eval_expr(stmt.cond, env)
            self.check_block(stmt.body, dict(env))
        elif isinstance(stmt, ast.Lock):
            tgt = self.eval_expr(stmt.target, env)
            if tgt not in (SHARED, MUTEX, UNKNOWN):
                self.err("type",
                         f"`lock` requires a mutex or shared mutex, found {tgt}",
                         stmt.span,
                         help="create one with `mutex(0)` then `share(m)`")
            child = dict(env)
            # The guard is an exclusive, mutable view of the protected int.
            child[stmt.guard] = VarInfo(INT, True)
            self.check_block(stmt.body, child)
        elif isinstance(stmt, ast.Join):
            ty = self.eval_expr(stmt.handle, env)
            if ty not in (HANDLE, UNKNOWN):
                self.err("type", f"`join` expects a thread handle, found {ty}",
                         stmt.span)

    # -- expressions --------------------------------------------------------
    def eval_expr(self, e: ast.Expr, env: Dict[str, VarInfo]) -> str:
        if isinstance(e, ast.IntLit):
            return INT
        if isinstance(e, ast.BoolLit):
            return BOOL
        if isinstance(e, ast.Name):
            return self.read_name(e, env, move=True)
        if isinstance(e, ast.Unary):
            self.eval_expr(e.operand, env)
            return BOOL if e.op == "!" else INT
        if isinstance(e, ast.Binary):
            self.eval_expr(e.left, env)
            self.eval_expr(e.right, env)
            return BOOL if e.op in ("==", "!=", "<", "<=", ">", ">=",
                                    "&&", "||") else INT
        if isinstance(e, ast.Call):
            return self.eval_call(e, env)
        if isinstance(e, ast.SpawnExpr):
            self.eval_spawn(e, env)
            return HANDLE
        return UNKNOWN

    def read_name(self, e: ast.Name, env: Dict[str, VarInfo],
                  move: bool) -> str:
        info = env.get(e.ident)
        if info is None:
            self.err("name", f"use of undefined variable {e.ident!r}", e.span)
            return UNKNOWN
        if info.moved_at is not None:
            self.err("use-after-move",
                     f"use of {e.ident!r} after its ownership was moved",
                     e.span,
                     help="a moved value has a single owner; keep it in one "
                          "thread, or share it with `mutex`+`share`.",
                     notes=[f"{e.ident!r} was moved at line {info.moved_at.line}"])
            return info.ty
        # Reading a non-Copy value by *value* moves it.
        if move and not is_copy(info.ty):
            info.moved_at = e.span
        return info.ty

    def eval_call(self, e: ast.Call, env: Dict[str, VarInfo]) -> str:
        name = e.callee
        # Built-ins with custom ownership behaviour.
        if name == "cell":
            self.expect_args(e, 1); self.expect_int(e.args, 0, env)
            return CELL
        if name == "mutex":
            self.expect_args(e, 1); self.expect_int(e.args, 0, env)
            return MUTEX
        if name == "alias":
            self.expect_args(e, 1)
            ty = self.arg_owner_type(e.args[0], env, {CELL, SHARED_CELL})
            if ty is False:
                self.err("type", "`alias` expects a cell", e.span)
            return SHARED_CELL
        if name == "share":
            self.expect_args(e, 1)
            ty = self.arg_owner_type(e.args[0], env, {MUTEX, SHARED})
            if ty is False:
                self.err("type", "`share` expects a mutex", e.span)
            return SHARED
        if name in ("get", "set"):
            self.expect_args(e, 2 if name == "set" else 1)
            self.expect_container(e, e.args[0], env, {CELL, SHARED_CELL},
                                  write=(name == "set"))
            if name == "set":
                self.expect_int(e.args, 1, env)
            return INT if name == "get" else UNIT
        if name == "load":
            self.expect_args(e, 1)
            self.expect_container(e, e.args[0], env, {SHARED, MUTEX},
                                  write=False)
            return INT
        if name == "print":
            for a in e.args:
                self.eval_expr(a, env)
            return UNIT
        # User-defined function: arguments are moved/copied as usual.
        if name in self.fns:
            for a in e.args:
                self.eval_expr(a, env)
            return UNKNOWN
        self.err("name", f"call to unknown function {name!r}", e.span)
        for a in e.args:
            self.eval_expr(a, env)
        return UNKNOWN

    def eval_spawn(self, e: ast.SpawnExpr, env: Dict[str, VarInfo]):
        # Capture analysis: non-Copy, non-cloneable values (cells / mutexes)
        # are MOVED into the thread; shared handles are cloned; scalars copied.
        captured = collect_names(e.body)
        child = dict(env)
        for name in captured:
            info = env.get(name)
            if info is None or info.moved_at is not None:
                continue
            if info.ty in CLONEABLE or is_copy(info.ty):
                child[name] = VarInfo(info.ty, info.mutable)  # clone / copy
            else:
                # Move into the thread: the parent loses ownership here.
                child[name] = VarInfo(info.ty, info.mutable)
                info.moved_at = e.span
        self.check_block(e.body, child)

    # -- built-in argument helpers -----------------------------------------
    def expect_args(self, e: ast.Call, n: int):
        if len(e.args) != n:
            self.err("arity",
                     f"`{e.callee}` expects {n} argument(s), got {len(e.args)}",
                     e.span)

    def expect_int(self, args: List[ast.Expr], i: int, env):
        if i < len(args):
            ty = self.eval_expr(args[i], env)
            if ty not in (INT, UNKNOWN):
                self.err("type", f"expected int, found {ty}", args[i].span)

    def arg_owner_type(self, arg: ast.Expr, env, allowed: Set[str]):
        """For consuming builtins (`alias`/`share`): the arg is moved."""
        ty = self.eval_expr(arg, env)  # this performs the move
        if ty == UNKNOWN:
            return UNKNOWN
        return ty if ty in allowed else False

    def expect_container(self, call: ast.Call, arg: ast.Expr, env,
                         allowed: Set[str], write: bool):
        """`get`/`set`/`load` BORROW their container (no move)."""
        if isinstance(arg, ast.Name):
            ty = self.read_name(arg, env, move=False)
            info = env.get(arg.ident)
            if write and info is not None and not info.mutable \
                    and ty in (CELL, SHARED_CELL):
                self.err("mutability",
                         f"cannot mutate {arg.ident!r} through `set`: it is "
                         f"not declared `mut`",
                         call.span,
                         help=f"declare it with `let mut {arg.ident} = ...`")
        else:
            ty = self.eval_expr(arg, env)
        if ty not in allowed and ty != UNKNOWN:
            verb = {"set": "set", "get": "get", "load": "load"}.get(
                call.callee, call.callee)
            self.err("type",
                     f"`{verb}` cannot be applied to a value of type {ty}",
                     arg.span)


def collect_names(node) -> Set[str]:
    """Free-ish variable names referenced anywhere under `node`.

    Conservative (ignores shadowing) — good enough for capture analysis in the
    small Weave language, where inner `let`s rarely shadow captured handles.
    """
    names: Set[str] = set()

    def walk(n):
        if isinstance(n, ast.Name):
            names.add(n.ident)
        elif isinstance(n, ast.Assign):
            names.add(n.name)
            walk(n.value)
        elif isinstance(n, ast.Block):
            for s in n.stmts:
                walk(s)
        else:
            for f in getattr(n, "__dataclass_fields__", {}):
                v = getattr(n, f)
                if isinstance(v, (ast.Expr, ast.Stmt, ast.Block)):
                    walk(v)
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, (ast.Expr, ast.Stmt, ast.Block)):
                            walk(item)

    walk(node)
    return names


# ===========================================================================
# Analysis B: thread-escape race analysis
# ===========================================================================

@dataclass
class LocRef:
    loc: int
    kind: str  # cell | sharedcell | mutex | shared | guard | handle


@dataclass
class Access:
    loc: int
    write: bool
    synchronized: bool
    thread: int
    span: Span
    kind: str  # human label: "read"/"write"


@dataclass
class Group:
    """A spawned thread and every thread beneath it (its subtree)."""
    root: int
    handle: Optional[str]
    members: Set[int]


class RaceAnalyzer:
    def __init__(self):
        self.diags: List[Diagnostic] = []
        self.accesses: List[Access] = []
        self.concurrent: Set[frozenset] = set()
        self.loc_origin: Dict[int, Span] = {}
        self._next_loc = 0
        self._next_tid = 0

    def analyze(self, program: ast.Program) -> List[Diagnostic]:
        for fn in program.fns:
            root = self.new_tid()
            env: Dict[str, LocRef] = {}
            self.analyze_block(fn.body, root, env)
        self.detect_races()
        return self.diags

    def new_loc(self, span: Span) -> int:
        loc = self._next_loc
        self._next_loc += 1
        self.loc_origin[loc] = span
        return loc

    def new_tid(self) -> int:
        t = self._next_tid
        self._next_tid += 1
        return t

    def mark_concurrent(self, a_members: Set[int], b_members: Set[int]):
        for a in a_members:
            for b in b_members:
                if a != b:
                    self.concurrent.add(frozenset((a, b)))

    # -- traversal ----------------------------------------------------------
    def analyze_block(self, block: ast.Block, tid: int,
                      env: Dict[str, LocRef]) -> Set[int]:
        """Analyze a block executed by `tid`. Returns the set of thread ids
        created anywhere within it (its descendants)."""
        created: Set[int] = set()
        active: List[Group] = []  # child groups spawned here, not yet joined

        def record(loc, write, sync, span, kind):
            self.accesses.append(Access(loc, write, sync, tid, span, kind))
            for g in active:
                self.mark_concurrent({tid}, g.members)

        for stmt in block.stmts:
            self.analyze_stmt(stmt, tid, env, active, created, record)
        return created

    def analyze_stmt(self, stmt, tid, env, active, created, record):
        if isinstance(stmt, ast.Let):
            ref = self.eval_ref(stmt.value, tid, env, active, created, record)
            if isinstance(stmt.value, ast.SpawnExpr):
                # `let h = spawn { .. }` — the spawn already registered a group;
                # tag the most recent group with this handle name.
                if active and active[-1].handle is None:
                    active[-1].handle = stmt.name
            if ref is not None:
                env[stmt.name] = ref
            elif stmt.name in env:
                del env[stmt.name]
        elif isinstance(stmt, ast.Assign):
            # Assigning to a lock guard is a synchronized write.
            info = env.get(stmt.name)
            if info is not None and info.kind == "guard":
                record(info.loc, True, True, stmt.span, "write")
            self.walk_expr(stmt.value, tid, env, record)
        elif isinstance(stmt, ast.ExprStmt):
            self.eval_ref(stmt.expr, tid, env, active, created, record)
        elif isinstance(stmt, ast.Return):
            if stmt.value is not None:
                self.walk_expr(stmt.value, tid, env, record)
        elif isinstance(stmt, ast.If):
            self.walk_expr(stmt.cond, tid, env, record)
            created |= self.analyze_block(stmt.then_block, tid, dict(env))
            if stmt.else_block:
                created |= self.analyze_block(stmt.else_block, tid, dict(env))
        elif isinstance(stmt, ast.While):
            self.walk_expr(stmt.cond, tid, env, record)
            created |= self.analyze_block(stmt.body, tid, dict(env))
        elif isinstance(stmt, ast.Lock):
            ref = self.name_ref(stmt.target, env)
            child = dict(env)
            if ref is not None:
                # Entering the lock is itself a synchronized touch.
                record(ref.loc, False, True, stmt.span, "read")
                child[stmt.guard] = LocRef(ref.loc, "guard")
            created |= self.analyze_block(stmt.body, tid, child)
        elif isinstance(stmt, ast.Join):
            if isinstance(stmt.handle, ast.Name):
                hname = stmt.handle.ident
                for i, g in enumerate(active):
                    if g.handle == hname:
                        active.pop(i)  # joined => no longer concurrent
                        break

    def eval_ref(self, e, tid, env, active, created,
                 record) -> Optional[LocRef]:
        """Evaluate an expression for its side effects (accesses / spawns) and
        return a LocRef if it denotes a heap location."""
        if isinstance(e, ast.SpawnExpr):
            child = self.new_tid()
            child_env = dict(env)  # capture
            descendants = self.analyze_block(e.body, child, child_env)
            members = {child} | descendants
            for g in active:  # concurrent with siblings still running
                self.mark_concurrent(members, g.members)
            active.append(Group(child, None, set(members)))
            created |= members
            return LocRef(-1, "handle")
        if isinstance(e, ast.Call):
            return self.eval_call_ref(e, tid, env, record)
        if isinstance(e, ast.Name):
            return env.get(e.ident)
        # Any other expression: still walk it for nested accesses.
        self.walk_expr(e, tid, env, record)
        return None

    def eval_call_ref(self, e: ast.Call, tid, env, record) -> Optional[LocRef]:
        name = e.callee
        if name == "cell":
            self.walk_expr_args(e, tid, env, record)
            return LocRef(self.new_loc(e.span), "cell")
        if name == "mutex":
            self.walk_expr_args(e, tid, env, record)
            return LocRef(self.new_loc(e.span), "mutex")
        if name == "alias":
            base = self.name_ref(e.args[0], env) if e.args else None
            return LocRef(base.loc if base else self.new_loc(e.span),
                          "sharedcell")
        if name == "share":
            base = self.name_ref(e.args[0], env) if e.args else None
            return LocRef(base.loc if base else self.new_loc(e.span), "shared")
        if name == "get":
            ref = self.name_ref(e.args[0], env) if e.args else None
            if ref is not None:
                record(ref.loc, False, False, e.span, "read")
            return None
        if name == "set":
            ref = self.name_ref(e.args[0], env) if e.args else None
            if ref is not None:
                record(ref.loc, True, False, e.span, "write")
            if len(e.args) > 1:
                self.walk_expr(e.args[1], tid, env, record)
            return None
        if name == "load":
            ref = self.name_ref(e.args[0], env) if e.args else None
            if ref is not None:
                record(ref.loc, False, True, e.span, "read")
            return None
        # print / user fns: walk args for nested accesses.
        self.walk_expr_args(e, tid, env, record)
        return None

    def walk_expr_args(self, e: ast.Call, tid, env, record):
        for a in e.args:
            self.walk_expr(a, tid, env, record)

    def walk_expr(self, e, tid, env, record):
        """Walk an expression recording memory accesses (get/set/load and
        reads of a lock guard)."""
        if isinstance(e, ast.Name):
            info = env.get(e.ident)
            if info is not None and info.kind == "guard":
                record(info.loc, False, True, e.span, "read")
        elif isinstance(e, ast.Call):
            self.eval_call_ref(e, tid, env, record)
        elif isinstance(e, ast.Binary):
            self.walk_expr(e.left, tid, env, record)
            self.walk_expr(e.right, tid, env, record)
        elif isinstance(e, ast.Unary):
            self.walk_expr(e.operand, tid, env, record)
        elif isinstance(e, ast.SpawnExpr):
            # A spawn in raw expression position (unusual) — analyze its body.
            child = self.new_tid()
            self.analyze_block(e.body, child, dict(env))

    def name_ref(self, e, env) -> Optional[LocRef]:
        if isinstance(e, ast.Name):
            return env.get(e.ident)
        return None

    # -- the actual race check ---------------------------------------------
    def detect_races(self):
        by_loc: Dict[int, List[Access]] = defaultdict(list)
        for a in self.accesses:
            by_loc[a.loc].append(a)

        reported: Set[frozenset] = set()
        for loc, accs in by_loc.items():
            for i in range(len(accs)):
                for j in range(i + 1, len(accs)):
                    a, b = accs[i], accs[j]
                    if a.thread == b.thread:
                        continue
                    if frozenset((a.thread, b.thread)) not in self.concurrent:
                        continue
                    if not (a.write or b.write):
                        continue  # read/read never races
                    # A common lock is held iff BOTH accesses are synchronized
                    # (the mutex protecting `loc` is the shared lock).
                    if a.synchronized and b.synchronized:
                        continue
                    # One diagnostic per (location, pair-of-threads): a
                    # read/write and write/write conflict on the same value
                    # between the same two threads is one race, not two.
                    key = (loc, frozenset((a.thread, b.thread)))
                    if key in reported:
                        continue
                    reported.add(key)
                    self.report_race(loc, a, b)

    def report_race(self, loc: int, a: Access, b: Access):
        writer = a if a.write else b
        other = b if writer is a else a
        origin = self.loc_origin.get(loc)
        origin_note = (f"the shared value was created at line {origin.line}"
                       if origin else "the value is shared across threads")
        self.diags.append(Diagnostic(
            "data-race",
            f"data race: thread {writer.thread} writes a shared value that "
            f"thread {other.thread} {other.kind}s concurrently, with no lock "
            f"held",
            writer.span,
            help=(
                "wrap the value in a mutex and take a lock around every "
                "access:\n"
                "    let m = mutex(0);\n"
                "    let s = share(m);        // move into a shared handle\n"
                "    // in each thread:\n"
                "    lock s as g { g = g + 1; }\n"
                "or give the value a single owner by moving it into one thread."
            ),
            notes=[
                origin_note,
                f"conflicting {other.kind} is at line {other.span.line}, "
                f"column {other.span.col}",
            ],
        ))


# ===========================================================================
# Orchestration
# ===========================================================================

@dataclass
class CheckResult:
    diagnostics: List[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.diagnostics) == 0


def check_program(program: ast.Program) -> CheckResult:
    diags: List[Diagnostic] = []
    diags.extend(OwnershipChecker(program).check())
    diags.extend(RaceAnalyzer().analyze(program))
    # Stable ordering by source position for deterministic output.
    diags.sort(key=lambda d: (d.span.line, d.span.col))
    return CheckResult(diags)
