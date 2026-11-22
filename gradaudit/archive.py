"""Paper-archive (纸档) reconstruction layer on top of ``gradaudit``.

The existing library owns the *truth*: transfer records (real points, external
course) and the explicit course equivalences.  This module models the **paper**
side that proves those records exist on paper:

* ``boxes``   -- physical archive boxes, each with a storage cost.  A box is an
  indivisible physical location; the custodian keeps or drops it whole.
* ``copies``  -- verified physical copies of a *named page*.  A copy sits in
  exactly one box and is usable only while it is ``accepted`` and not
  ``voided``.  Several copies of one page may exist (in different boxes).
* ``seal rules`` -- per transfer record, the original school's seal rule: a
  list of *alternative complete evidence sets*.  Every named page listed in a
  set is indispensable (缺一不可); different records (and different students)
  may reference the same named page.
* ``operations`` -- the archive station's own edits before custody: boxing
  (入盒), transfer (调拨), voiding (作废), duplicate-page-number variant
  conflict resolution (重名页号异文冲突), and batch sealing (整批签封).

Given any set of still-readable boxes this module returns the *actually
reconstructable* records together with a page-by-page evidence chain.  A page
with two usable copies still reconstructs the same record only once: a record
is the unit of reconstruction, and each page contributes a single witness copy.

Nothing here decides which boxes to keep -- that is :mod:`gradaudit.custody`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .errors import BatchAtomicityError, SchemaError

ARCHIVE_FORMAT = "paper-archive/1"
MAX_BOXES = 16


def _require(cond: bool, message: str, *, location: Optional[str] = None) -> None:
    if not cond:
        raise SchemaError(message, location=location)


def _as_nonneg_int(value, *, field_name: str, location: str) -> int:
    _require(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0,
        f"{field_name} must be a non-negative integer",
        location=location,
    )
    return value


def _as_bool(value, *, field_name: str, location: str, default: bool) -> bool:
    if value is None:
        return default
    _require(isinstance(value, bool), f"{field_name} must be a boolean", location=location)
    return value


# --------------------------------------------------------------------------- #
# immutable value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Box:
    box_id: str
    cost: int


@dataclass(frozen=True)
class Copy:
    copy_id: str
    page_id: str
    box_id: Optional[str]
    accepted: bool = True
    voided: bool = False
    variant: str = ""
    sealed: bool = False


@dataclass(frozen=True)
class EvidencePage:
    """One concrete copy backing one named page of an evidence set."""

    page_id: str
    copy_id: str
    box_id: str
    variant: str

    def as_dict(self) -> dict:
        return {
            "page_id": self.page_id,
            "copy_id": self.copy_id,
            "box_id": self.box_id,
            "variant": self.variant,
        }


@dataclass(frozen=True)
class RecordEvidence:
    """A reconstructed transfer record and the concrete pages that prove it."""

    student_id: str
    record_id: str
    seal_set: Tuple[str, ...]
    pages: Tuple[EvidencePage, ...]

    def as_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "seal_set": list(self.seal_set),
            "pages": [p.as_dict() for p in self.pages],
        }


# --------------------------------------------------------------------------- #
# archive
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Archive:
    revision: int
    boxes: Mapping[str, Box]
    copies: Mapping[str, Copy]
    resolutions: Mapping[str, str]
    seal_rules: Mapping[Tuple[str, str], Tuple[Tuple[str, ...], ...]]
    events: Tuple[dict, ...] = ()

    # -- basic accessors ---------------------------------------------------
    def box_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self.boxes))

    def student_ids(self) -> Tuple[str, ...]:
        return tuple(sorted({sid for (sid, _rid) in self.seal_rules}))

    def records_for_student(self, student_id: str) -> Tuple[str, ...]:
        return tuple(sorted(rid for (sid, rid) in self.seal_rules if sid == student_id))

    def rule_sets(self, student_id: str, record_id: str) -> Tuple[Tuple[str, ...], ...]:
        return self.seal_rules.get((student_id, record_id), ())

    def all_pages(self) -> Tuple[str, ...]:
        return tuple(sorted({c.page_id for c in self.copies.values()}))

    # -- variant-conflict handling ----------------------------------------
    def page_variants(self) -> Dict[str, FrozenSet[str]]:
        """Distinct variants of each *usable-grade* page copy.

        ``(page number, variant text)``: two boxed, accepted, non-voided copies
        of one named page with different variant text are a duplicate-page-number
        variant conflict (重名页号异文冲突).  Voiding a copy removes it from the
        conflict, which is one legitimate way to resolve the conflict.  Un-accepted
        copies are not "经核对" and therefore never create a conflict.
        """
        out: Dict[str, Set[str]] = {}
        for c in self.copies.values():
            if c.box_id is None or c.voided or not c.accepted:
                continue
            out.setdefault(c.page_id, set()).add(c.variant)
        return {p: frozenset(v) for p, v in out.items()}

    def _variant_usable(self, copy: Copy, variants: Mapping[str, FrozenSet[str]]) -> bool:
        vs = variants.get(copy.page_id, frozenset())
        if len(vs) <= 1:
            return True
        resolved = self.resolutions.get(copy.page_id)
        return resolved is not None and copy.variant == resolved

    # -- usability ---------------------------------------------------------
    def is_copy_usable(self, copy: Copy, variants: Optional[Mapping[str, FrozenSet[str]]] = None) -> bool:
        if copy.box_id is None or not copy.accepted or copy.voided:
            return False
        vs = variants if variants is not None else self.page_variants()
        return self._variant_usable(copy, vs)

    def usable_copies(self, readable_boxes: Iterable[str]) -> List[Copy]:
        boxes = set(readable_boxes)
        variants = self.page_variants()
        usable = [c for c in self.copies.values() if c.box_id in boxes and self.is_copy_usable(c, variants)]
        return sorted(usable, key=lambda c: (c.box_id or "", c.copy_id))

    def available_pages(self, readable_boxes: Iterable[str]) -> FrozenSet[str]:
        return frozenset(c.page_id for c in self.usable_copies(readable_boxes))

    # -- reconstruction ----------------------------------------------------
    def reconstructable_records(
        self, student_id: str, readable_boxes: Iterable[str]
    ) -> List[str]:
        """Record ids of ``student_id`` whose *some* complete seal set is available
        from the usable page copies in ``readable_boxes``."""
        avail = self.available_pages(readable_boxes)
        out: List[str] = []
        for rid in self.records_for_student(student_id):
            sets = self.rule_sets(student_id, rid)
            if any(all(p in avail for p in s) for s in sets):
                out.append(rid)
        return out

    def reconstruct_for_student(
        self, student_id: str, readable_boxes: Iterable[str]
    ) -> List[RecordEvidence]:
        """Full evidence chain for every reconstructable record of a student."""
        readable = set(readable_boxes)
        copies = self.usable_copies(readable)
        by_page: Dict[str, List[Copy]] = {}
        for c in copies:
            by_page.setdefault(c.page_id, []).append(c)
        # usable_copies is already sorted, so the first copy is the witness.
        out: List[RecordEvidence] = []
        for rid in self.records_for_student(student_id):
            witness = self._pick_witness(student_id, rid, by_page)
            if witness is not None:
                out.append(witness)
        return out

    def _pick_witness(
        self, student_id: str, record_id: str, by_page: Mapping[str, List[Copy]]
    ) -> Optional[RecordEvidence]:
        for seal_set in self.rule_sets(student_id, record_id):
            if all(p in by_page for p in seal_set):
                pages: List[EvidencePage] = []
                for pid in seal_set:
                    c = by_page[pid][0]  # deterministic first usable copy
                    pages.append(EvidencePage(page_id=pid, copy_id=c.copy_id,
                                              box_id=c.box_id or "", variant=c.variant))
                return RecordEvidence(
                    student_id=student_id,
                    record_id=record_id,
                    seal_set=tuple(seal_set),
                    pages=tuple(pages),
                )
        return None

    def report(self, readable_boxes: Iterable[str]) -> dict:
        readable = tuple(sorted(set(readable_boxes)))
        unknown = [b for b in readable if b not in self.boxes]
        _require(not unknown, f"unknown box(es) {unknown}", location="readable_boxes")
        students = []
        for sid in self.student_ids():
            ev = self.reconstruct_for_student(sid, readable)
            students.append({
                "student_id": sid,
                "records": [e.as_dict() for e in ev],
            })
        return {
            "format": "archive-report/1",
            "archive_revision": self.revision,
            "readable_boxes": list(readable),
            "students": students,
        }


# --------------------------------------------------------------------------- #
# parsing + archive-station operations
# --------------------------------------------------------------------------- #
def _parse_box(entry: dict, idx: int) -> Box:
    loc = f"boxes[{idx}]"
    _require(isinstance(entry, dict), "box must be an object", location=loc)
    bid = entry.get("box_id")
    _require(isinstance(bid, str) and bid.strip() != "", "box_id must be a non-empty string", location=loc)
    cost = _as_nonneg_int(entry.get("cost"), field_name="cost", location=bid)
    return Box(box_id=bid, cost=cost)


def _parse_copy(entry: dict, idx: int, box_ids: Set[str]) -> Copy:
    loc = f"copies[{idx}]"
    _require(isinstance(entry, dict), "copy must be an object", location=loc)
    cid = entry.get("copy_id")
    _require(isinstance(cid, str) and cid.strip() != "", "copy_id must be a non-empty string", location=loc)
    pid = entry.get("page_id")
    _require(isinstance(pid, str) and pid.strip() != "", f"page_id for {cid!r} must be a non-empty string", location=cid)
    box_id = entry.get("box_id")
    if box_id is not None:
        _require(isinstance(box_id, str) and box_id in box_ids,
                 f"copy {cid!r} references unknown box {box_id!r}", location=cid)
    accepted = _as_bool(entry.get("accepted"), field_name="accepted", location=cid, default=True)
    voided = _as_bool(entry.get("voided"), field_name="voided", location=cid, default=False)
    sealed = _as_bool(entry.get("sealed"), field_name="sealed", location=cid, default=False)
    variant = entry.get("variant", "")
    _require(isinstance(variant, str), f"variant for {cid!r} must be a string", location=cid)
    return Copy(copy_id=cid, page_id=pid, box_id=box_id, accepted=accepted,
                voided=voided, variant=variant, sealed=sealed)


def _parse_seal_rules(raw: object) -> Dict[Tuple[str, str], Tuple[Tuple[str, ...], ...]]:
    _require(isinstance(raw, list), "seal_rules must be a list", location="seal_rules")
    rules: Dict[Tuple[str, str], Tuple[Tuple[str, ...], ...]] = {}
    for idx, entry in enumerate(raw):
        loc = f"seal_rules[{idx}]"
        _require(isinstance(entry, dict), "seal rule must be an object", location=loc)
        sid = entry.get("student_id")
        rid = entry.get("record_id")
        _require(isinstance(sid, str) and sid.strip() != "", "student_id must be a non-empty string", location=loc)
        _require(isinstance(rid, str) and rid.strip() != "", "record_id must be a non-empty string", location=loc)
        key = (sid, rid)
        _require(key not in rules, f"duplicate seal rule for {sid}/{rid}", location=rid)
        raw_sets = entry.get("sets")
        _require(isinstance(raw_sets, list) and raw_sets,
                 f"seal rule for {sid}/{rid} must list at least one alternative evidence set", location=rid)
        sets: List[Tuple[str, ...]] = []
        for s_idx, raw_set in enumerate(raw_sets):
            _require(isinstance(raw_set, list),
                     f"evidence set {s_idx} for {sid}/{rid} must be a list of page ids", location=rid)
            pages: List[str] = []
            for p in raw_set:
                _require(isinstance(p, str) and p.strip() != "",
                         f"page id in {sid}/{rid} must be a non-empty string", location=rid)
                if p not in pages:  # a set is a set; duplicates add nothing
                    pages.append(p)
            sets.append(tuple(pages))
        rules[key] = tuple(sets)
    return rules


def _variants_of(copies: Mapping[str, Copy]) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = {}
    for c in copies.values():
        if c.box_id is None or c.voided or not c.accepted:
            continue
        out.setdefault(c.page_id, set()).add(c.variant)
    return out


def _apply_operations(
    boxes: Mapping[str, Box],
    copies: Dict[str, Copy],
    resolutions: Dict[str, str],
    operations: object,
) -> Tuple[dict, ...]:
    _require(isinstance(operations, list), "operations must be a list", location="operations")
    events: List[dict] = []
    for idx, op in enumerate(operations):
        loc = f"operations[{idx}]"
        _require(isinstance(op, dict), "operation must be an object", location=loc)
        kind = op.get("op")
        _require(isinstance(kind, str) and kind.strip() != "", "operation needs an 'op'", location=loc)

        if kind == "box":
            cid = op.get("copy_id")
            bid = op.get("box_id")
            _require(cid in copies, f"box: unknown copy {cid!r}", location=loc)
            _require(bid in boxes, f"box: unknown box {bid!r}", location=loc)
            c = copies[cid]
            _require(c.box_id is None, f"box: copy {cid!r} is already boxed in {c.box_id!r}", location=loc)
            copies[cid] = replace(c, box_id=bid)
            events.append({"op": kind, "copy_id": cid, "box_id": bid})

        elif kind == "transfer":
            cid = op.get("copy_id")
            bid = op.get("to_box")
            _require(cid in copies, f"transfer: unknown copy {cid!r}", location=loc)
            _require(bid in boxes, f"transfer: unknown box {bid!r}", location=loc)
            c = copies[cid]
            _require(c.box_id is not None, f"transfer: copy {cid!r} is not boxed yet", location=loc)
            copies[cid] = replace(c, box_id=bid)
            events.append({"op": kind, "copy_id": cid, "from_box": c.box_id, "to_box": bid})

        elif kind == "void":
            cid = op.get("copy_id")
            _require(cid in copies, f"void: unknown copy {cid!r}", location=loc)
            copies[cid] = replace(copies[cid], voided=True, sealed=False)
            events.append({"op": kind, "copy_id": cid})

        elif kind in ("restore", "unvoid"):
            cid = op.get("copy_id")
            _require(cid in copies, f"{kind}: unknown copy {cid!r}", location=loc)
            # Restoring the original seal makes the copy usable again.
            copies[cid] = replace(copies[cid], voided=False, accepted=True)
            events.append({"op": kind, "copy_id": cid})

        elif kind in ("resolve_variant",):
            pid = op.get("page_id")
            variant = op.get("variant")
            _require(isinstance(pid, str) and pid in {c.page_id for c in copies.values()},
                     f"resolve_variant: unknown page {pid!r}", location=loc)
            variants = _variants_of(copies).get(pid, set())
            _require(isinstance(variant, str) and variant in variants,
                     f"resolve_variant: {variant!r} is not a live variant of page {pid!r}", location=loc)
            resolutions[pid] = variant
            events.append({"op": kind, "page_id": pid, "variant": variant})

        elif kind in ("seal", "batch_seal"):
            box_ids = op.get("box_ids")
            _require(isinstance(box_ids, list) and box_ids,
                     f"{kind}: box_ids must be a non-empty list", location=loc)
            for bid in box_ids:
                _require(bid in boxes, f"{kind}: unknown box {bid!r}", location=loc)
            target = set(box_ids)
            variants = _variants_of(copies)
            # Atomic pre-check: a batch seal must not seal a box holding an
            # unresolved duplicate-page-number variant conflict.
            conflicted = sorted({
                c.page_id
                for c in copies.values()
                if c.box_id in target and len(variants.get(c.page_id, set())) > 1
                and c.page_id not in resolutions
            })
            if conflicted:
                raise BatchAtomicityError(
                    f"{kind}: pages {conflicted} have unresolved variant conflicts; batch seal refused",
                    location=loc,
                    detail={"pages": conflicted, "box_ids": sorted(target)},
                )
            for cid, c in list(copies.items()):
                if c.box_id in target:
                    copies[cid] = replace(c, accepted=True, sealed=True)
            events.append({"op": kind, "box_ids": sorted(target)})

        else:
            raise SchemaError(f"unknown operation {kind!r}", location=loc)
    return tuple(events)


def parse_archive(doc: object) -> Archive:
    _require(isinstance(doc, dict), "archive must be a JSON object")
    _require(doc.get("format") == ARCHIVE_FORMAT, f"archive format must be {ARCHIVE_FORMAT!r}", location="format")
    revision = doc.get("revision")
    _require(isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1,
             "archive revision must be a positive integer", location="revision")

    raw_boxes = doc.get("boxes")
    _require(isinstance(raw_boxes, list), "boxes must be a list", location="boxes")
    _require(len(raw_boxes) <= MAX_BOXES, f"at most {MAX_BOXES} boxes are supported", location="boxes")
    boxes: Dict[str, Box] = {}
    for idx, entry in enumerate(raw_boxes):
        box = _parse_box(entry, idx)
        _require(box.box_id not in boxes, f"duplicate box_id {box.box_id!r}", location=box.box_id)
        boxes[box.box_id] = box

    raw_copies = doc.get("copies")
    _require(isinstance(raw_copies, list), "copies must be a list", location="copies")
    copies: Dict[str, Copy] = {}
    for idx, entry in enumerate(raw_copies):
        copy = _parse_copy(entry, idx, set(boxes))
        _require(copy.copy_id not in copies, f"duplicate copy_id {copy.copy_id!r}", location=copy.copy_id)
        copies[copy.copy_id] = copy

    seal_rules = _parse_seal_rules(doc.get("seal_rules", []))

    resolutions: Dict[str, str] = {}
    raw_res = doc.get("conflict_resolutions", {})
    _require(isinstance(raw_res, dict), "conflict_resolutions must be an object", location="conflict_resolutions")
    page_variants = _variants_of(copies)
    for pid, variant in raw_res.items():
        _require(isinstance(variant, str), "conflict resolution must be a string variant", location=pid)
        _require(pid in {c.page_id for c in copies.values()},
                 f"conflict resolution for unknown page {pid!r}", location=pid)
        _require(variant in page_variants.get(pid, set()),
                 f"resolved variant {variant!r} is not a live variant of page {pid!r}", location=pid)
        resolutions[pid] = variant

    events = _apply_operations(boxes, copies, resolutions, doc.get("operations", []))

    return Archive(
        revision=revision,
        boxes=dict(boxes),
        copies=dict(copies),
        resolutions=dict(resolutions),
        seal_rules=dict(seal_rules),
        events=events,
    )


__all__ = [
    "ARCHIVE_FORMAT",
    "MAX_BOXES",
    "Archive",
    "Box",
    "Copy",
    "EvidencePage",
    "RecordEvidence",
    "parse_archive",
]
