"""Worked scenarios for editorial notes: proof sheets and page samples.

Reproduces the three situations from the brief:

1. two 2-line blocks sharing note N on a 5-line page with a one-page cap:
   N is footnoted once per page, both blocks share the page, zero lookup cost;
2. the second block's real marker switches to note M (lookup costs N=4, M=1):
   with one page, M must become an end note and the reader pays cost 1;
   with a two-page cap each block takes its own page and cost drops to zero;
3. a fork-then-merge dependency graph: two paths reach one shared edition,
   the proof sheet reads it once and the rendered page sample shows why.
"""

from __future__ import annotations

from typing import Any

from .annotation_models import (
    BlockSpec,
    NoteRef,
    SegmentNoteMarkers,
)
from .annotations import (
    BlockInput,
    NoteRegistry,
    build_proof_sheet,
    render_page_sample,
    typeset,
)
from .errors import (
    DanglingNoteRef,
    IncompatibleNoteEdition,
    MissingMandatoryNote,
    NoteDependencyCycle,
)


def _basic_registry(*, m_cost: int = 1) -> NoteRegistry:
    registry = NoteRegistry()
    registry.sign_note("N", "note N text", lines=1, lookup_cost=4)
    registry.sign_note("M", "note M text", lines=1, lookup_cost=m_cost)
    registry.register_segment_markers(
        SegmentNoteMarkers(
            segment_id="S1",
            revision=1,
            markers=(NoteRef("N", 1),),
            mandatory=(NoteRef("N", 1),),
        )
    )
    return registry


def _blocks_for(registry: NoteRegistry, second_ref: NoteRef) -> list[BlockSpec]:
    registry.register_segment_markers(
        SegmentNoteMarkers(
            segment_id="S2",
            revision=1,
            markers=(second_ref,),
            mandatory=(second_ref,),
        )
    )
    proof_one = build_proof_sheet(
        BlockInput("B1", (("S1", 1),), (NoteRef("N", 1),)), registry
    )
    proof_two = build_proof_sheet(
        BlockInput("B2", (("S2", 1),), (second_ref,)), registry
    )
    return [
        BlockSpec("B1", body_lines=2, proof=proof_one),
        BlockSpec("B2", body_lines=2, proof=proof_two),
    ]


