"""Editorial note registry, proof-sheet builder and pagination solver.

Two stages, matching the editorial workflow:

1. *Signing / proofing* (:class:`NoteRegistry`, :func:`build_proof_sheet`).
   Note editions are immutable and keep existing references valid forever.
   A block's markers must come from segment revisions the block really covers,
   mandatory markers cannot be dropped, every expansion must resolve, and one
   block may never mix incompatible editions of one note id.  A rejection
   never invalidates already signed editions — other blocks keep reading them.

2. *Typesetting* (:func:`typeset`).  Blocks keep package order, never split,
   and are packed into fixed-height pages.  Each note gets one book-wide
   destination: footnote (re-typeset for free on every needing page, but
   consuming page lines there) or end note (typeset once in the unlimited
   appendix, but paying its lookup cost on every body page that meets it).
   The solver is exact: minimum total lookup cost, then minimum body pages.
"""

from __future__ import annotations

from dataclasses import dataclass

from .annotation_models import (
    BlockSpec,
    MarkerCheck,
    NoteDefinition,
    NoteReading,
    NoteRef,
    PageLayout,
    ProofSheet,
    SegmentNoteMarkers,
    TypesetResult,
)
from .errors import (
    DanglingNoteRef,
    IncompatibleNoteEdition,
    InfeasibleLayout,
    MissingMandatoryNote,
    NoteDependencyCycle,
)


# ----------------------------------------------------------------------
# registry
# ----------------------------------------------------------------------


class NoteRegistry:
    """Signed note editions and the marker contracts of segment revisions."""

    def __init__(self) -> None:
        self._definitions: dict[tuple[str, int], NoteDefinition] = {}
        self._editions: dict[str, list[int]] = {}
        self._segments: dict[tuple[str, int], SegmentNoteMarkers] = {}

    # -- notes -----------------------------------------------------------

    def sign_note(
        self,
        note_id: str,
        text: str,
        lines: int,
        *,
        edition: int | None = None,
        depends_on: tuple[NoteRef, ...] = (),
        lookup_cost: int = 1,
    ) -> NoteDefinition:
        """Sign one immutable edition; older editions stay readable."""

        if lines < 0:
            raise ValueError("note lines must be non-negative")
        existing = self._editions.setdefault(note_id, [])
        if edition is None:
            edition = (max(existing) + 1) if existing else 1
        if edition in existing:
            raise ValueError(f"note {note_id!r} edition {edition} is already signed")
        definition = NoteDefinition(
            note_id=note_id,
            edition=edition,
            text=text,
            lines=lines,
            depends_on=tuple(depends_on),
            lookup_cost=lookup_cost,
        )
        self._definitions[(note_id, edition)] = definition
        existing.append(edition)
        existing.sort()
        return definition

    def resolve(self, ref: NoteRef) -> NoteDefinition:
        try:
            return self._definitions[ref.key]
        except KeyError as exc:
            raise DanglingNoteRef(
                f"no signed edition {ref.edition} of note {ref.note_id!r}"
            ) from exc

    def editions(self, note_id: str) -> tuple[int, ...]:
        return tuple(self._editions.get(note_id, ()))

    def all_definitions(self) -> tuple[NoteDefinition, ...]:
        return tuple(
            self._definitions[key]
            for key in sorted(self._definitions, key=lambda k: (k[0], k[1]))
        )

    # -- segment marker contracts ---------------------------------------

    def register_segment_markers(self, contract: SegmentNoteMarkers) -> None:
        # Validate immediately against the signed registry: a dangling
        # contract would make every covering block unsignable later anyway.
        for ref in (*contract.markers, *contract.mandatory):
            self.resolve(ref)
        marker_set = set(contract.markers)
        extra = [ref for ref in contract.mandatory if ref not in marker_set]
        if extra:
            raise MissingMandatoryNote(
                f"mandatory refs must be included in markers for "
                f"{contract.segment_id}@{contract.revision}: {extra}"
            )
        self._segments[contract.key] = contract

    def segment_contract(self, segment_id: str, revision: int) -> SegmentNoteMarkers:
        try:
            return self._segments[(segment_id, revision)]
        except KeyError as exc:
            raise DanglingNoteRef(
                f"no marker contract registered for {segment_id}@{revision}"
            ) from exc

    # -- expansion -------------------------------------------------------

    def expand(
        self,
        direct_refs: tuple[NoteRef, ...],
        sources_by_ref: dict[NoteRef, set[tuple[str, int]]] | None = None,
    ) -> dict[NoteRef, NoteReading]:
        """Fully expand dependencies; each exact edition is read once.

        Rejects dangling references and dependency cycles.  Two different
        editions of the same note id are distinct readings at this stage;
        block-level incompatibility is checked separately in
        :func:`build_proof_sheet`.  ``sources_by_ref`` maps each direct marker
        to the covered segment revisions carrying it, for proof provenance.
        """

        source_map = sources_by_ref or {}
        readings: dict[NoteRef, dict[str, object]] = {}

        def walk(
            ref: NoteRef,
            path: tuple[NoteRef, ...],
            sources: frozenset[tuple[str, int]],
        ) -> None:
            definition = self.resolve(ref)  # raises DanglingNoteRef
            if ref in path:
                chain = " -> ".join(
                    f"{r.note_id}@{r.edition}" for r in (*path, ref)
                )
                raise NoteDependencyCycle(f"note dependency cycle: {chain}")
            next_path = (*path, ref)
            entry = readings.get(ref)
            if entry is None:
                entry = {
                    "definition": definition,
                    "direct": False,
                    "paths": [],
                    "sources": set(),
                }
                readings[ref] = entry
            if next_path not in entry["paths"]:
                entry["paths"].append(next_path)
            entry["sources"].update(sources)
            for dep in definition.depends_on:
                walk(dep, next_path, sources)

        for ref in direct_refs:
            walk(ref, (), frozenset(source_map.get(ref, set())))
            readings[ref]["direct"] = True

        result: dict[NoteRef, NoteReading] = {}
        for ref in sorted(readings, key=lambda r: (r.note_id, r.edition)):
            entry = readings[ref]
            definition: NoteDefinition = entry["definition"]  # type: ignore[assignment]
            result[ref] = NoteReading(
                ref=ref,
                lines=definition.lines,
                lookup_cost=definition.lookup_cost,
                direct=bool(entry["direct"]),
                paths=tuple(sorted(entry["paths"])),  # type: ignore[arg-type]
                sources=tuple(sorted(entry["sources"])),  # type: ignore[arg-type]
            )
        return result


