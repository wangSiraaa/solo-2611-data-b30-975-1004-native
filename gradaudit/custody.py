"""Custodian retention optimiser (保管处留盒问题).

Given the paper-archive state (:mod:`gradaudit.archive`), the mother-library
catalog / records / requirements, and per-box storage costs, decide **which
boxes to keep** so that:

* in the all-normal case, and
* after *any single retained box* becomes permanently unreadable,

every student can still assemble a satisfying graduation plan from the records
that remain reconstructable -- each fault case free to use *different* records
and a *different* allocation.

The objective is minimum total storage cost.  A kept box that happens to be
empty is still kept (it costs, and it appears among the loss scenarios); it is
checked under the all-normal case exactly like any other box.

Feasibility is monotone in the kept set: reading more boxes can only make more
records reconstructable.  Therefore if even *all* boxes cannot survive some
single-box loss, no subset can, and we report the failing students/scenarios.
Otherwise a subset search in increasing cost returns the cheapest working list.

Every emitted allocation is an ordinary ``course-allocation/1`` document that
the independent verifier (:func:`gradaudit.verify.verify_plan`) can re-check
record by record against the mother library -- the custody layer does not
bypass that check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .archive import Archive, parse_archive
from .errors import SchemaError
from .model import (
    Assignment,
    Catalog,
    Plan,
    Records,
    Requirements,
    parse_catalog,
    parse_records,
    parse_requirements,
)
from .solver import MasterResult, solve
from .verify import verify_plan

CUSTODY_FORMAT = "custody/1"


def _require(cond: bool, message: str, *, location: Optional[str] = None) -> None:
    if not cond:
        raise SchemaError(message, location=location)


@dataclass(frozen=True)
class CustodyStudent:
    records: Records
    requirements: Requirements

    @property
    def student_id(self) -> str:
        return self.records.student_id

    def record_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self.records.records))


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def parse_custody(doc: object) -> Tuple[Archive, Catalog, Dict[str, CustodyStudent]]:
    _require(isinstance(doc, dict), "custody document must be a JSON object")
    _require(doc.get("format") == CUSTODY_FORMAT,
             f"custody format must be {CUSTODY_FORMAT!r}", location="format")

    _require("archive" in doc, "custody document needs an 'archive'", location="archive")
    archive = parse_archive(doc["archive"])
    _require("catalog" in doc, "custody document needs a 'catalog'", location="catalog")
    catalog = parse_catalog(doc["catalog"])

    raw_students = doc.get("students")
    _require(isinstance(raw_students, list) and raw_students,
             "students must be a non-empty list", location="students")
    students: Dict[str, CustodyStudent] = {}
    for idx, entry in enumerate(raw_students):
        loc = f"students[{idx}]"
        _require(isinstance(entry, dict), "student must be an object", location=loc)
        _require("records" in entry and "requirements" in entry,
                 "student needs both 'records' and 'requirements'", location=loc)
        records = parse_records(entry["records"])
        requirements = parse_requirements(entry["requirements"])
        sid = records.student_id
        _require(sid not in students, f"duplicate student {sid!r}", location=sid)
        students[sid] = CustodyStudent(records=records, requirements=requirements)

    # Seal rules may only talk about students we were given.
    for sid in archive.student_ids():
        _require(sid in students,
                 f"archive has seal rules for student {sid!r} but no records/requirements were supplied",
                 location=sid)
    return archive, catalog, students


# --------------------------------------------------------------------------- #
# optimiser
# --------------------------------------------------------------------------- #
class _Model:
    """Bitmask model of one archive + catalog + set of students."""

    def __init__(self, archive: Archive, catalog: Catalog, students: Mapping[str, CustodyStudent]):
        self.archive = archive
        self.catalog = catalog
        self.students = dict(students)

        self.box_ids: Tuple[str, ...] = archive.box_ids()
        self.box_index: Dict[str, int] = {b: i for i, b in enumerate(self.box_ids)}
        self.box_cost: Tuple[int, ...] = tuple(archive.boxes[b].cost for b in self.box_ids)
        self.all_mask: int = (1 << len(self.box_ids)) - 1

        # page -> bitmask of boxes holding a usable copy of that page
        variants = archive.page_variants()
        self.page_boxmask: Dict[str, int] = {}
        for copy in archive.copies.values():
            if archive.is_copy_usable(copy, variants):
                self.page_boxmask[copy.page_id] = (
                    self.page_boxmask.get(copy.page_id, 0) | (1 << self.box_index[copy.box_id])
                )

        # per-student ordered records and their alternative seal sets
        self.record_ids: Dict[str, Tuple[str, ...]] = {}
        self.seal_sets: Dict[str, List[Tuple[Tuple[str, ...], ...]]] = {}
        for sid, student in self.students.items():
            rids = student.record_ids()
            self.record_ids[sid] = rids
            self.seal_sets[sid] = [archive.rule_sets(sid, rid) for rid in rids]

        self._avail_cache: Dict[Tuple[str, int], int] = {}
        self._solve_cache: Dict[Tuple[str, int], MasterResult] = {}
        self._scenario_cache: Dict[int, bool] = {}

    # -- record availability ----------------------------------------------
    def available_record_mask(self, sid: str, readable: int) -> int:
        key = (sid, readable)
        cached = self._avail_cache.get(key)
        if cached is not None:
            return cached
        mask = 0
        for i, sets in enumerate(self.seal_sets[sid]):
            for seal_set in sets:
                if all(self.page_boxmask.get(p, 0) & readable for p in seal_set):
                    mask |= 1 << i
                    break
        self._avail_cache[key] = mask
        return mask

    # -- graduation feasibility for one student ---------------------------
    def student_result(self, sid: str, readable: int) -> MasterResult:
        amask = self.available_record_mask(sid, readable)
        key = (sid, amask)
        cached = self._solve_cache.get(key)
        if cached is not None:
            return cached
        student = self.students[sid]
        subset = {
            rid: student.records.records[rid]
            for i, rid in enumerate(self.record_ids[sid])
            if amask & (1 << i)
        }
        recs = Records(student_id=sid, revision=student.records.revision, records=subset)
        result = solve(self.catalog, recs, student.requirements)
        self._solve_cache[key] = result
        return result

    def scenario_ok(self, readable: int) -> bool:
        cached = self._scenario_cache.get(readable)
        if cached is not None:
            return cached
        ok = all(self.student_result(sid, readable).feasible for sid in self.students)
        self._scenario_cache[readable] = ok
        return ok

    # -- helpers -----------------------------------------------------------
    def boxes_of(self, mask: int) -> List[str]:
        return [b for i, b in enumerate(self.box_ids) if mask & (1 << i)]

    def bits(self, mask: int) -> Iterable[int]:
        m = mask
        while m:
            low = m & -m
            yield low.bit_length() - 1
            m ^= low


def _subset_costs(box_cost: Sequence[int]) -> List[int]:
    n = len(box_cost)
    cost = [0] * (1 << n)
    for m in range(1, 1 << n):
        low = m & -m
        i = low.bit_length() - 1
        cost[m] = cost[m ^ low] + box_cost[i]
    return cost


def _failing_students(model: _Model, readable: int) -> List[dict]:
    out = []
    for sid in model.students:
        result = model.student_result(sid, readable)
        if not result.feasible:
            out.append({
                "student_id": sid,
                "request_id": result.request_id,
                "group_maxima_ignoring_other_groups": {
                    g.group_id: g.max_ignoring_other_groups for g in result.groups
                },
                "group_minima": {g.group_id: g.minimum_points for g in result.groups},
                "note": result.note,
            })
    return out


def _student_plan(model: _Model, sid: str, readable: int) -> Optional[dict]:
    result = model.student_result(sid, readable)
    if not result.feasible:
        return None
    student = model.students[sid]
    readable_boxes = model.boxes_of(readable)

    plan_doc = result.as_plan_document()
    plan = Plan(
        student_id=plan_doc["student_id"],
        catalog_revision=plan_doc["catalog_revision"],
        records_revision=plan_doc["records_revision"],
        request_id=plan_doc["request_id"],
        requirements_revision=plan_doc["requirements_revision"],
        assignments=tuple(
            Assignment(a["record_id"], a["course_id"], a["group_id"])
            for a in plan_doc["assignments"]
        ),
    )
    # Re-check every emitted allocation with the *independent* verifier against
    # the mother-library records (not the reconstructed subset).
    verification = verify_plan(plan, model.catalog, student.records, student.requirements)

    evidence_by_record = {
        e.record_id: e for e in model.archive.reconstruct_for_student(sid, readable_boxes)
    }
    evidence = []
    for a in result.assignments:
        rec = student.records.records[a.record_id]
        ev = evidence_by_record.get(a.record_id)
        evidence.append({
            "record_id": a.record_id,
            "external_course": rec.external_course,
            "points": rec.points,
            "course_id": a.course_id,
            "group_id": a.group_id,
            "seal_set": list(ev.seal_set) if ev else None,
            "pages": [p.as_dict() for p in ev.pages] if ev else [],
        })

    reconstructable = sorted(evidence_by_record)
    return {
        "student_id": sid,
        "catalog_revision": model.catalog.revision,
        "records_revision": student.records.revision,
        "request_id": student.requirements.request_id,
        "requirements_revision": student.requirements.revision,
        "plan": plan_doc,
        "group_totals": dict(result.group_totals),
        "used_records": [a.record_id for a in result.assignments],
        "reconstructable_records": reconstructable,
        "evidence": evidence,
        "independent_verification": {
            "accepted": verification.accepted,
            "reasons": verification.reasons,
        },
    }


def _plans_for_scenario(model: _Model, readable: int) -> List[dict]:
    plans = []
    for sid in model.students:
        p = _student_plan(model, sid, readable)
        if p is not None:
            plans.append(p)
    return plans


def _build_certificates(model: _Model, mask: int, total_cost: int) -> dict:
    """Certificates for one retention list: all-normal + every single-box loss."""
    retained = model.boxes_of(mask)
    normal = {
        "scenario": {"kind": "all_normal", "lost_box": None},
        "readable_boxes": retained,
        "students": _plans_for_scenario(model, mask),
    }
    loss_certificates = []
    for i in model.bits(mask):
        lost = model.box_ids[i]
        readable = mask ^ (1 << i)
        plans = _plans_for_scenario(model, readable)
        # Every evidence page used must come from a box other than the lost one.
        for p in plans:
            for e in p["evidence"]:
                for page in e["pages"]:
                    _require(page["box_id"] != lost,
                             "internal error: evidence cites the lost box", location=lost)
        loss_certificates.append({
            "scenario": {"kind": "single_box_loss", "lost_box": lost},
            "missing_box": lost,
            "readable_boxes": model.boxes_of(readable),
            "students": plans,
        })
    return {
        "format": "custody-result/1",
        "feasible": True,
        "retained_boxes": [
            {"box_id": b, "cost": model.archive.boxes[b].cost} for b in retained
        ],
        "total_cost": total_cost,
        "normal": normal,
        "loss_certificates": loss_certificates,
    }


def _retention_failure(model: _Model, mask: int) -> dict:
    """Describe why a retention list is not survivable (empty box checked too)."""
    normal_ok = model.scenario_ok(mask)
    loss_failures: List[dict] = []
    if normal_ok:
        for i in model.bits(mask):
            lost = model.box_ids[i]
            readable = mask ^ (1 << i)
            if not model.scenario_ok(readable):
                loss_failures.append({
                    "scenario": {"kind": "single_box_loss", "lost_box": lost},
                    "readable_boxes": model.boxes_of(readable),
                    "students": _failing_students(model, readable),
                })
    return {
        "format": "custody-result/1",
        "feasible": False,
        "reason": "not every student can graduate from this retention list",
        "retained_boxes": [{"box_id": b, "cost": model.archive.boxes[b].cost}
                           for b in model.boxes_of(mask)],
        "students": sorted(model.students),
        "normal_failure": None if normal_ok else {
            "scenario": {"kind": "all_normal", "lost_box": None},
            "readable_boxes": model.boxes_of(mask),
            "students": _failing_students(model, mask),
        },
        "loss_failures": loss_failures,
    }


def review_retention(
    archive: Archive,
    catalog: Catalog,
    students: Mapping[str, CustodyStudent],
    retained_boxes: Sequence[str],
) -> dict:
    """Validate one explicitly supplied retention list.

    Empty boxes are *not* skipped: the all-normal case and the loss of every
    retained box (including an empty one) are all evaluated.  Returns the same
    certificate shape as :func:`optimize_custody`.
    """
    model = _Model(archive, catalog, students)
    wanted = sorted(set(retained_boxes))
    unknown = [b for b in wanted if b not in model.box_index]
    _require(not unknown, f"unknown retained box(es) {unknown}", location="retained_boxes")
    mask = 0
    for b in wanted:
        mask |= 1 << model.box_index[b]
    if not model.scenario_ok(mask) or not all(
        model.scenario_ok(mask ^ (1 << i)) for i in model.bits(mask)
    ):
        return _retention_failure(model, mask)
    cost = sum(model.archive.boxes[b].cost for b in wanted)
    return _build_certificates(model, mask, cost)


def optimize_custody(
    archive: Archive,
    catalog: Catalog,
    students: Mapping[str, CustodyStudent],
) -> dict:
    """Find the cheapest working retention list and build all certificates."""
    model = _Model(archive, catalog, students)

    # Monotonicity: all boxes is the most permissive readable set.  If it
    # cannot survive the normal case or some single-box loss, nothing can.
    normal_ok = model.scenario_ok(model.all_mask)
    loss_failures: List[dict] = []
    if normal_ok:
        for i in model.bits(model.all_mask):
            lost = model.box_ids[i]
            readable = model.all_mask ^ (1 << i)
            if not model.scenario_ok(readable):
                loss_failures.append({
                    "scenario": {"kind": "single_box_loss", "lost_box": lost},
                    "readable_boxes": model.boxes_of(readable),
                    "students": _failing_students(model, readable),
                })
    if not normal_ok or loss_failures:
        return {
            "format": "custody-result/1",
            "feasible": False,
            "reason": "not every student can graduate even using all boxes",
            "all_boxes": model.boxes_of(model.all_mask),
            "students": sorted(model.students),
            "normal_failure": None if normal_ok else {
                "scenario": {"kind": "all_normal", "lost_box": None},
                "readable_boxes": model.boxes_of(model.all_mask),
                "students": _failing_students(model, model.all_mask),
            },
            "loss_failures": loss_failures,
        }

    # Search by increasing total cost for the first set that survives normal +
    # every single retained-box loss.  Zero-cost boxes are still considered
    # (they must pass the same checks); the secondary key merely prefers fewer
    # boxes among equal-cost solutions.
    box_cost = model.box_cost
    costs = _subset_costs(box_cost)
    order = sorted(
        range(1 << len(model.box_ids)),
        key=lambda m: (costs[m], bin(m).count("1"), m),
    )
    chosen: Optional[int] = None
    for m in order:
        if not model.scenario_ok(m):
            continue
        if all(model.scenario_ok(m ^ (1 << i)) for i in model.bits(m)):
            chosen = m
            break

    if chosen is None:  # pragma: no cover - monotonicity says this cannot happen
        return {
            "format": "custody-result/1",
            "feasible": False,
            "reason": "no retention set survives all single-box losses",
        }

    return _build_certificates(model, chosen, costs[chosen])


def custody_report(doc: object) -> dict:
    archive, catalog, students = parse_custody(doc)
    return optimize_custody(archive, catalog, students)


__all__ = [
    "CUSTODY_FORMAT",
    "CustodyStudent",
    "parse_custody",
    "optimize_custody",
    "review_retention",
    "custody_report",
]
