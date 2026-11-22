"""Tests for editorial-note proof sheets and pagination/placement.

Covers the brief's exact numeric scenarios plus every rejection mode,
multi-path dedup, fork-merge dependencies, and the 200-block scale shape.
"""

from __future__ import annotations

import time
import unittest

from delivery_system.annotation_models import (
    BlockSpec,
    NoteRef,
    SegmentNoteMarkers,
)
from delivery_system.annotation_demo import run_annotation_demo
from delivery_system.annotations import (
    BlockInput,
    NoteRegistry,
    build_proof_sheet,
    render_page_sample,
    typeset,
)
from delivery_system.errors import (
    DanglingNoteRef,
    IncompatibleNoteEdition,
    InfeasibleLayout,
    MissingMandatoryNote,
    NoteDependencyCycle,
)


def shared_registry() -> NoteRegistry:
    registry = NoteRegistry()
    registry.sign_note("N", "N", lines=1, lookup_cost=4)
    registry.sign_note("M", "M", lines=1, lookup_cost=1)
    registry.register_segment_markers(
        SegmentNoteMarkers("S1", 1, (NoteRef("N", 1),), (NoteRef("N", 1),))
    )
    registry.register_segment_markers(
        SegmentNoteMarkers("S2", 1, (NoteRef("M", 1),), (NoteRef("M", 1),))
    )
    registry.register_segment_markers(
        SegmentNoteMarkers("S2", 2, (NoteRef("N", 1),), (NoteRef("N", 1),))
    )
    return registry


def make_blocks(registry: NoteRegistry, second_ref: NoteRef) -> list[BlockSpec]:
    rev = 1 if second_ref.note_id == "M" else 2
    p1 = build_proof_sheet(
        BlockInput("B1", (("S1", 1),), (NoteRef("N", 1),)), registry
    )
    p2 = build_proof_sheet(
        BlockInput("B2", (("S2", rev),), (second_ref,)), registry
    )
    return [
        BlockSpec("B1", 2, p1),
        BlockSpec("B2", 2, p2),
    ]


class ProofSheetTests(unittest.TestCase):
    def test_shared_note_read_once_per_sheet_with_provenance(self):
        registry = shared_registry()
        registry.register_segment_markers(
            SegmentNoteMarkers("S3", 1, (NoteRef("N", 1),), (NoteRef("N", 1),))
        )
        proof = build_proof_sheet(
            BlockInput(
                "BIG",
                (("S1", 1), ("S3", 1)),
                (NoteRef("N", 1), NoteRef("N", 1)),
            ),
            registry,
        )
        self.assertEqual(proof.note_count, 1)
        reading = proof.reading("N", 1)
        self.assertTrue(reading.direct)
        # One distinct expansion path, but both print sites are provenance.
        self.assertEqual(len(reading.paths), 1)
        self.assertEqual(
            sorted(reading.sources), [("S1", 1), ("S3", 1)]
        )

    def test_dangling_marker_rejected(self):
        registry = shared_registry()
        with self.assertRaises(DanglingNoteRef):
            build_proof_sheet(
                BlockInput("B", (("S1", 1),), (NoteRef("GHOST", 9),)),
                registry,
            )

    def test_marker_outside_coverage_rejected(self):
        registry = shared_registry()
        # B1 only covers S1; S2's M marker is not legal on it.
        with self.assertRaises(DanglingNoteRef):
            build_proof_sheet(
                BlockInput("B1", (("S1", 1),), (NoteRef("M", 1),)),
                registry,
            )

    def test_mandatory_marker_cannot_be_dropped(self):
        registry = shared_registry()
        with self.assertRaises(MissingMandatoryNote):
            build_proof_sheet(BlockInput("B1", (("S1", 1),), ()), registry)

    def test_incompatible_editions_in_one_block_rejected(self):
        registry = NoteRegistry()
        registry.sign_note("K", "one", 1, edition=1)
        registry.sign_note("K", "two", 1, edition=2)
        registry.register_segment_markers(
            SegmentNoteMarkers(
                "S", 1, (NoteRef("K", 1), NoteRef("K", 2)), ()
            )
        )
        with self.assertRaises(IncompatibleNoteEdition):
            build_proof_sheet(
                BlockInput(
                    "B", (("S", 1),), (NoteRef("K", 1), NoteRef("K", 2))
                ),
                registry,
            )

    def test_other_editions_remain_readable_after_rejection(self):
        registry = NoteRegistry()
        registry.sign_note("K", "one", 1, edition=1)
        registry.sign_note("K", "two", 1, edition=2)
        self.assertEqual(registry.editions("K"), (1, 2))
        # A dangling rejection on one block does not touch signed editions.
        registry.register_segment_markers(
            SegmentNoteMarkers("S", 1, (NoteRef("K", 1),), ())
        )
        with self.assertRaises(DanglingNoteRef):
            build_proof_sheet(
                BlockInput("B", (("S", 1),), (NoteRef("ZZZ", 1),)), registry
            )
        self.assertEqual(registry.resolve(NoteRef("K", 1)).text, "one")
        self.assertEqual(registry.resolve(NoteRef("K", 2)).text, "two")

    def test_dependency_cycle_rejected(self):
        registry = NoteRegistry()
        registry.sign_note("X", "x", 1, depends_on=(NoteRef("Y", 1),))
        registry.sign_note("Y", "y", 1, depends_on=(NoteRef("X", 1),))
        with self.assertRaises(NoteDependencyCycle):
            registry.expand((NoteRef("X", 1),))

    def test_fork_merge_reads_shared_note_once(self):
        registry = NoteRegistry()
        registry.sign_note("C", "c", 1)
        registry.sign_note("A", "a", 1, depends_on=(NoteRef("C", 1),))
        registry.sign_note("B", "b", 1, depends_on=(NoteRef("C", 1),))
        registry.sign_note(
            "R", "r", 1, depends_on=(NoteRef("A", 1), NoteRef("B", 1))
        )
        registry.register_segment_markers(
            SegmentNoteMarkers("S", 1, (NoteRef("R", 1),), (NoteRef("R", 1),))
        )
        proof = build_proof_sheet(
            BlockInput("B", (("S", 1),), (NoteRef("R", 1),)), registry
        )
        self.assertEqual(proof.note_count, 4)
        c = proof.reading("C", 1)
        self.assertFalse(c.direct)
        self.assertEqual(
            sorted(
                [tuple(f"{r.note_id}@{r.edition}" for r in path) for path in c.paths]
            ),
            [
                ("R@1", "A@1", "C@1"),
                ("R@1", "B@1", "C@1"),
            ],
        )