# ----------------------------------------------------------------------
# proof sheet
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class BlockInput:
    """A selected block and the markers it actually prints."""

    block_id: str
    covered_segments: tuple[tuple[str, int], ...]
    markers: tuple[NoteRef, ...] = ()


def build_proof_sheet(block: BlockInput, registry: NoteRegistry) -> ProofSheet:
    """Validate one block's markers and produce its independent proof sheet.

    Checks, in order:

    * every covered segment revision has a registered marker contract;
    * every printed marker is legal for a segment revision the block covers;
    * every mandatory marker of a covered revision is printed;
    * the block does not print two editions of one note id;
    * every reference (and transitive dependency) resolves and is acyclic.
    """

    # What the block is allowed to print, and what it must print.
    legal: dict[NoteRef, list[tuple[str, int]]] = {}
    mandatory: set[NoteRef] = set()
    for segment_id, revision in block.covered_segments:
        contract = registry.segment_contract(segment_id, revision)
        for ref in contract.markers:
            legal.setdefault(ref, []).append((segment_id, revision))
        mandatory.update(contract.mandatory)

    # Edition consistency: one block, one edition per note id.
    edition_by_id: dict[str, int] = {}
    marker_checks: list[MarkerCheck] = []
    for ref in block.markers:
        if ref.note_id in edition_by_id and edition_by_id[ref.note_id] != ref.edition:
            raise IncompatibleNoteEdition(
                f"block {block.block_id!r} references note {ref.note_id!r} at "
                f"incompatible editions {edition_by_id[ref.note_id]} and {ref.edition}"
            )
        if ref.note_id not in edition_by_id:
            edition_by_id[ref.note_id] = ref.edition
        origins = legal.get(ref)
        if not origins:
            raise DanglingNoteRef(
                f"block {block.block_id!r} prints marker {ref.note_id}@{ref.edition} "
                "that no covered segment revision is allowed to carry"
            )
        # Record one check per covered segment revision carrying this marker;
        # repeated markers over different segments stay distinct evidence.
        for segment_id, seg_revision in origins:
            marker_checks.append(
                MarkerCheck(
                    ref=ref,
                    from_segment_id=segment_id,
                    from_revision=seg_revision,
                )
            )

    missing = sorted(
        mandatory - set(block.markers), key=lambda r: (r.note_id, r.edition)
    )
    if missing:
        raise MissingMandatoryNote(
            f"block {block.block_id!r} omits mandatory marker(s): "
            + ", ".join(f"{r.note_id}@{r.edition}" for r in missing)
        )

    # Every covered segment revision that legally carries a direct marker is
    # recorded as that reading's provenance, then propagated through deps.
    sources_by_ref: dict[NoteRef, set[tuple[str, int]]] = {}
    for check in marker_checks:
        sources_by_ref.setdefault(check.ref, set()).add(
            (check.from_segment_id, check.from_revision)
        )
    # Keep duplicate direct markers in the expansion so every print site gets
    # its own recorded path; readings themselves are still deduped by edition.
    readings = registry.expand(tuple(block.markers), sources_by_ref)

    final_readings = tuple(readings.values())
    return ProofSheet(
        block_id=block.block_id,
        covered_segments=tuple(block.covered_segments),
        direct_markers=tuple(
            sorted(marker_checks, key=lambda c: (c.ref.note_id, c.ref.edition))
        ),
        readings=final_readings,
        note_count=len(final_readings),
    )


