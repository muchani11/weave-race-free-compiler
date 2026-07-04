# Weave — a data-race-free compiler front end

Weave compiles a small concurrent language **only if it can prove that no data
race can ever occur at runtime.** If a race is possible under *any* thread
interleaving, the program is rejected with an explanation and a suggested
race-free rewrite.

```
$ weave check examples/racy/parent_child_conflict.wv
error[data-race]: data race: thread 1 writes a shared value that thread 0
                  writes concurrently, with no lock held
    --> examples/racy/parent_child_conflict.wv:7:9
   7 |         set(s, 1);
               ^^^
    note: the shared value was created at line 4
    help: wrap the value in a mutex and take a lock around every access:
              let m = mutex(0);
              let s = share(m);
              lock s as g { g = g + 1; }
          or give the value a single owner by moving it into one thread.
✗ rejected: 1 problem(s) — this code could race.

$ weave check examples/safe/mutex_counter.wv -v
✓ compiled: no data race is possible.
  model checker explored 160 interleavings: no racing schedule exists.
```

---

## The core idea

A data race is: two threads access the same memory location, at least one
access is a write, and nothing orders them. Detecting this by *enumerating all
interleavings* is exponential (and undecidable in general once you add loops
and unbounded threads). So Weave does what Rust does — it makes races
**unrepresentable in the type system** instead of hunting for them:

> **Aliasing XOR mutation.** A value may have many readers, *or* one writer,
> never both at the same time across threads. The only way to get shared
> mutation is to go through a lock.

That single rule is enforced by an **ownership / borrow checker with
thread-escape analysis**, and it guarantees race freedom *without* enumerating
schedules. This is the primary, sound engine.

A **second, independent engine** — a bounded interleaving model checker —
exists to *demonstrate* the result: it lowers each thread to atomic memory
operations (with real lock acquire/release) and searches schedules to either
exhibit a concrete racy trace or confirm none exists. The two engines are
cross-checked against each other on every example in CI (`EngineAgreementTests`).

Why keep both? The type system is the *proof*; the model checker is the
*witness*. Proofs convince the compiler; witnesses convince the programmer.

---

## The language

Weave is intentionally tiny — the interesting engineering is in the checker,
not the syntax. Functions, `let`/`let mut`, `if`/`while`, integer and boolean
expressions, plus four concurrency-relevant object kinds:

| Constructor      | Type            | Sharing                | Safe to write concurrently? |
|------------------|-----------------|------------------------|-----------------------------|
| `cell(0)`        | `Cell`          | single owner (moves)   | n/a — only one thread owns it |
| `alias(c)`       | `SharedCell`    | shared, **no lock**    | ❌ writing it is a data race |
| `mutex(0)`       | `Mutex`         | single owner (moves)   | n/a |
| `share(m)`       | `Shared`        | shared, lock-mediated  | ✅ via `lock` |

Operations: `get(x)` / `set(x, v)` (unsynchronized), `load(s)` (synchronized
read of a `Shared`), and

```
lock s as g {      // exclusive, synchronized access to the protected int
    g = g + 1;     // `g` is a mutable view; reads/writes are race-free
}
```

Concurrency: `spawn { ... }` returns a handle; `join(h)` waits.

`alias` is deliberately the "footgun" primitive — an *unsynchronized* shared
handle, exactly like `Rc`. It is perfectly legal single-threaded; it becomes a
compile error the instant two concurrent threads touch it and one writes. That
is the whole philosophy in one primitive: sharing is fine, *unsynchronized
shared mutation* is not.

---

## How the checker works

Two passes over each function (`weave/checker.py`):

**A. Ownership / borrow / move (`OwnershipChecker`).**
Infers types, validates built-ins, and tracks whether each non-`Copy` value is
live or *moved*. Reading a value after it was moved (e.g. into a thread) is a
use-after-move error. `set` requires the binding to be `mut`. This pass is what
makes "give the value a single owner" work: moving a `Cell` into a `spawn`
transfers ownership, so the parent literally cannot reference it afterward.