def run_annotation_demo() -> dict[str, Any]:
    report: dict[str, Any] = {"ok": True, "scenarios": []}

    def record(name: str, payload: dict[str, Any]) -> None:
        report["scenarios"].append({"name": name, **payload})

    # -- scenario 1: shared N, one page -> footnote, cost 0 ---------------
    registry = _basic_registry()
    blocks = _blocks_for(registry, NoteRef("N", 1))
    # Both proofs read exactly one note; shared edition deduped per sheet.
    assert all(block.proof.note_count == 1 for block in blocks)
    one_page = typeset(blocks, registry, page_height=5, max_body_pages=1)
    record(
        "shared_note_one_page",
        {
            "body_pages": one_page.body_page_count,
            "lookup_cost": one_page.total_lookup_cost,
            "page_block_ids": [list(page.block_ids) for page in one_page.pages],
            "footnotes": [
                f"{r.note_id}@{r.edition}" for r in one_page.footnote_refs
            ],
            "end_notes": [f"{r.note_id}@{r.edition}" for r in one_page.endnote_refs],
            "page_sample": render_page_sample(one_page, registry),
        },
    )
    assert one_page.body_page_count == 1
    assert one_page.total_lookup_cost == 0
    assert one_page.pages[0].footnote_refs == (NoteRef("N", 1),)

    # -- scenario 2a: B2 really marks M; one page -> M end note, cost 1 ----
    registry = _basic_registry()
    blocks = _blocks_for(registry, NoteRef("M", 1))
    forced = typeset(blocks, registry, page_height=5, max_body_pages=1)
    record(
        "m_marker_one_page_forced_end_note",
        {
            "body_pages": forced.body_page_count,
            "lookup_cost": forced.total_lookup_cost,
            "footnotes": [
                f"{r.note_id}@{r.edition}" for r in forced.footnote_refs
            ],
            "end_notes": [
                f"{r.note_id}@{r.edition}" for r in forced.endnote_refs
            ],
            "per_page_cost": [
                page.endnote_cost_on_page for page in forced.pages
            ],
            "page_sample": render_page_sample(forced, registry),
        },
    )
    assert forced.total_lookup_cost == 1
    assert NoteRef("M", 1) in forced.endnote_refs
    assert NoteRef("N", 1) in forced.footnote_refs

    # -- scenario 2b: fresh proof sheets, two-page cap -> cost 0 ----------
    registry = _basic_registry()
    blocks = _blocks_for(registry, NoteRef("M", 1))
    spread = typeset(blocks, registry, page_height=5, max_body_pages=2)
    record(
        "m_marker_two_pages_split",
        {
            "body_pages": spread.body_page_count,
            "lookup_cost": spread.total_lookup_cost,
            "page_block_ids": [list(page.block_ids) for page in spread.pages],
            "footnotes": [
                f"{r.note_id}@{r.edition}" for r in spread.footnote_refs
            ],
            "end_notes": [
                f"{r.note_id}@{r.edition}" for r in spread.endnote_refs
            ],
            "page_sample": render_page_sample(spread, registry),
        },
    )
    assert spread.body_page_count == 2
    assert spread.total_lookup_cost == 0

    # -- scenario 3: fork-then-merge dependency graph ---------------------
    fork_registry = NoteRegistry()
    # C is the merge target; A and B both depend on it, root depends on A, B.
    fork_registry.sign_note("C", "shared merge note", lines=1)
    fork_registry.sign_note(
        "A", "fork arm A", lines=1, depends_on=(NoteRef("C", 1),)
    )
    fork_registry.sign_note(
        "B", "fork arm B", lines=1, depends_on=(NoteRef("C", 1),)
    )
    fork_registry.sign_note(
        "R",
        "root note",
        lines=1,
        depends_on=(NoteRef("A", 1), NoteRef("B", 1)),
    )
    fork_registry.register_segment_markers(
        SegmentNoteMarkers(
            segment_id="S9",
            revision=1,
            markers=(NoteRef("R", 1),),
            mandatory=(NoteRef("R", 1),),
        )
    )
    proof = build_proof_sheet(
        BlockInput("B9", (("S9", 1),), (NoteRef("R", 1),)), fork_registry
    )
    c_reading = proof.reading("C", 1)
    record(
        "fork_merge_dependencies",
        {
            "read_once_notes": [
                f"{r.note_id}@{r.edition}" for r in proof.refs()
            ],
            "note_count": proof.note_count,
            "c_paths": [
                [f"{r.note_id}@{r.edition}" for r in path]
                for path in c_reading.paths
            ],
            "c_sources": [list(source) for source in c_reading.sources],
        },
    )
    assert proof.note_count == 4  # R, A, B, C — C merged, read once
    assert len(c_reading.paths) == 2  # R->A->C and R->B->C

    laid_out = typeset(
        [BlockSpec("B9", body_lines=2, proof=proof)],
        fork_registry,
        page_height=6,
        max_body_pages=1,
    )
    record(
        "fork_merge_page_sample",
        {"page_sample": render_page_sample(laid_out, fork_registry)},
    )
    assert laid_out.total_lookup_cost == 0
    assert len(laid_out.pages[0].footnote_refs) == 4

    # -- rejection modes do not invalidate already signed editions --------
    rejections: dict[str, str] = {}
    try:
        build_proof_sheet(
            BlockInput("X", (("S9", 1),), (NoteRef("GHOST", 1),)), fork_registry
        )
    except DanglingNoteRef as exc:
        rejections["dangling"] = str(exc)
    cyclic = NoteRegistry()
    cyclic.sign_note("X", "x", 1, depends_on=(NoteRef("Y", 1),))
    cyclic.sign_note("Y", "y", 1, depends_on=(NoteRef("X", 1),))
    try:
        cyclic.expand((NoteRef("X", 1),))
    except NoteDependencyCycle as exc:
        rejections["cycle"] = str(exc)
    mixed_registry = NoteRegistry()
    mixed_registry.sign_note("K", "ed1", 1, edition=1)
    mixed_registry.sign_note("K", "ed2", 1, edition=2)
    mixed_registry.register_segment_markers(
        SegmentNoteMarkers(
            "S10", 1, (NoteRef("K", 1), NoteRef("K", 2)), ()
        )
    )
    try:
        build_proof_sheet(
            BlockInput(
                "B10",
                (("S10", 1),),
                (NoteRef("K", 1), NoteRef("K", 2)),
            ),
            mixed_registry,
        )
    except IncompatibleNoteEdition as exc:
        rejections["incompatible_editions"] = str(exc)
    missing_registry = NoteRegistry()
    missing_registry.sign_note("Q", "q", 1)
    missing_registry.register_segment_markers(
        SegmentNoteMarkers(
            "S11",
            1,
            markers=(NoteRef("Q", 1),),
            mandatory=(NoteRef("Q", 1),),
        )
    )
    try:
        build_proof_sheet(BlockInput("B11", (("S11", 1),), ()), missing_registry)
    except MissingMandatoryNote as exc:
        rejections["missing_mandatory"] = str(exc)
    record("rejection_modes", {"rejections": rejections})
    assert set(rejections) == {
        "dangling",
        "cycle",
        "incompatible_editions",
        "missing_mandatory",
    }
    # The signed edition stays readable after a rejection.
    assert fork_registry.resolve(NoteRef("R", 1)).note_id == "R"
    return report