# ----------------------------------------------------------------------
# pagination: enumerate every non-dominated packing
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Packing:
    pages: tuple[tuple[int, ...], ...]
    note_masks: tuple[int, ...]
    body_lines: tuple[int, ...]


@dataclass(frozen=True)
class _Node:
    """One bounded-DP node after placing blocks ``0..layer-1``.

    The identity key is ``(page_count, open_page, open_body, open_mask)``.
    Including the open page's block tuple keeps reconstruction exact: a node's
    parent chain always reproduces the same concrete pages.  The page cap bounds
    the number of boundary combinations, so the frontier stays small (a few
    hundred states at 200 blocks / 50 pages) rather than growing over the
    exponential number of unrestricted packings.
    """

    node_id: int
    layer: int
    page_count: int
    body: int
    mask: int
    open_page: tuple[int, ...]
    parent_id: int | None
    closed_page: tuple[int, ...] | None
    closed_mask: int
    closed_body: int


def _enumerate_packings(
    blocks: list[BlockSpec],
    page_height: int,
    bit_of: dict[NoteRef, int],
    max_pages: int,
) -> list[_Packing]:
    """Enumerate distinct feasible packings within ``max_pages`` pages.

    At every block we either append it to the open page (if it fits) or close
    the page and open a new one; taking more pages on purpose is allowed
    because separating blocks can remove end-note cost.  Every relevant
    whitespace level is retained (these are the layouts that let costly notes
    stay as footnotes), while the page cap keeps the state space bounded.
    """

    n = len(blocks)
    heights = [block.body_lines for block in blocks]
    masks = [_block_mask(block, bit_of) for block in blocks]

    # Root: no page exists yet, so its empty open page is never closed into an
    # empty page; the first block opens page 1.
    nodes: list[_Node] = [_Node(0, 0, 0, 0, 0, (), None, None, 0, 0)]
    by_id = {0: nodes[0]}
    # key (page_count, open_page, open_body, open_mask) -> representative id.
    frontier: dict[tuple[int, tuple[int, ...], int, int], int] = {
        (0, (), 0, 0): 0
    }

    for i in range(n):
        next_frontier: dict[tuple[int, tuple[int, ...], int, int], int] = {}

        def admit(
            page_count: int,
            body: int,
            mask: int,
            open_page: tuple[int, ...],
            parent_id: int,
            closed_page: tuple[int, ...] | None,
            closed_mask: int,
            closed_body: int,
        ) -> None:
            key = (page_count, open_page, body, mask)
            if key in next_frontier:
                return  # identical continuation state already represented
            node = _Node(
                len(nodes),
                i + 1,
                page_count,
                body,
                mask,
                open_page,
                parent_id,
                closed_page,
                closed_mask,
                closed_body,
            )
            nodes.append(node)
            by_id[node.node_id] = node
            next_frontier[key] = node.node_id

        for parent_id in frontier.values():
            parent = by_id[parent_id]
            if heights[i] <= page_height - parent.body:
                # Append to the open page; on the empty root this opens page 1.
                admit(
                    max(parent.page_count, 1),
                    parent.body + heights[i],
                    parent.mask | masks[i],
                    (*parent.open_page, i),
                    parent_id,
                    None,
                    0,
                    0,
                )
            # Close a non-empty open page and start a fresh page with block i.
            if parent.open_page and parent.page_count + 1 <= max_pages:
                admit(
                    parent.page_count + 1,
                    heights[i],
                    masks[i],
                    (i,),
                    parent_id,
                    parent.open_page,
                    parent.mask,
                    parent.body,
                )
        frontier = next_frontier

    packings: list[_Packing] = []
    for node_id in frontier.values():
        pages_rev: list[tuple[int, ...]] = []
        masks_rev: list[int] = []
        body_rev: list[int] = []
        cursor: _Node | None = by_id[node_id]
        # Closed pages are collected most-recent-first; the terminal open page
        # is chronologically last, appended after reversing the closed ones.
        while cursor is not None and cursor.layer > 0:
            if cursor.closed_page is not None:
                pages_rev.append(cursor.closed_page)
                masks_rev.append(cursor.closed_mask)
                body_rev.append(cursor.closed_body)
            cursor = (
                None if cursor.parent_id is None else by_id[cursor.parent_id]
            )
        terminal = by_id[node_id]
        pages = tuple(reversed(pages_rev)) + (terminal.open_page,)
        page_masks = tuple(reversed(masks_rev)) + (terminal.mask,)
        page_body = tuple(reversed(body_rev)) + (terminal.body,)
        flat = [idx for page in pages for idx in page]
        assert flat == list(range(n))
        assert len(pages) <= max_pages
        packings.append(
            _Packing(pages=pages, note_masks=page_masks, body_lines=page_body)
        )
    return _dedup_packings(packings)


