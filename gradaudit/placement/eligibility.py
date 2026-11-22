"""Department-side joint eligibility for an internship post.

A student qualifies for a post only when **both** halves of a single joint
condition hold at the same time:

1. a set of whole transfer records can be *reserved for the post* -- every
   reserved record maps (through the catalog's explicit equivalences only) to a
   local course carrying one of the post's accepted categories, and the
   reserved records total at least the post's required points; and
2. every record *not* reserved still admits a legal graduation allocation
   against the student's own registered requirements (the ordinary
   ``gradaudit`` problem, re-solved here with the reserved records removed).

The same record can never appear on both sides: the graduation sub-problem is
solved on a records document that simply does not contain the reserved ids.
That makes the disjointness structural, not a convention -- a graduation plan
cannot mention a record it never saw, and the verifier re-checks this.

Search
------
For each record (id order) the options are: skip it, or reserve it under one of
its qualifying candidate courses (catalog equivalence order).  Every resulting
reservation that reaches the required point total is handed to the exact
graduation solver on the remaining records; the first joint-feasible choice is
returned with a deterministic, independently verifiable graduation plan.  When
none works the located reason says which half of the joint condition failed.
Scale is bounded by the existing contract (at most 12 records, points 1..6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..errors import SchemaError
from ..model import Catalog, Record, Records, Requirements
from ..solver import MAX_RECORDS, MAX_POINTS, solve
from .documents import (
    GRADUATION_BLOCKED_BY_RESERVATION,
    GRADUATION_NOT_FEASIBLE,
    POST_REQUIREMENTS_UNMET,
    Post,
)

# Re-exported for callers that import names from this module.
__all__ = ["ReviewResult", "evaluate_eligibility", "qualifying_courses"]


@dataclass
class ReviewResult:
    decision: str                       # "eligible" | "ineligible"
    student_id: str
    post_id: str
    request_id: str
    catalog_revision: int
    records_revision: int
    requirements_revision: int
    post: Post
    reservation: List[dict] = field(default_factory=list)
    reservation_total_points: int = 0
    graduation_plan: Optional[dict] = None
    reasons: List[dict] = field(default_factory=list)
    subsets_considered: int = 0
    max_post_points: int = 0

    def as_document(self) -> dict:
        return {
            "format": "internship-review/1",
            "decision": self.decision,
            "student_id": self.student_id,
            "post_id": self.post_id,
            "request_id": self.request_id,
            "catalog_revision": self.catalog_revision,
            "records_revision": self.records_revision,
            "requirements_revision": self.requirements_revision,
            "post": {
                "post_id": self.post.post_id,
                "accepted_categories": sorted(self.post.accepted_categories),
                "required_points": self.post.required_points,
            },
            "reservation": list(self.reservation),
            "reservation_total_points": self.reservation_total_points,
            "graduation_plan": self.graduation_plan,
            "reasons": list(self.reasons),
            "search": {"subsets_considered": self.subsets_considered},
        }


def _reason(code: str, message: str, *, location: Optional[str] = None, detail=None) -> dict:
    r = {"code": code, "message": message}
    if location is not None:
        r["location"] = location
    if detail is not None:
        r["detail"] = detail
    return r


def _validate_scale(records: Records) -> None:
    if len(records.records) > MAX_RECORDS:
        raise SchemaError(
            f"at most {MAX_RECORDS} records are supported (got {len(records.records)})",
            location="records",
        )
    for rec in records.records.values():
        if not (1 <= rec.points <= MAX_POINTS):
            raise SchemaError(
                f"points must be in 1..{MAX_POINTS} (record {rec.record_id} has {rec.points})",
                location=rec.record_id,
            )


def qualifying_courses(catalog: Catalog, rec: Record, post: Post) -> Tuple[str, ...]:
    """Candidate courses (explicit equivalences only) that hit a post category."""
    return tuple(
        cid
        for cid in catalog.candidates(rec.external_course)
        if post.accepted_categories & set(catalog.courses[cid].categories)
    )


def _reservation_options(
    catalog: Catalog, records: Records, post: Post
) -> List[Tuple[Record, Tuple[str, ...]]]:
    """Per record: the courses under which it could be reserved for the post."""
    options: List[Tuple[Record, Tuple[str, ...]]] = []
    for rec in sorted(records.records.values(), key=lambda r: r.record_id):
        courses = qualifying_courses(catalog, rec, post)
        if courses:
            options.append((rec, courses))
    return options


def _without_records(records: Records, reserved: FrozenSet[str]) -> Records:
    return Records(
        student_id=records.student_id,
        revision=records.revision,
        records={rid: rec for rid, rec in records.records.items() if rid not in reserved},
    )


def evaluate_eligibility(
    catalog: Catalog,
    records: Records,
    requirements: Requirements,
    post: Post,
) -> ReviewResult:
    """Decide the joint post/graduation condition for one student and one post.

    The review is specific to this pair; eligibility for another post is a
    separate review that may reserve a different set of records.  Nothing here
    requires the student to keep reservations for several posts at once.
    """
    _validate_scale(records)
    base = ReviewResult(
        decision="ineligible",
        student_id=records.student_id,
        post_id=post.post_id,
        request_id=requirements.request_id,
        catalog_revision=catalog.revision,
        records_revision=records.revision,
        requirements_revision=requirements.revision,
        post=post,
    )

    # Half 0: graduation must be legal even before anything is reserved.
    plain = solve(catalog, records, requirements)
    if not plain.feasible:
        base.reasons.append(_reason(
            GRADUATION_NOT_FEASIBLE,
            f"student {records.student_id!r} cannot complete the registered graduation "
            f"requirements even without reserving any record for post {post.post_id!r}",
            location=post.post_id,
            detail={"groups": [g.__dict__ for g in plain.groups]},
        ))
        return base

    options = _reservation_options(catalog, records, post)
    max_post_points = sum(rec.points for rec, _ in options)
    base.max_post_points = max_post_points
    if max_post_points < post.required_points:
        base.reasons.append(_reason(
            POST_REQUIREMENTS_UNMET,
            f"records that can map into post {post.post_id!r} categories "
            f"{sorted(post.accepted_categories)} total {max_post_points} points, "
            f"below the required {post.required_points}",
            location=post.post_id,
            detail={"max_reachable_points": max_post_points, "required_points": post.required_points},
        ))
        return base

    # Enumerate reservations (record subsets x qualifying course choices) in a
    # fixed order; stop at the first one whose remainder still graduates.
    qualifying: List[Tuple[List[dict], FrozenSet[str], int]] = []
    subsets_considered = 0

    def search(idx: int, chosen: List[dict], reserved: set, points: int) -> None:
        nonlocal subsets_considered
        if idx == len(options):
            if points >= post.required_points:
                subsets_considered += 1
                qualifying.append((list(chosen), frozenset(reserved), points))
            return
        rec, courses = options[idx]
        # option 1: leave this record for the graduation side
        search(idx + 1, chosen, reserved, points)
        # option 2: reserve it whole under each qualifying course
        for cid in courses:
            chosen.append({
                "record_id": rec.record_id,
                "external_course": rec.external_course,
                "course_id": cid,
                "points": rec.points,
            })
            reserved.add(rec.record_id)
            search(idx + 1, chosen, reserved, points + rec.points)
            reserved.remove(rec.record_id)
            chosen.pop()

    search(0, [], set(), 0)
    base.subsets_considered = subsets_considered

    for reservation, reserved_ids, total in qualifying:
        remainder = _without_records(records, reserved_ids)
        outcome = solve(catalog, remainder, requirements)
        if outcome.feasible:
            base.decision = "eligible"
            base.reservation = reservation
            base.reservation_total_points = total
            base.graduation_plan = outcome.as_plan_document()
            return base

    base.reasons.append(_reason(
        GRADUATION_BLOCKED_BY_RESERVATION,
        f"every reservation that gives post {post.post_id!r} its "
        f"{post.required_points} points removes a record the graduation requirements "
        f"still need; no record can count for both the post and graduation",
        location=post.post_id,
        detail={
            "required_points": post.required_points,
            "reservations_reaching_required_points": subsets_considered,
            "max_reachable_points": max_post_points,
        },
    ))
    return base