**B. Thread-escape race analysis (`RaceAnalyzer`).**
- Every `cell`/`mutex` gets a unique **location id**; `alias`/`share` and
  variable bindings propagate it, so all handles to one object share an id.
- The spawn/join structure builds a **thread tree** and a *can-run-concurrently*
  relation: a child is concurrent with parent code between its `spawn` and its
  `join`, and with any sibling whose lifetime overlaps. Code before a spawn or
  after its join is ordered (happens-before) and therefore *not* concurrent.
- Every `get`/`set`/`load`/`lock` is recorded as an access `(location, read|write,
  synchronized?)` tagged with its thread.
- **Race rule:** two accesses to the same location, from concurrent threads,
  at least one a write, not *both* synchronized under the protecting lock → data
  race. Mutex accesses are synchronized and share one lock, so they never race;
  `alias` accesses are never synchronized, so concurrent writes always do.

Because concurrency is derived from `spawn`/`join` structure and synchronization
from lock discipline, the analysis is **sound** (no false "safe") for the
supported language, and never has to enumerate interleavings.

---

## Usage

```bash
python3 -m weave.cli check  file.wv        # prove race freedom (exit 0/1)
python3 -m weave.cli check  file.wv -v     # also run the model checker
python3 -m weave.cli explain file.wv       # print a concrete racy schedule
```

Install as a real CLI:

```bash
pip install -e .
weave check examples/safe/mutex_counter.wv -v
```

The **exit code is the contract**: `0` means "compiled, provably race-free".
Drop `weave check` into CI and a merge can never introduce a data race.

Run the tests (no dependencies required):

```bash
python3 -m unittest discover -s tests -v
```

---

## Worked examples

`examples/safe/` all compile; `examples/racy/` are all rejected:

- **`mutex_counter`** — two threads increment a shared counter through a
  `lock`. Safe: the model checker confirms across all 160 interleavings.
- **`move_ownership`** — a `Cell` is moved into one thread; no sharing, no race.
- **`read_only_share`** — many readers of an `alias`, zero writers. Safe.
- **`unsynchronized_counter`** — two threads `set` an `alias`. Rejected.
- **`parent_child_conflict`** — parent writes while the child runs. Rejected.
- **`use_after_move`** — the ownership rule catches the race as a move error.

---

## Design honesty: scope & limitations

This is a rigorous MVP, not a production compiler. Deliberate boundaries:

- **Granularity is whole-object, integer payloads.** No struct fields or arrays
  yet; adding them means per-field location ids (straightforward) and an alias
  analysis for indexing (not).
- **Concurrency shape is structured `spawn`/`join`.** Threads stored in data
  structures, detached threads, or condition variables would need an escape
  analysis over the heap, not just the syntax tree.
- **Capture analysis ignores shadowing** in the conservative `collect_names`
  pass — fine for this language, would need proper scoping for a larger one.
- **The model checker is bounded** (200k states) and models each synchronized
  access as a one-op critical section; it is a witness/teaching tool, and the
  type system — not the search — is the actual proof of safety.
- **No codegen.** Weave is a *front end*: it type-checks and proves safety. A
  backend (lower to C/LLVM with real `pthread`/`std::mutex`) is the natural next
  milestone; the ownership model already guarantees the emitted code is sound.

## Roadmap

1. Structs/arrays with field-sensitive locations.
2. `Send`/`Sync`-style marker traits so user types opt into shareability.
3. Channels (`send`/`recv`) as a first-class move-based message-passing path.
4. A codegen backend emitting race-free C.
5. Deadlock detection (lock-ordering analysis) — the natural sequel to race
   freedom.

---

## Layout

```
weave/
  lexer.py         hand-written tokenizer
  parser.py        recursive-descent + Pratt expressions
  ast.py           dataclass AST nodes with source spans
  checker.py       ownership/borrow checker + race analysis  ← the core
  interleave.py    bounded interleaving model checker (witness engine)
  diagnostics.py   spans + Rust-style rendered errors with `help:`
  cli.py           `weave check` / `weave explain`
examples/          safe/ (compile) and racy/ (rejected)
tests/             unittest suite incl. cross-engine agreement
```
