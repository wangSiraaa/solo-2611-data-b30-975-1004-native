"""Tests for the contrastive textbook, dictionary desk, and tile board.

Run with ``python -m unittest discover -s tests``.  These tests exercise the
claims in the brief directly: per-chunk word-count booklets, no arbitrary
splitting of a multi-segment chunk, no borrowing of an old chunking, the
dictionary desk's joint-feasibility answers with full witnesses, contradictory
books being refused, and the pre-reveal tile board under "any compatible
dictionary".
"""

from __future__ import annotations

import os
import tempfile
import unittest

from delivery_system.errors import (
    ExerciseContradiction,
    TextbookContradiction,
    TextbookLimitExceeded,
)
from delivery_system.models import SourceProduct, SourceSegment
from delivery_system.textbook import (
    DictionarySpace,
    Exercise,
    Textbook,
    analyze_package,
    build_book,
    frozen_multiset,
    package_supply,
    solve_allocation,
    verify_package_against_book,
)
from delivery_system.textbook_demo import run_textbook_demo


def product(*segments):
    return SourceProduct(
        document_id="D",
        document_revision=1,
        segments=tuple(
            SourceSegment(segment_id=sid, position=i, revision=rev, text=text)
            for i, (sid, rev, text) in enumerate(segments)
        ),
    )


def chunk(tid, ids, text, revs=None):
    return {
        "translation_id": tid,
        "source_ids": list(ids),
        "source_revisions": list(revs if revs is not None else [1] * len(ids)),
        "text": text,
    }


def package(document_id, snapshot, chunks, package_id="P", sha="sha"):
    return {
        "format": "translation-package/1",
        "package_id": package_id,
        "document_id": document_id,
        "language": "EN",
        "source_snapshot": [
            {"segment_id": sid, "revision": rev} for sid, rev in snapshot
        ],
        "chunks": chunks,
        "content_sha256": sha,
    }


class BookletTests(unittest.TestCase):
    def test_multiset_ignores_word_order(self):
        segments = [("S1", 1, "a b c"), ("S2", 1, "d")]
        book = build_book(
            "D", "EN", "P", "sha",
            segments,
            [chunk("C", ["S1", "S2"], "y x w z")],
        )
        pair = book.pairs[0]
        self.assertEqual(
            pair.source_words, (("a", 1), ("b", 1), ("c", 1), ("d", 1))
        )
        self.assertEqual(
            pair.target_words, (("w", 1), ("x", 1), ("y", 1), ("z", 1))
        )

    def test_two_segment_chunk_is_one_pair(self):
        segments = [("S1", 1, "p"), ("S2", 1, "q")]
        book = build_book(
            "D", "EN", "P", "sha",
            segments,
            [chunk("C", ["S1", "S2"], "x y")],
        )
        self.assertEqual(len(book.pairs), 1)
        self.assertEqual(book.pairs[0].source_ids, ("S1", "S2"))
        self.assertEqual(book.pairs[0].source_multiset, {"p": 1, "q": 1})

    def test_counts_are_preserved(self):
        segments = [("S1", 1, "a a b")]
        book = build_book(
            "D", "EN", "P", "sha", segments, [chunk("C", ["S1"], "u u v")]
        )
        self.assertEqual(book.pairs[0].source_words, (("a", 2), ("b", 1)))
        self.assertEqual(book.pairs[0].target_words, (("u", 2), ("v", 1)))

    def test_analyze_package_checks_snapshot_revision(self):
        live = product(("S1", 2, "a"))
        pkg = package("D", [("S1", 1)], [chunk("C", ["S1"], "u")])
        with self.assertRaises(TextbookContradiction):
            analyze_package(live, pkg)

    def test_analyze_package_checks_exact_cover(self):
        live = product(("S1", 1, "a"), ("S2", 1, "b"))
        pkg = package("D", [("S1", 1), ("S2", 1)], [chunk("C", ["S1"], "u")])
        with self.assertRaises(TextbookContradiction):
            analyze_package(live, pkg)

    def test_analyze_package_uses_published_text(self):
        live = product(("S1", 1, "a b"))
        pkg = package("D", [("S1", 1)], [chunk("C", ["S1"], "u v")])
        book = analyze_package(live, pkg)
        self.assertEqual(book.pairs[0].source_multiset, {"a": 1, "b": 1})

    def test_borrowed_chunking_rejected_even_if_text_same(self):
        segments = [("S1", 1, "p"), ("S2", 1, "q")]
        bundled = build_book(
            "D", "EN", "P", "sha",
            segments,
            [chunk("XY", ["S1", "S2"], "x y")],
        )
        split_pkg = package(
            "D",
            [("S1", 1), ("S2", 1)],
            [chunk("X", ["S1"], "x"), chunk("Y", ["S2"], "y")],
            package_id="P",
            sha="sha",
        )
        # Identical rendered words, different chunking: must be refused.
        with self.assertRaises(TextbookContradiction):
            verify_package_against_book(bundled, split_pkg)

    def test_roundtrip_to_from_dict(self):
        book = build_book(
            "D", "EN", "P", "sha",
            [("S1", 1, "a")],
            [chunk("C", ["S1"], "u")],
        )
        again = Textbook.from_dict(book.to_dict())
        self.assertEqual(again, book)