def _block_mask(block: BlockSpec, bit_of: dict[NoteRef, int]) -> int:
    mask = 0
    for ref in block.note_mask_refs:
        mask |= 1 << bit_of[ref]
    return mask


def _dedup_packings(packings: list[_Packing]) -> list[_Packing]:
    """De-duplicate identical packings and order them deterministically.

    No whole-packing dominance pruning is done: a packing with more whitespace
    per page is not dominated for our objective, because that whitespace is
    exactly what lets a costly note stay as a footnote instead of moving to the
    appendix.  The bounded-state DP already keeps the frontier compact.
    """

    unique = list({(p.pages, p.note_masks, p.body_lines) for p in packings})
    unique.sort(
        key=lambda p: (
            len(p[0]),
            tuple(idx for page in p[0] for idx in page),
            tuple(p[1]),
        )
    )
    return [_Packing(pages=p, note_masks=m, body_lines=b) for p, m, b in unique]


# ----------------------------------------------------------------------
# placement: one book-wide destination per note, exact minimum lookup cost
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Placement:
    footnote_mask: int
    endnote_mask: int
    cost: int


def _union(masks: tuple[int, ...]) -> int:
    total = 0
    for mask in masks:
        total |= mask
    return total


def _solve_placement(
    packing: _Packing,
    page_height: int,
    note_lines: list[int],
    bit_cost: list[int],
) -> _Placement | None:
    """Choose footnote/end-note destinations for one fixed pagination.

    A footnote note must fit on every page that meets it alongside that page's
    body lines.  End notes never constrain page height (the appendix is
    unlimited).  Search is branch-and-bound over note bits, seeded with the
    all-end-note baseline, and is exact for the minimum total lookup cost.
    """

    note_count = len(note_lines)
    page_count = len(packing.pages)
    meets = [0] * note_count  # bit p set -> note meets page p
    for page_no, mask in enumerate(packing.note_masks):
        for bit in range(note_count):
            if mask & (1 << bit):
                meets[bit] |= 1 << page_no

    distinct_bits = sorted(
        (
            bit
            for bit in range(note_count)
            if any(mask & (1 << bit) for mask in packing.note_masks)
        ),
        key=lambda b: (-bit_cost[b], note_lines[b], b),
    )

    best = _Placement(
        footnote_mask=0,
        endnote_mask=_union(packing.note_masks),
        cost=sum(
            bit_cost[bit] * meets[bit].bit_count() for bit in distinct_bits
        ),
    )

    def fits(footnote_mask: int) -> bool:
        for page_no in range(page_count):
            used = packing.body_lines[page_no]
            bits = packing.note_masks[page_no] & footnote_mask
            bit = 0
            while bits:
                if bits & 1:
                    used += note_lines[bit]
                    if used > page_height:
                        return False
                bits >>= 1
                bit += 1
        return True

    def search(index: int, footnote_mask: int, end_mask: int, cost: int) -> None:
        nonlocal best
        if cost >= best.cost:
            return
        if index == len(distinct_bits):
            if fits(footnote_mask):
                best = _Placement(
                    footnote_mask=footnote_mask, endnote_mask=end_mask, cost=cost
                )
            return
        bit = distinct_bits[index]
        # Try footnote first: zero lookup cost, only page capacity is at stake.
        candidate = footnote_mask | (1 << bit)
        if fits(candidate):
            search(index + 1, candidate, end_mask, cost)
        # Otherwise end note: pays its lookup cost once per meeting page.
        search(
            index + 1,
            footnote_mask,
            end_mask | (1 << bit),
            cost + bit_cost[bit] * meets[bit].bit_count(),
        )

    search(0, 0, 0, 0)
    if not fits(best.footnote_mask):
        return None
    return best