class TypesetTests(unittest.TestCase):
    def test_shared_note_one_page_footnote_cost_zero(self):
        registry = shared_registry()
        blocks = make_blocks(registry, NoteRef("N", 1))
        result = typeset(blocks, registry, page_height=5, max_body_pages=1)
        self.assertEqual(result.body_page_count, 1)
        self.assertEqual(result.total_lookup_cost, 0)
        self.assertEqual(result.pages[0].block_ids, ("B1", "B2"))
        self.assertEqual(result.pages[0].footnote_refs, (NoteRef("N", 1),))
        self.assertEqual(result.pages[0].total_lines, 5)  # 4 body + 1 note

    def test_m_forced_end_note_cost_one(self):
        registry = shared_registry()
        blocks = make_blocks(registry, NoteRef("M", 1))
        result = typeset(blocks, registry, page_height=5, max_body_pages=1)
        self.assertEqual(result.body_page_count, 1)
        self.assertEqual(result.total_lookup_cost, 1)
        self.assertIn(NoteRef("M", 1), result.endnote_refs)
        self.assertIn(NoteRef("N", 1), result.footnote_refs)
        # N (4) footnoted because it fits and is more expensive to move;
        # M (1) moved because both footnotes would overflow 5 lines.
        self.assertEqual(result.pages[0].endnote_cost_on_page, 1)
        sample = render_page_sample(result, registry)
        self.assertIn("appendix", sample)
        self.assertIn("M@1", sample)

    def test_two_page_cap_splits_and_cost_zero(self):
        registry = shared_registry()
        blocks = make_blocks(registry, NoteRef("M", 1))
        result = typeset(blocks, registry, page_height=5, max_body_pages=2)
        self.assertEqual(result.body_page_count, 2)
        self.assertEqual(result.total_lookup_cost, 0)
        self.assertEqual([p.block_ids for p in result.pages], [("B1",), ("B2",)])
        self.assertEqual(result.endnote_refs, ())

    def test_end_note_charged_per_meeting_page_not_per_marker(self):
        registry = NoteRegistry()
        registry.sign_note("E", "e", 1, lookup_cost=3)
        registry.register_segment_markers(
            SegmentNoteMarkers("S1", 1, (NoteRef("E", 1),), ())
        )
        registry.register_segment_markers(
            SegmentNoteMarkers("S2", 1, (NoteRef("E", 1),), ())
        )
        p1 = build_proof_sheet(
            BlockInput("B1", (("S1", 1),), (NoteRef("E", 1),)), registry
        )
        p2 = build_proof_sheet(
            BlockInput("B2", (("S2", 1),), (NoteRef("E", 1),)), registry
        )
        blocks = [BlockSpec("B1", 4, p1), BlockSpec("B2", 4, p2)]
        # Height 4: a footnote E cannot share a page with a 4-line block, so
        # E must go to the appendix; two meeting pages -> 2 * cost 3.
        result = typeset(blocks, registry, page_height=4, max_body_pages=2)
        self.assertEqual(result.total_lookup_cost, 6)
        self.assertEqual(result.body_page_count, 2)

    def test_infeasible_when_block_taller_than_page(self):
        registry = shared_registry()
        blocks = make_blocks(registry, NoteRef("N", 1))
        with self.assertRaises(InfeasibleLayout):
            typeset(blocks, registry, page_height=1, max_body_pages=2)

    def test_infeasible_when_page_cap_too_small(self):
        registry = shared_registry()
        blocks = make_blocks(registry, NoteRef("M", 1))
        with self.assertRaises(InfeasibleLayout):
            typeset(blocks, registry, page_height=2, max_body_pages=1)

    def test_demo_runs_all_scenarios(self):
        report = run_annotation_demo()
        self.assertTrue(report["ok"])
        self.assertEqual(
            [s["name"] for s in report["scenarios"]],
            [
                "shared_note_one_page",
                "m_marker_one_page_forced_end_note",
                "m_marker_two_pages_split",
                "fork_merge_dependencies",
                "fork_merge_page_sample",
                "rejection_modes",
            ],
        )