class DictionaryDeskTests(unittest.TestCase):
    def _bundle_book(self):
        return build_book(
            "D", "EN", "P", "sha",
            [("S1", 1, "p"), ("S2", 1, "q")],
            [chunk("XY", ["S1", "S2"], "x y")],
        )

    def _split_book(self):
        return build_book(
            "D", "EN", "P", "sha",
            [("S1", 1, "p"), ("S2", 1, "q")],
            [chunk("X", ["S1"], "x"), chunk("Y", ["S2"], "y")],
        )

    def test_bundled_book_has_two_dictionaries(self):
        space = DictionarySpace(self._bundle_book())
        self.assertTrue(space.consistent)
        self.assertFalse(space.unique)
        self.assertEqual(
            space.all_dictionaries(),
            [{"p": "x", "q": "y"}, {"p": "y", "q": "x"}],
        )
        self.assertEqual(space.image_of("p"), ("x", "y"))

    def test_split_book_is_unique(self):
        space = DictionarySpace(self._split_book())
        self.assertTrue(space.unique)
        self.assertEqual(space.dictionary, {"p": "x", "q": "y"})
        self.assertEqual(space.image_of("p"), ("x",))
        self.assertEqual(space.image_of("q"), ("y",))

    def test_pair_request_feasible_gives_witness(self):
        space = DictionarySpace(self._bundle_book())
        result = space.check_pairs([("p", "x")])
        self.assertTrue(result.feasible)
        self.assertEqual(result.dictionary["p"], "x")
        self.assertEqual(set(result.dictionary.values()), {"x", "y"})

    def test_two_alone_feasible_together_not(self):
        space = DictionarySpace(self._bundle_book())
        self.assertTrue(space.check_pairs([("p", "x")]).feasible)
        self.assertTrue(space.check_pairs([("p", "y")]).feasible)
        together = space.check_pairs([("p", "x"), ("p", "y")])
        self.assertFalse(together.feasible)
        self.assertIsNone(together.dictionary)

    def test_injectivity_violation_rejected(self):
        space = DictionarySpace(self._bundle_book())
        # p->x and q->x share a target: not a bijection.
        result = space.check_pairs([("p", "x"), ("q", "x")])
        self.assertFalse(result.feasible)

    def test_matching_detects_hall_violation(self):
        # source rows have locally non-empty candidate sets, but rows 0 and 1
        # both only reach target 0, so no injective assignment exists.
        from delivery_system.textbook import _perfect_matching

        self.assertIsNone(_perfect_matching([(0,), (0,), (0, 1)]))
        # A lock that steals the only target two rows can reach is infeasible.
        self.assertIsNone(_perfect_matching([(0,), (0, 1)], {1: 0}))
        self.assertIsNotNone(_perfect_matching([(0, 1), (0, 1)], {0: 0}))

    def test_contradictory_book_refused(self):
        book = build_book(
            "D", "EN", "P", "sha",
            [("S1", 1, "a b")],
            [chunk("C", ["S1"], "u u")],
        )
        space = DictionarySpace(book)
        self.assertFalse(space.consistent)
        self.assertIsNotNone(space.contradiction)
        self.assertFalse(space.check_pairs([("a", "u")]).feasible)

    def test_unequal_sized_vocabularies_contradiction(self):
        book = build_book(
            "D", "EN", "P", "sha",
            [("S1", 1, "a b")],
            [chunk("C", ["S1"], "u u")],
        )
        self.assertFalse(DictionarySpace(book).consistent)

    def test_sign_off_blocked_for_contradiction(self):
        book = build_book(
            "D", "EN", "P", "sha",
            [("S1", 1, "a")],
            [chunk("C", ["S1"], "u v")],
        )
        with self.assertRaises(TextbookContradiction):
            solve_allocation(book, DictionarySpace(book), [], {})

    def test_dictionary_limit(self):
        words = " ".join(f"a{i}" for i in range(25))
        targets = " ".join(f"b{i}" for i in range(25))
        book = build_book(
            "D", "EN", "P", "sha", [("S1", 1, words)], [chunk("C", ["S1"], targets)]
        )
        with self.assertRaises(TextbookLimitExceeded):
            DictionarySpace(book)