# ----------------------------------------------------------------------
# public typesetting entry point
# ----------------------------------------------------------------------


def typeset(
    blocks: list[BlockSpec],
    registry: NoteRegistry,
    *,
    page_height: int,
    max_body_pages: int,
) -> TypesetResult:
    """Paginate ordered blocks and assign each note one book-wide destination.

    Objective order: (1) minimum total reader lookup cost, (2) minimum body
    page count among cost-optimal solutions.  Raises :class:`InfeasibleLayout`
    when even all-end-note pagination cannot keep every block whole within
    ``max_body_pages`` pages of ``page_height`` lines.
    """

    if not blocks:
        raise InfeasibleLayout("no blocks to typeset")
    for block in blocks:
        if block.body_lines > page_height:
            raise InfeasibleLayout(
                f"block {block.block_id!r} needs {block.body_lines} lines but a "
                f"page holds only {page_height}; blocks cannot be split"
            )

    # Stable bit ordering across every packing: by note id, then edition.
    all_refs = sorted(
        {ref for block in blocks for ref in block.note_mask_refs},
        key=lambda r: (r.note_id, r.edition),
    )
    bit_of = {ref: bit for bit, ref in enumerate(all_refs)}
    note_lines = [registry.resolve(ref).lines for ref in all_refs]
    bit_cost = [registry.resolve(ref).lookup_cost for ref in all_refs]

    packings = _enumerate_packings(
        blocks, page_height, bit_of, max_body_pages
    )
    if not packings:
        raise InfeasibleLayout(
            f"no pagination fits {len(blocks)} unsplittable blocks into "
            f"{max_body_pages} page(s) of {page_height} lines"
        )

    solutions: list[tuple[_Packing, _Placement]] = []
    for packing in packings:
        placement = _solve_placement(packing, page_height, note_lines, bit_cost)
        if placement is not None:
            solutions.append((packing, placement))
    if not solutions:
        raise InfeasibleLayout("every feasible pagination failed note placement")

    # Primary: lowest lookup cost.  Secondary: fewest body pages.  Tertiary:
    # deterministic lexicographic choice for reproducible page samples.
    solutions.sort(
        key=lambda pair: (
            pair[1].cost,
            len(pair[0].pages),
            tuple(pair[0].pages),
            pair[1].footnote_mask,
        )
    )
    packing, placement = solutions[0]

    pages: list[PageLayout] = []
    for page_no, page_blocks in enumerate(packing.pages, start=1):
        page_mask = packing.note_masks[page_no - 1]
        footnote_refs = tuple(
            all_refs[bit]
            for bit in range(len(all_refs))
            if page_mask & placement.footnote_mask & (1 << bit)
        )
        endnote_refs = tuple(
            all_refs[bit]
            for bit in range(len(all_refs))
            if page_mask & placement.endnote_mask & (1 << bit)
        )
        footnote_lines = sum(
            note_lines[bit]
            for bit in range(len(all_refs))
            if page_mask & placement.footnote_mask & (1 << bit)
        )
        endnote_cost = sum(
            bit_cost[bit]
            for bit in range(len(all_refs))
            if page_mask & placement.endnote_mask & (1 << bit)
        )
        pages.append(
            PageLayout(
                page_no=page_no,
                block_ids=tuple(blocks[idx].block_id for idx in page_blocks),
                body_lines_used=packing.body_lines[page_no - 1],
                footnote_refs=footnote_refs,
                footnote_lines=footnote_lines,
                endnote_refs=endnote_refs,
                endnote_cost_on_page=endnote_cost,
            )
        )

    footnote_refs = tuple(
        all_refs[bit]
        for bit in range(len(all_refs))
        if placement.footnote_mask & (1 << bit)
    )
    endnote_refs = tuple(
        all_refs[bit]
        for bit in range(len(all_refs))
        if placement.endnote_mask & (1 << bit)
    )
    appendix = tuple(registry.resolve(ref) for ref in endnote_refs)
    return TypesetResult(
        page_height=page_height,
        max_body_pages=max_body_pages,
        pages=tuple(pages),
        footnote_refs=footnote_refs,
        endnote_refs=endnote_refs,
        appendix_entries=appendix,
        total_lookup_cost=placement.cost,
        body_page_count=len(pages),
        candidate_paginations_considered=len(packings),
    )


