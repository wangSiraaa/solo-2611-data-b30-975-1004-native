"""Independent verifier for an ``internship-review/1`` decision.

This module never imports :mod:`gradaudit.placement.eligibility` or
:mod:`gradaudit.solver`.  It re-derives the joint condition from the source
documents itself:

* every reserved record is re-checked against the catalog's explicit
  equivalences and the post's accepted categories;
* the embedded graduation plan is fed through the *original*
  :func:`gradaudit.verify.verify_plan` entry point after removing the reserved
  records, so an old contract plan stays verifiable through the old door;
* negative decisions are re-examined with this module's own exhaustive search,
  including its own graduation feasibility routine (memoised backtracking over
  whole records), so a wrongly-issued refusal is located too.

Reasons carry stable codes (``review_*``); no solver output is trusted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional, Sequence, Tuple

from ..model import Catalog, Plan, Record, Records, Requirements, parse_plan
from ..verify import verify_plan
from .documents import (
    GRADUATION_BLOCKED_BY_RESERVATION,
    GRADUATION_NOT_FEASIBLE,
    POST_REQUIREMENTS_UNMET,
    Post,
)


@dataclass
class ReviewVerifyResult:
    accepted: bool
    decision: str
    student_id: str
    post_id: str
    reasons: List[dict] = field(default_factory=list)
    reservation_points: int = 0
    graduation_verification: Optional[dict] = None

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "decision": self.decision,
            "identity": {"student_id": self.student_id, "post_id": self.post_id},
            "reservation_points": self.reservation_points,
            "graduation_verification": self.graduation_verification,
            "reasons": list(self.reasons),
        }


def _reason(code: str, message: str, *, location: Optional[str] = None, detail=None) -> dict:
    r = {"code": code, "message": message}
    if location is not None:
        r["location"] = location
    if detail is not None:
        r["detail"] = detail
    return r


# --------------------------------------------------------------------------- #
# own graduation feasibility routine (no solver import)
# --------------------------------------------------------------------------- #
def graduation_feasible(
    catalog: Catalog, records: Sequence[Record], requirements: Requirements
) -> bool:
    """True iff the given records can satisfy every requirement group.

    Whole records only: each is skipped or assigned one candidate course that
    hits one group's categories and contributes its full points there.
    """
    groups = requirements.groups
    minima = tuple(g.minimum_points for g in groups)
    recs = sorted(records, key=lambda r: r.record_id)

    @lru_cache(maxsize=None)
    def go(pointer: int, gains: Tuple[int, ...]) -> bool:
        if all(gains[i] >= minima[i] for i in range(len(groups))):
            return True
        if pointer == len(recs):
            return False
        rec = recs[pointer]
        if go(pointer + 1, gains):           # skip the record
            return True
        candidates = catalog.candidates(rec.external_course)
        for g, group in enumerate(groups):
            if gains[g] >= minima[g]:
                continue
            if any(set(catalog.courses[cid].categories) & set(group.allowed_categories)
                   for cid in candidates):
                new = list(gains)
                new[g] = min(gains[g] + rec.points, minima[g])
                if go(pointer + 1, tuple(new)):
                    return True
        return False

    return go(0, tuple(0 for _ in groups))


def _post_options(
    catalog: Catalog, records: Records, post: Post
) -> List[Tuple[Record, Tuple[str, ...]]]:
    out = []
    for rec in sorted(records.records.values(), key=lambda r: r.record_id):
        courses = tuple(
            cid
            for cid in catalog.candidates(rec.external_course)
            if post.accepted_categories & set(catalog.courses[cid].categories)
        )
        if courses:
            out.append((rec, courses))
    return out


def _exists_joint_feasible(
    catalog: Catalog, records: Records, requirements: Requirements, post: Post
) -> bool:
    """Independent re-derivation of the whole joint condition."""
    options = _post_options(catalog, records, post)

    def search(idx: int, reserved: set, points: int) -> bool:
        if idx == len(options):
            if points < post.required_points:
                return False
            remaining = [rec for rid, rec in records.records.items() if rid not in reserved]
            return graduation_feasible(catalog, remaining, requirements)
        rec, _courses = options[idx]
        if search(idx + 1, reserved, points):                       # leave for graduation
            return True
        reserved.add(rec.record_id)
        if search(idx + 1, reserved, points + rec.points):          # reserve whole record
            reserved.remove(rec.record_id)
            return True
        reserved.remove(rec.record_id)
        return False

    return search(0, set(), 0)


def _actual_failure_codes(
    catalog: Catalog, records: Records, requirements: Requirements, post: Post
) -> List[str]:
    codes: List[str] = []
    if not graduation_feasible(catalog, list(records.records.values()), requirements):
        codes.append(GRADUATION_NOT_FEASIBLE)
    max_points = sum(rec.points for rec, _ in _post_options(catalog, records, post))
    if max_points < post.required_points:
        codes.append(POST_REQUIREMENTS_UNMET)
    if not codes and not _exists_joint_feasible(catalog, records, requirements, post):
        codes.append(GRADUATION_BLOCKED_BY_RESERVATION)
    return codes


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def verify_review(
    review_doc: dict,
    catalog: Catalog,
    records: Records,
    requirements: Requirements,
    posts: Dict[str, Post],
) -> ReviewVerifyResult:
    reasons: List[dict] = []
    student_id = review_doc.get("student_id")
    post_id = review_doc.get("post_id")
    decision = review_doc.get("decision")

    # ---- identity / revisions ----------------------------------------------
    if student_id != records.student_id:
        reasons.append(_reason(
            "review_identity_mismatch",
            f"review student {student_id!r} does not match records student {records.student_id!r}",
            location="student_id",
        ))
    if review_doc.get("request_id") != requirements.request_id:
        reasons.append(_reason(
            "review_identity_mismatch",
            f"review request {review_doc.get('request_id')!r} does not match requirements "
            f"{requirements.request_id!r}",
            location="request_id",
        ))
    for name, doc_val in (
        ("catalog_revision", catalog.revision),
        ("records_revision", records.revision),
        ("requirements_revision", requirements.revision),
    ):
        if review_doc.get(name) != doc_val:
            reasons.append(_reason(
                "review_revision_mismatch",
                f"review {name} {review_doc.get(name)} does not match supplied revision {doc_val}",
                location=name,
            ))

    post = posts.get(post_id)
    if post is None:
        reasons.append(_reason(
            "review_unknown_post", f"post {post_id!r} is not in the posts document", location="post_id"
        ))
        return ReviewVerifyResult(False, decision, student_id, post_id, reasons)

    embedded = review_doc.get("post", {})
    if (set(embedded.get("accepted_categories", [])) != set(post.accepted_categories)
            or embedded.get("required_points") != post.required_points):
        reasons.append(_reason(
            "review_post_tampered",
            "the post embedded in the review does not match the posts document",
            location="post",
            detail={"embedded": embedded, "actual": {
                "post_id": post.post_id,
                "accepted_categories": sorted(post.accepted_categories),
                "required_points": post.required_points,
            }},
        ))

    # ---- reservation facts, re-derived from the catalog --------------------
    reservation = review_doc.get("reservation", [])
    reserved_ids: List[str] = []
    reservation_points = 0
    seen = set()
    for idx, entry in enumerate(reservation):
        loc = entry.get("record_id", f"reservation[{idx}]")
        rid = entry.get("record_id")
        if rid in seen:
            reasons.append(_reason(
                "review_reservation_reuses_record",
                f"record {rid!r} appears in the reservation more than once", location=rid
            ))
            continue
        seen.add(rid)
        rec = records.records.get(rid)
        if rec is None:
            reasons.append(_reason(
                "review_reservation_unknown_record",
                f"record {rid!r} is not present in records revision {records.revision}", location=rid
            ))
            continue
        cid = entry.get("course_id")
        candidates = catalog.candidates(rec.external_course)
        if cid not in candidates:
            reasons.append(_reason(
                "review_reservation_not_candidate",
                f"course {cid!r} is not an explicit candidate for external course "
                f"{rec.external_course!r} (candidates: {list(candidates)})", location=rid
            ))
            continue
        if not (set(catalog.courses[cid].categories) & post.accepted_categories):
            reasons.append(_reason(
                "review_reservation_category_mismatch",
                f"course {cid!r} categories {sorted(catalog.courses[cid].categories)} do not "
                f"hit post categories {sorted(post.accepted_categories)}", location=rid
            ))
            continue
        reserved_ids.append(rid)
        reservation_points += rec.points

    # ---- decision-specific semantics ---------------------------------------
    if decision == "eligible":
        if reservation_points < post.required_points:
            reasons.append(_reason(
                "review_post_underreserved",
                f"reservation provides {reservation_points} points, post requires "
                f"{post.required_points}",
                location="reservation",
                detail={"achieved": reservation_points, "required": post.required_points},
            ))
        plan_doc = review_doc.get("graduation_plan")
        if not isinstance(plan_doc, dict):
            reasons.append(_reason(
                "review_missing_graduation_plan",
                "an eligible review must carry a course-allocation/1 graduation plan",
                location="graduation_plan",
            ))
        else:
            # The old verifier runs on the *full* records document, so every
            # candidate / category / group / point rule keeps its original
            # meaning; the post/graduation disjointness is then an explicit
            # check rather than being hidden as an unknown-record rejection.
            plan: Plan = parse_plan(plan_doc)
            plan_res = verify_plan(plan, catalog, records, requirements)
            grad_verdict = plan_res.as_dict()
            if not plan_res.accepted:
                reasons.append(_reason(
                    "review_graduation_plan_rejected",
                    "the embedded graduation plan failed the independent course-allocation verifier",
                    location="graduation_plan",
                    detail=plan_res.reasons,
                ))
            overlap = set(plan_res.used_records) & set(reserved_ids)
            if overlap:
                reasons.append(_reason(
                    "review_record_split_between_post_and_graduation",
                    f"records {sorted(overlap)} are both reserved for the post and used by the "
                    f"graduation plan; one record cannot serve both",
                    location=sorted(overlap)[0],
                    detail={"shared_records": sorted(overlap)},
                ))
    else:
        grad_verdict = None
        # A refusal must be genuine: re-run the joint condition independently.
        if not reasons:
            actual = _actual_failure_codes(catalog, records, requirements, post)
            if not actual:
                reasons.append(_reason(
                    "review_ineligible_decision_unsupported",
                    "the review refuses eligibility, but an independent re-derivation found a "
                    "reservation plus a legal graduation allocation",
                    location="decision",
                ))
            else:
                stated = {r.get("code") for r in review_doc.get("reasons", [])}
                if not stated:
                    reasons.append(_reason(
                        "review_ineligible_without_reason",
                        "an ineligible review must state which joint condition failed",
                        location="reasons",
                    ))
                elif not (stated & set(actual)):
                    reasons.append(_reason(
                        "review_reason_unsupported",
                        "the stated reason does not match the independently recomputed failure",
                        location="reasons",
                        detail={"stated": sorted(stated), "actual": actual},
                    ))

    return ReviewVerifyResult(
        accepted=not reasons,
        decision=decision,
        student_id=student_id,
        post_id=post_id,
        reasons=reasons,
        reservation_points=reservation_points,
        graduation_verification=grad_verdict if decision == "eligible" else None,
    )


__all__ = ["ReviewVerifyResult", "verify_review", "graduation_feasible"]