class TileBoardTests(unittest.TestCase):
    def _bundle_setup(self):
        book = build_book(
            "D", "EN", "P", "sha",
            [("S1", 1, "p"), ("S2", 1, "q")],
            [chunk("XY", ["S1", "S2"], "x y")],
        )
        return book, DictionarySpace(book)

    def _split_setup(self):
        book = build_book(
            "D", "EN", "P", "sha",
            [("S1", 1, "p"), ("S2", 1, "q")],
            [chunk("X", ["S1"], "x"), chunk("Y", ["S2"], "y")],
        )
        return book, DictionarySpace(book)

    def _exercises(self):
        return [
            Exercise.from_tokens("EP", "甲", ["p"], 3),
            Exercise.from_tokens("EQ", "乙", ["q"], 2),
        ]

    def test_bundled_book_caps_at_3(self):
        book, space = self._bundle_setup()
        self.assertEqual(package_supply(book), {"x": 1, "y": 1})
        result = solve_allocation(book, space, self._exercises())
        self.assertEqual(result.score, 3)
        self.assertEqual([p.table for p in result.plans], ["甲"])
        # Table 甲 needs both kinds under the bundled two-way dictionary.
        self.assertEqual(dict(result.plans[0].bag), {"x": 1, "y": 1})
        self.assertEqual(result.plans[0].tile_count, 2)
        self.assertEqual(len(result.unselected), 1)
        short = result.unselected[0]
        self.assertEqual(short.table, "乙")
        self.assertTrue(short.shortages)
        self.assertTrue(short.primary[0] in ("x", "y"))

    def test_split_book_scores_5(self):
        book, space = self._split_setup()
        result = solve_allocation(book, space, self._exercises())
        self.assertEqual(result.score, 5)
        bags = {p.table: dict(p.bag) for p in result.plans}
        self.assertEqual(bags, {"甲": {"x": 1}, "乙": {"y": 1}})
        self.assertEqual(result.unselected, ())

    def test_keep_bundle_double_copies_scores_5(self):
        book, space = self._bundle_setup()
        result = solve_allocation(book, space, self._exercises(), {"x": 2, "y": 2})
        self.assertEqual(result.score, 5)
        self.assertEqual(sorted(p.table for p in result.plans), ["乙", "甲"])

    def test_over_provisioning_allowed(self):
        # "p p" needs two x and two y under the bundled dictionary.
        book, space = self._bundle_setup()
        ex = [Exercise.from_tokens("E", "甲", ["p", "p"], 1)]
        result = solve_allocation(book, space, ex, {"x": 2, "y": 2})
        self.assertTrue(result.ok)
        self.assertEqual(dict(result.plans[0].bag), {"x": 2, "y": 2})

    def test_one_sheet_per_table(self):
        book, space = self._bundle_setup()
        ex = [
            Exercise.from_tokens("E1", "甲", ["p"], 1),
            Exercise.from_tokens("E2", "甲", ["q"], 5),
        ]
        with self.assertRaises(ExerciseContradiction):
            solve_allocation(book, space, ex)

    def test_exercise_points_must_be_positive(self):
        with self.assertRaises(ExerciseContradiction):
            Exercise.from_tokens("E", "甲", ["p"], 0)

    def test_exercise_outside_vocab_rejected(self):
        book, space = self._bundle_setup()
        ex = [Exercise.from_tokens("E", "甲", ["zzz"], 1)]
        with self.assertRaises(ExerciseContradiction):
            solve_allocation(book, space, ex, {"x": 5, "y": 5})

    def test_tie_break_is_deterministic(self):
        book, space = self._split_setup()
        ex = [
            Exercise.from_tokens("EA", "甲", ["p"], 2),
            Exercise.from_tokens("EB", "乙", ["q"], 2),
        ]
        first = solve_allocation(book, space, ex, {"x": 5, "y": 5})
        second = solve_allocation(book, space, ex, {"x": 5, "y": 5})
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.score, 4)

    def test_exercise_limit(self):
        book, space = self._bundle_setup()
        ex = [
            Exercise.from_tokens(f"E{i}", f"T{i}", ["p"], 1) for i in range(23)
        ]
        with self.assertRaises(TextbookLimitExceeded):
            solve_allocation(book, space, ex, {"x": 99, "y": 99})

    def test_shared_supply_not_per_table(self):
        # Two tables each wanting the one x tile cannot both be served unless a
        # second copy is bought.
        book, space = self._split_setup()
        ex = [
            Exercise.from_tokens("EA", "甲", ["p"], 2),
            Exercise.from_tokens("EB", "乙", ["p"], 2),
        ]
        result = solve_allocation(book, space, ex, {"x": 1})
        self.assertEqual(result.score, 2)
        self.assertEqual(len(result.plans), 1)
        result2 = solve_allocation(book, space, ex, {"x": 2})
        self.assertEqual(result2.score, 4)


