"""Test suite for the Weave data-race compiler.

Runs under `python3 -m unittest` (no third-party deps) or pytest.
"""
from __future__ import annotations

import glob
import os
import unittest

from weave import compile_source
from weave.checker import RaceAnalyzer
from weave.interleave import explore
from weave.lexer import lex
from weave.parser import parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def kinds(source: str):
    return {d.kind for d in compile_source(source).diagnostics}


# ---------------------------------------------------------------------------
class LexerTests(unittest.TestCase):
    def test_operators_and_keywords(self):
        toks = lex("fn main() { let mut x = 1 + 2; }")
        self.assertEqual(toks[0].kind, "kw")
        self.assertEqual(toks[0].value, "fn")
        self.assertEqual(toks[-1].kind, "eof")

    def test_two_char_ops(self):
        vals = [t.value for t in lex("a == b && c -> d")]
        self.assertIn("==", vals)
        self.assertIn("&&", vals)
        self.assertIn("->", vals)

    def test_comments_skipped(self):
        toks = lex("// hi\nfn f(){}")
        self.assertEqual(toks[0].value, "fn")

    def test_line_tracking(self):
        toks = lex("\n\nfn")
        self.assertEqual(toks[0].span.line, 3)


class ParserTests(unittest.TestCase):
    def test_precedence(self):
        prog = parse("fn f() { let x = 1 + 2 * 3; }")
        self.assertEqual(len(prog.fns), 1)

    def test_spawn_and_lock_parse(self):
        prog = parse("fn f() { let h = spawn { lock s as g { g = g + 1; } }; }")
        self.assertEqual(prog.fns[0].name, "f")


# ---------------------------------------------------------------------------
class SafeProgramTests(unittest.TestCase):
    def test_mutex_counter_compiles(self):
        src = """
        fn main() {
            let m = mutex(0);
            let s = share(m);
            let h1 = spawn { lock s as g { g = g + 1; } };
            let h2 = spawn { lock s as g { g = g + 1; } };
            join(h1); join(h2);
            print(load(s));
        }"""
        self.assertTrue(compile_source(src).ok)

    def test_move_into_single_thread_compiles(self):
        src = """
        fn main() {
            let mut c = cell(0);
            let h = spawn { set(c, 9); print(get(c)); };
            join(h);
        }"""
        self.assertTrue(compile_source(src).ok)

    def test_read_only_sharing_compiles(self):
        src = """
        fn main() {
            let c = cell(1);
            let s = alias(c);
            let h1 = spawn { print(get(s)); };
            let h2 = spawn { print(get(s)); };
            join(h1); join(h2);
        }"""
        self.assertTrue(compile_source(src).ok)

    def test_sequential_reuse_after_join_is_fine(self):
        # Access after a join isn't concurrent, so no race.
        src = """
        fn main() {
            let c = cell(0);
            let mut s = alias(c);
            let h = spawn { set(s, 1); };
            join(h);
            set(s, 2);
        }"""
        self.assertTrue(compile_source(src).ok)


class RaceRejectionTests(unittest.TestCase):
    def test_unsynchronized_counter_rejected(self):
        src = """
        fn main() {
            let c = cell(0);
            let mut s = alias(c);
            let h1 = spawn { set(s, get(s) + 1); };
            let h2 = spawn { set(s, get(s) + 1); };
            join(h1); join(h2);
        }"""
        self.assertIn("data-race", kinds(src))

    def test_parent_child_conflict_rejected(self):
        src = """
        fn main() {
            let c = cell(0);
            let mut s = alias(c);
            let h = spawn { set(s, 1); };
            set(s, 2);
            join(h);
        }"""
        self.assertIn("data-race", kinds(src))

    def test_write_read_conflict_rejected(self):
        src = """
        fn main() {
            let c = cell(0);
            let mut s = alias(c);
            let h = spawn { set(s, 5); };
            print(get(s));
            join(h);
        }"""
        self.assertIn("data-race", kinds(src))


class OwnershipTests(unittest.TestCase):
    def test_use_after_move_rejected(self):
        src = """
        fn main() {
            let mut c = cell(0);
            let h = spawn { set(c, 1); };
            print(get(c));
            join(h);
        }"""
        self.assertIn("use-after-move", kinds(src))

    def test_mutate_without_mut_rejected(self):
        src = """
        fn main() {
            let c = cell(0);
            set(c, 1);
        }"""
        self.assertIn("mutability", kinds(src))

    def test_undefined_variable_rejected(self):
        self.assertIn("name", kinds("fn main() { print(nope); }"))

    def test_lock_requires_mutex(self):
        src = """
        fn main() {
            let c = cell(0);
            lock c as g { g = g + 1; }
        }"""
        self.assertIn("type", kinds(src))


# ---------------------------------------------------------------------------
class InterleavingTests(unittest.TestCase):
    def _analyze(self, src):
        prog = parse(src)
        a = RaceAnalyzer()
        a.analyze(prog)
        return explore(a)

    def test_model_checker_finds_witness_for_race(self):
        src = """
        fn main() {
            let c = cell(0);
            let mut s = alias(c);
            let h1 = spawn { set(s, 1); };
            let h2 = spawn { set(s, 2); };
            join(h1); join(h2);
        }"""
        res = self._analyze(src)
        self.assertIsNotNone(res.witness)

    def test_model_checker_proves_mutex_safe(self):
        src = """
        fn main() {
            let m = mutex(0);
            let s = share(m);
            let h1 = spawn { lock s as g { g = g + 1; } };
            let h2 = spawn { lock s as g { g = g + 1; } };
            join(h1); join(h2);
        }"""
        res = self._analyze(src)
        self.assertTrue(res.race_free)


class EngineAgreementTests(unittest.TestCase):
    """The prover and the interleaving checker must never disagree on any
    example in the repo."""

    def _examples(self, subdir):
        return sorted(glob.glob(os.path.join(ROOT, "examples", subdir, "*.wv")))

    def test_all_examples_agree(self):
        for path in self._examples("safe") + self._examples("racy"):
            with self.subTest(example=os.path.basename(path)):
                with open(path) as f:
                    src = f.read()
                result = compile_source(src)
                prog = result.program
                self.assertIsNotNone(prog, f"{path} failed to parse")
                a = RaceAnalyzer()
                a.analyze(prog)
                res = explore(a)
                has_race_diag = any(
                    d.kind == "data-race" for d in result.diagnostics)
                # Flagged race => the model checker must find a witness.
                # Proven safe => no witness may exist.
                if has_race_diag:
                    self.assertIsNotNone(
                        res.witness, f"{path}: prover says race, MC disagrees")
                else:
                    self.assertTrue(
                        res.race_free, f"{path}: MC found race prover missed")


if __name__ == "__main__":
    unittest.main()