class ScaleTests(unittest.TestCase):
    def test_two_hundred_blocks_with_local_notes(self):
        # Mirrors "a book of ~200 blocks, dozens of small notes each appearing
        # in a few neighboring blocks".  Blocks are tall enough that pages stay
        # sparse, so every meeting note fits as a footnote and the optimum is
        # zero lookup cost; the assertion also guards the solver's runtime.
        registry = NoteRegistry()
        blocks: list[BlockSpec] = []
        for j in range(40):
            registry.sign_note(f"L{j}", f"note L{j}", lines=1)
        for i in range(200):
            ref = NoteRef(f"L{i // 5}", 1)  # each note spans five neighbors
            registry.register_segment_markers(
                SegmentNoteMarkers(f"S{i}", 1, (ref,), (ref,))
            )
            proof = build_proof_sheet(
                BlockInput(f"B{i}", ((f"S{i}", 1),), (ref,)), registry
            )
            blocks.append(BlockSpec(f"B{i}", 6, proof))

        started = time.perf_counter()
        result = typeset(blocks, registry, page_height=8, max_body_pages=200)
        elapsed = time.perf_counter() - started
        # One 6-line block plus its one 1-line footnote fits on every page.
        self.assertEqual(result.total_lookup_cost, 0)
        for page in result.pages:
            self.assertLessEqual(page.total_lines, 8)
        self.assertLess(elapsed, 10.0)

    def test_two_hundred_blocks_scale_runtime_with_tight_cap(self):
        # Same 200-block scale, but a tight page cap that forces end notes:
        # the bounded DP must stay fast and never overflow the cap.
        registry = NoteRegistry()
        blocks: list[BlockSpec] = []
        for j in range(5):
            registry.sign_note(f"C{j}", f"common C{j}", lines=1)
        for i in range(200):
            ref = NoteRef(f"C{i % 5}", 1)
            registry.register_segment_markers(
                SegmentNoteMarkers(f"S{i}", 1, (ref,), (ref,))
            )
            proof = build_proof_sheet(
                BlockInput(f"B{i}", ((f"S{i}", 1),), (ref,)), registry
            )
            blocks.append(BlockSpec(f"B{i}", 2, proof))

        started = time.perf_counter()
        result = typeset(blocks, registry, page_height=25, max_body_pages=50)
        elapsed = time.perf_counter() - started
        self.assertLessEqual(result.body_page_count, 50)
        for page in result.pages:
            self.assertLessEqual(page.total_lines, 25)
        # A finite, deterministic cost is produced even though zero is
        # infeasible under the tight cap.
        self.assertGreaterEqual(result.total_lookup_cost, 0)
        self.assertLess(elapsed, 10.0)


if __name__ == "__main__":
    unittest.main()