class ScaleAndShapeTests(unittest.TestCase):
    def test_fully_symmetric_book(self):
        # One chunk pairs 24 source tokens with 24 target tokens: every
        # permutation is legal, so the book is symmetric on all 24 words.
        n = 24
        src = [f"a{i}" for i in range(n)]
        tgt = [f"b{i}" for i in range(n)]
        book = build_book(
            "D", "EN", "P", "sha",
            [("S1", 1, " ".join(src))],
            [chunk("C", ["S1"], " ".join(reversed(tgt)))],
        )
        space = DictionarySpace(book)
        self.assertTrue(space.consistent)
        self.assertFalse(space.unique)
        self.assertEqual(len(space.image_of("a0")), n)
        self.assertEqual(len(space.all_dictionaries(limit=5)), 5)

    def test_unique_dictionary_with_contention(self):
        # 24 identity chunks pin every word; unique dictionary but tiles for
        # the one hot source word are scarce.
        n = 24
        src = [f"a{i}" for i in range(n)]
        tgt = [f"b{i}" for i in range(n)]
        segments = [(f"S{i}", 1, src[i]) for i in range(n)]
        chunks = [
            chunk(f"C{i}", [f"S{i}"], tgt[i]) for i in range(n)
        ]
        book = build_book("D", "EN", "P", "sha", segments, chunks)
        space = DictionarySpace(book)
        self.assertTrue(space.unique)
        self.assertEqual(space.dictionary, {src[i]: tgt[i] for i in range(n)})
        # Five tables all want a0; only two copies of b0 exist.
        exercises = [
            Exercise.from_tokens(f"E{i}", f"T{i}", ["a0"], 1) for i in range(5)
        ]
        result = solve_allocation(book, space, exercises, {"b0": 2})
        self.assertEqual(result.score, 2)
        self.assertEqual(len(result.plans), 2)
        self.assertEqual(len(result.unselected), 3)
        self.assertEqual(result.unselected[0].primary, ("b0", 1))


class DemoReproductionTests(unittest.TestCase):
    def test_full_demo_all_checks_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = run_textbook_demo(tmp)
        failed = [c for c in report["checks"] if not c["ok"]]
        self.assertTrue(report["ok"], failed)
        self.assertGreaterEqual(len(report["checks"]), 25)


if __name__ == "__main__":
    unittest.main()