def render_page_sample(result: TypesetResult, registry: NoteRegistry) -> str:
    """Human-readable concrete page sample, one section per body page."""

    lines = [
        f"page_height={result.page_height} max_body_pages={result.max_body_pages} "
        f"body_pages={result.body_page_count} total_lookup_cost="
        f"{result.total_lookup_cost}"
    ]
    for page in result.pages:
        lines.append(f"--- page {page.page_no} ---")
        lines.append(
            f"body {page.body_lines_used}/{result.page_height} lines; "
            f"blocks: {', '.join(page.block_ids)}"
        )
        if page.footnote_refs:
            lines.append("footnotes (printed here, no lookup cost):")
            for ref in page.footnote_refs:
                definition = registry.resolve(ref)
                lines.append(
                    f"  [{ref.note_id}@{ref.edition}] {definition.lines} line(s): "
                    f"{definition.text}"
                )
        if page.endnote_refs:
            desc = ", ".join(
                f"{ref.note_id}@{ref.edition}" for ref in page.endnote_refs
            )
            lines.append(
                f"end-note markers -> appendix (lookup cost on this page: "
                f"{page.endnote_cost_on_page}): {desc}"
            )
        lines.append(f"page total {page.total_lines}/{result.page_height} lines")
    if result.appendix_entries:
        lines.append("--- appendix (unlimited length, each end note typeset once) ---")
        for definition in result.appendix_entries:
            lines.append(
                f"[{definition.note_id}@{definition.edition}] "
                f"{definition.lines} line(s), lookup {definition.lookup_cost}: "
                f"{definition.text}"
            )
    return "\n".join(lines)
