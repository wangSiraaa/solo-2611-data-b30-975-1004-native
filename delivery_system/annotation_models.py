"""Immutable value objects for editorial notes, proof sheets and pagination.

Identity model, mirroring the segment side of the system:

* a note *id* is the stable editorial identity;
* an *edition* is an immutable registered version that other definitions may
  keep referencing after a newer edition is signed;
* a :class:`NoteRef` therefore always pins ``(note_id, edition)`` exactly.

Markers live on frozen segment revisions.  A delivery block covers a fixed set
of those revisions, so the set of legal markers and the mandatory markers are
both derived from what the block actually covers — never from its position.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ----------------------------------------------------------------------
# registry side
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class NoteRef:
    """A pointer to one exact edition of a note definition."""

    note_id: str
    edition: int

    @property
    def key(self) -> tuple[str, int]:
        return (self.note_id, self.edition)

    def __lt__(self, other: "NoteRef") -> bool:
        if not isinstance(other, NoteRef):
            return NotImplemented
        return self.key < other.key


@dataclass(frozen=True)
class NoteDefinition:
    """One signed edition of an editorial note.

    ``lines`` is the typeset height of the note text itself; appendix length is
    unlimited, but a footnote placement consumes exactly this many lines on
    every body page that needs the note.
    """

    note_id: str
    edition: int
    text: str
    lines: int
    depends_on: tuple[NoteRef, ...] = ()
    lookup_cost: int = 1

    @property
    def ref(self) -> NoteRef:
        return NoteRef(self.note_id, self.edition)


@dataclass(frozen=True)
class SegmentNoteMarkers:
    """The marker contract attached to one frozen segment revision."""

    segment_id: str
    revision: int
    markers: tuple[NoteRef, ...] = ()
    mandatory: tuple[NoteRef, ...] = ()

    @property
    def key(self) -> tuple[str, int]:
        return (self.segment_id, self.revision)


# ----------------------------------------------------------------------
# proof sheet side
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class MarkerCheck:
    """One marker printed by a block, with the segment revision it came from."""

    ref: NoteRef
    from_segment_id: str
    from_revision: int


@dataclass(frozen=True)
class NoteReading:
    """One note a proofreader actually has to read, with dependency provenance.

    A note reached through several paths (shared dependency, repeated markers)
    appears exactly once; ``paths`` records every expansion route so the
    "read once" decision stays auditable.
    """

    ref: NoteRef
    lines: int
    lookup_cost: int
    direct: bool
    paths: tuple[tuple[NoteRef, ...], ...]
    sources: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class ProofSheet:
    """The independent proof sheet for one selected delivery block."""

    block_id: str
    covered_segments: tuple[tuple[str, int], ...]
    direct_markers: tuple[MarkerCheck, ...]
    readings: tuple[NoteReading, ...]
    note_count: int

    def reading(self, note_id: str, edition: int) -> NoteReading:
        for reading in self.readings:
            if reading.ref.note_id == note_id and reading.ref.edition == edition:
                return reading
        raise KeyError((note_id, edition))

    def refs(self) -> tuple[NoteRef, ...]:
        return tuple(reading.ref for reading in self.readings)


# ----------------------------------------------------------------------
# typesetting side
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class BlockSpec:
    """A delivery block as handed to the typesetter."""

    block_id: str
    body_lines: int
    proof: ProofSheet

    @property
    def note_mask_refs(self) -> tuple[NoteRef, ...]:
        return self.proof.refs()


@dataclass(frozen=True)
class PageLayout:
    """One body page in the final page sample."""

    page_no: int
    block_ids: tuple[str, ...]
    body_lines_used: int
    footnote_refs: tuple[NoteRef, ...]
    footnote_lines: int
    endnote_refs: tuple[NoteRef, ...]
    endnote_cost_on_page: int

    @property
    def total_lines(self) -> int:
        return self.body_lines_used + self.footnote_lines


@dataclass(frozen=True)
class TypesetResult:
    """The optimal pagination + placement decision and its concrete pages."""

    page_height: int
    max_body_pages: int
    pages: tuple[PageLayout, ...]
    footnote_refs: tuple[NoteRef, ...]
    endnote_refs: tuple[NoteRef, ...]
    appendix_entries: tuple[NoteDefinition, ...]
    total_lookup_cost: int
    body_page_count: int
    candidate_paginations_considered: int = field(default=0)
