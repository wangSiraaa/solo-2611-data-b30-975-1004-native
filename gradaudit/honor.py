"""Honor evaluation layered on top of a graduation audit.

Given one student, the enrolled graduation requirements and a *charter*, we

1. first find a feasible **complete** allocation of the student's graduation
   points against the requirement groups, then
2. from the records that were *not* spent on graduation, choose whole records
   whose chosen local course maps into an honor category, maximizing the sum of
   their points.

Records are whole: a record is used at most once and cannot be split between
graduation and honors.  If graduation cannot be satisfied at all the student is
**ineligible** and no honor points are reported.

Charter
-------
A charter draft (``CharterDraft``) covers a set of external course codes.  A
*finalization* pins, for every covered code, exactly one local course drawn from
the catalog's explicit candidate list.  Every occurrence of a covered code --
across all students and all records -- obeys the same choice.  Codes not covered
by the charter keep the catalog's candidate set.  A finalization can never
invent a candidate the catalog does not list; :func:`apply_choices` rejects such
input.

The derived catalog keeps the original revision number but replaces covered
codes' equivalence tuples with a singleton.  A graduation plan produced here can
therefore be re-checked with the ordinary independent verifier against that
derived catalog (see :func:`review_bundle`).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .errors import SchemaError
from .model import Assignment, Catalog, Plan, Record, Records, Requirements
from .verify import VerifyResult, verify_plan

MAX_RECORDS = 12
MAX_POINTS = 6

HONOR_FORMAT = "honor-allocation/1"


# --------------------------------------------------------------------------- #
# charter
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CharterDraft:
    """The set of covered codes plus each code's catalog candidates."""

    covered: Tuple[str, ...]
    candidates: Mapping[str, Tuple[str, ...]]

    @classmethod
    def from_catalog(cls, catalog: Catalog, covered_codes: Sequence[str]) -> "CharterDraft":
        covered: List[str] = []
        seen = set()
        for code in covered_codes:
            if not isinstance(code, str) or not code.strip():
                raise SchemaError("covered code must be a non-empty string", location="covered_codes")
            if code in seen:
                continue
            seen.add(code)
            covered.append(code)
        cand: Dict[str, Tuple[str, ...]] = {}
        for code in covered:
            opts = catalog.candidates(code)
            if not opts:
                raise SchemaError(
                    f"covered code {code!r} has no candidates in catalog revision "
                    f"{catalog.revision}; a charter cannot introduce a candidate",
                    location=code,
                )
            cand[code] = tuple(opts)
        return cls(covered=tuple(covered), candidates=cand)

    def validate(self, choices: Mapping[str, str]) -> Dict[str, str]:
        """Require choices to cover *exactly* the covered codes, legally."""
        missing = [c for c in self.covered if c not in choices]
        extra = [c for c in choices if c not in self.candidates]
        if missing:
            raise SchemaError(
                f"finalization is missing covered code(s): {missing}", location=missing[0]
            )
        if extra:
            raise SchemaError(
                f"finalization mentions uncovered code(s): {extra}; a charter cannot "
                f"introduce new codes or candidates",
                location=extra[0],
            )
        out: Dict[str, str] = {}
        for code in self.covered:
            chosen = choices[code]
            if chosen not in self.candidates[code]:
                raise SchemaError(
                    f"chosen course {chosen!r} for {code!r} is not one of its catalog "
                    f"candidates {list(self.candidates[code])}",
                    location=code,
                )
            out[code] = chosen
        return out

    def legal_finalizations(self) -> Iterator[Dict[str, str]]:
        if not self.covered:
            yield {}
            return
        for combo in itertools.product(*(self.candidates[c] for c in self.covered)):
            yield dict(zip(self.covered, combo))


def apply_choices(catalog: Catalog, choices: Mapping[str, str]) -> Catalog:
    """Derive an effective catalog with ``choices`` pinned to explicit candidates.

    Unmentioned codes keep the catalog's candidate set.  Every choice must be a
    catalog candidate for its code; a charter can never add a candidate.
    """
    overrides: Dict[str, Tuple[str, ...]] = {}
    for code, chosen in choices.items():
        opts = catalog.candidates(code)
        if not opts:
            raise SchemaError(
                f"cannot pin {code!r}: it has no candidates in catalog revision "
                f"{catalog.revision}",
                location=code,
            )
        if chosen not in opts:
            raise SchemaError(
                f"chosen course {chosen!r} for {code!r} is not a catalog candidate "
                f"{list(opts)}; a charter cannot introduce a candidate",
                location=code,
            )
        overrides[code] = (chosen,)
    equivalences = dict(catalog.equivalences)
    equivalences.update(overrides)
    return Catalog(revision=catalog.revision, courses=catalog.courses, equivalences=equivalences)


# --------------------------------------------------------------------------- #
# honor result
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HonorSelection:
    record_id: str
    course_id: str
    points: int


@dataclass
class HonorResult:
    eligible: bool
    student_id: str
    catalog_revision: int
    records_revision: int
    requirements_revision: int
    honor_points: Optional[int]
    graduation_assignments: List[Assignment] = field(default_factory=list)
    graduation_totals: Dict[str, int] = field(default_factory=dict)
    honor_selections: List[HonorSelection] = field(default_factory=list)
    reason: Optional[str] = None
    states_explored: int = 0

    def as_dict(self) -> dict:
        return {
            "format": HONOR_FORMAT,
            "eligible": self.eligible,
            "identity": {
                "student_id": self.student_id,
                "catalog_revision": self.catalog_revision,
                "records_revision": self.records_revision,
                "requirements_revision": self.requirements_revision,
            },
            "honor_points": self.honor_points,
            "graduation_assignments": [
                {"record_id": a.record_id, "course_id": a.course_id, "group_id": a.group_id}
                for a in self.graduation_assignments
            ],
            "graduation_totals": dict(self.graduation_totals),
            "honor_selections": [
                {"record_id": s.record_id, "course_id": s.course_id, "points": s.points}
                for s in self.honor_selections
            ],
            "reason": self.reason,
        }


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


def _effective_candidates(catalog: Catalog, code: str) -> Tuple[str, ...]:
    return catalog.candidates(code)


def evaluate_honor(
    catalog: Catalog,
    records: Records,
    requirements: Requirements,
    honor_categories: Sequence[str],
    *,
    fixed_choices: Optional[Mapping[str, str]] = None,
    student_id: Optional[str] = None,
) -> HonorResult:
    """Maximize honor points subject to a complete graduation allocation.

    ``fixed_choices`` optionally pins covered codes to a single candidate (the
    effective catalog may also already be derived via :func:`apply_choices`).
    Capping each group's gains at its minimum is safe: over-satisfaction cannot
    free a record for honors, so it cannot raise the honor total.
    """
    _validate_scale(records)
    honor_cats = frozenset(honor_categories)
    if fixed_choices:
        catalog = apply_choices(catalog, fixed_choices)

    groups = requirements.groups
    n_groups = len(groups)
    minima = [g.minimum_points for g in groups]
    allowed = [set(g.allowed_categories) for g in groups]

    recs: List[Record] = sorted(records.records.values(), key=lambda r: r.record_id)
    n = len(recs)

    grad_options: List[List[Tuple[str, ...]]] = []
    honor_option: List[Optional[str]] = []
    for rec in recs:
        cands = _effective_candidates(catalog, rec.external_course)
        per_group: List[Tuple[str, ...]] = []
        for g in range(n_groups):
            usable = tuple(
                sorted(cid for cid in cands if allowed[g] & set(catalog.courses[cid].categories))
            )
            per_group.append(usable)
        grad_options.append(per_group)
        honor_courses = sorted(
            cid for cid in cands if honor_cats & set(catalog.courses[cid].categories)
        )
        honor_option.append(honor_courses[0] if honor_courses else None)

    starvation = tuple(0 for _ in range(n_groups))
    # layers[i][gains] = (honor_points, previous_gains, action)
    # action is None (skip) | ("honor", course_id) | ("group", course_id, g)
    layers: List[Dict[Tuple[int, ...], Tuple[int, Optional[Tuple[int, ...]], Optional[tuple]]]] = [
        {} for _ in range(n + 1)
    ]
    layers[0][starvation] = (0, None, None)
    states_explored = 1

    for i, rec in enumerate(recs):
        nxt = layers[i + 1]
        for gains, (hval, _prev, _action) in layers[i].items():
            # 1. skip the record (unused is legal)
            old = nxt.get(gains)
            if old is None or hval > old[0]:
                nxt[gains] = (hval, gains, None)
            # 2. use it for honors
            hc = honor_option[i]
            if hc is not None:
                nh = hval + rec.points
                old = nxt.get(gains)
                if old is None or nh > old[0]:
                    nxt[gains] = (nh, gains, ("honor", hc))
            # 3. use it for one graduation group
            for g in range(n_groups):
                for cid in grad_options[i][g]:
                    new_gain = min(gains[g] + rec.points, minima[g])
                    if new_gain == gains[g]:
                        # group already saturated: never better than skipping
                        continue
                    new_gains = list(gains)
                    new_gains[g] = new_gain
                    ng = tuple(new_gains)
                    old = nxt.get(ng)
                    if old is None or hval > old[0]:
                        nxt[ng] = (hval, gains, ("group", cid, g))
        states_explored += len(nxt)

    best_gains: Optional[Tuple[int, ...]] = None
    best_honor = -1
    for gains, (hval, _prev, _action) in layers[n].items():
        if all(gains[g] >= minima[g] for g in range(n_groups)) and hval > best_honor:
            best_gains, best_honor = gains, hval

    sid = student_id if student_id is not None else records.student_id
    identity = dict(
        student_id=sid,
        catalog_revision=catalog.revision,
        records_revision=records.revision,
        requirements_revision=requirements.revision,
    )

    if best_gains is None:
        return HonorResult(
            eligible=False,
            honor_points=None,
            reason="graduation requirements cannot be met under this charter",
            states_explored=states_explored,
            **identity,
        )

    grad_assignments: List[Assignment] = []
    honor_sels: List[HonorSelection] = []
    cur = best_gains
    for i in range(n, 0, -1):
        hval, prev, action = layers[i][cur]
        if action is not None:
            rec = recs[i - 1]
            if action[0] == "honor":
                honor_sels.append(HonorSelection(rec.record_id, action[1], rec.points))
            else:
                _kind, cid, g = action
                grad_assignments.append(
                    Assignment(record_id=rec.record_id, course_id=cid, group_id=groups[g].group_id)
                )
        cur = prev  # type: ignore[assignment]
    grad_assignments.reverse()
    honor_sels.reverse()

    totals: Dict[str, int] = {g.group_id: 0 for g in groups}
    for a in grad_assignments:
        totals[a.group_id] = totals.get(a.group_id, 0) + records.records[a.record_id].points

    return HonorResult(
        eligible=True,
        honor_points=best_honor,
        graduation_assignments=grad_assignments,
        graduation_totals=totals,
        honor_selections=honor_sels,
        states_explored=states_explored,
        **identity,
    )


# --------------------------------------------------------------------------- #
# independent re-check of an honor result
# --------------------------------------------------------------------------- #
@dataclass
class HonorVerifyResult:
    accepted: bool
    reasons: List[dict] = field(default_factory=list)
    graduation_totals: Dict[str, int] = field(default_factory=dict)
    honor_points: int = 0

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "graduation_totals": dict(self.graduation_totals),
            "honor_points": self.honor_points,
        }


def _reason(code: str, message: str, location: Optional[str] = None) -> dict:
    r = {"code": code, "message": message}
    if location is not None:
        r["location"] = location
    return r


def verify_honor_result(
    result: HonorResult,
    catalog: Catalog,
    records: Records,
    requirements: Requirements,
    honor_categories: Sequence[str],
    *,
    fixed_choices: Optional[Mapping[str, str]] = None,
) -> HonorVerifyResult:
    """Recompute an honor result from the inputs without using the DP.

    Points always come from the records, never from the result's stated points.
    """
    if fixed_choices:
        catalog = apply_choices(catalog, fixed_choices)
    honor_cats = frozenset(honor_categories)
    reasons: List[dict] = []

    if result.student_id != records.student_id:
        reasons.append(_reason(
            "identity_mismatch",
            f"result student_id {result.student_id!r} != records student {records.student_id!r}",
            "student_id",
        ))
    if (result.catalog_revision, result.records_revision, result.requirements_revision) != (
        catalog.revision, records.revision, requirements.revision
    ):
        reasons.append(_reason("revision_mismatch", "result revisions do not match inputs", "revision"))

    grad_seen = set()
    grad_totals = {g.group_id: 0 for g in requirements.groups}
    for a in result.graduation_assignments:
        if a.record_id in grad_seen:
            reasons.append(_reason(
                "duplicate_record",
                f"record {a.record_id!r} used more than once for graduation",
                a.record_id,
            ))
            continue
        rec = records.records.get(a.record_id)
        if rec is None:
            reasons.append(_reason("unknown_record", f"unknown record {a.record_id!r}", a.record_id))
            grad_seen.add(a.record_id)
            continue
        grad_seen.add(a.record_id)
        group = requirements.group(a.group_id)
        if group is None:
            reasons.append(_reason("unknown_group", f"unknown group {a.group_id!r}", a.group_id))
            continue
        if a.course_id not in catalog.candidates(rec.external_course):
            reasons.append(_reason(
                "course_not_candidate",
                f"course {a.course_id!r} is not a candidate for {rec.external_course!r}",
                a.record_id,
            ))
            continue
        if not (set(catalog.courses[a.course_id].categories) & set(group.allowed_categories)):
            reasons.append(_reason(
                "category_mismatch",
                f"course {a.course_id!r} does not hit group {a.group_id!r}",
                a.record_id,
            ))
            continue
        grad_totals[a.group_id] += rec.points

    for g in requirements.groups:
        got = grad_totals.get(g.group_id, 0)
        if got < g.minimum_points:
            # An eligible claim must actually satisfy graduation; an ineligible
            # claim has no graduation allocation by definition, so this is not a
            # reason against it (the ineligibility itself is checked below).
            if result.eligible:
                reasons.append(_reason(
                    "group_underfilled",
                    f"group {g.group_id!r} has {got} < {g.minimum_points}",
                    g.group_id,
                ))

    honor_seen = set()
    honor_total = 0
    for sel in result.honor_selections:
        if sel.record_id in grad_seen:
            reasons.append(_reason(
                "record_shared",
                f"record {sel.record_id!r} is counted for both graduation and honors",
                sel.record_id,
            ))
        if sel.record_id in honor_seen:
            reasons.append(_reason(
                "duplicate_record",
                f"honor record {sel.record_id!r} appears more than once",
                sel.record_id,
            ))
        honor_seen.add(sel.record_id)
        rec = records.records.get(sel.record_id)
        if rec is None:
            reasons.append(_reason("unknown_record", f"unknown record {sel.record_id!r}", sel.record_id))
            continue
        if sel.course_id not in catalog.candidates(rec.external_course):
            reasons.append(_reason(
                "course_not_candidate",
                f"honor course {sel.course_id!r} is not a candidate for {rec.external_course!r}",
                sel.record_id,
            ))
            continue
        if not (set(catalog.courses[sel.course_id].categories) & honor_cats):
            reasons.append(_reason(
                "category_mismatch",
                f"honor course {sel.course_id!r} has no honor category",
                sel.record_id,
            ))
            continue
        # points come from the record, never the result's stated value
        honor_total += rec.points

    if result.eligible:
        if result.honor_points != honor_total:
            reasons.append(_reason(
                "honor_total_mismatch",
                f"reported honor points {result.honor_points} != recomputed {honor_total}",
                "honor_points",
            ))
        # A separate exact pass confirms the selection is actually optimal.
        optimal = evaluate_honor(catalog, records, requirements, honor_categories,
                                 student_id=result.student_id)
        if not optimal.eligible or optimal.honor_points != honor_total:
            reasons.append(_reason(
                "honor_not_optimal",
                f"recomputed honor {honor_total} is not the maximum "
                f"{optimal.honor_points if optimal.eligible else None}",
                "honor_points",
            ))
    else:
        # Ineligible claims are checked with the independent solver: graduation
        # must genuinely be impossible under this finalization.
        if result.honor_points is not None or result.honor_selections or result.graduation_assignments:
            reasons.append(_reason(
                "ineligible_has_award",
                "an ineligible result must carry no graduation or honor allocation",
            ))
        from .solver import solve as _solve

        if _solve(catalog, records, requirements).feasible:
            reasons.append(_reason(
                "should_be_eligible",
                "graduation is satisfiable yet the result claims ineligibility",
            ))

    return HonorVerifyResult(
        accepted=not reasons,
        reasons=reasons,
        graduation_totals=grad_totals,
        honor_points=honor_total,
    )


def graduation_plan(result: HonorResult, records: Records, requirements: Requirements) -> Plan:
    return Plan(
        student_id=result.student_id,
        catalog_revision=result.catalog_revision,
        records_revision=result.records_revision,
        request_id=requirements.request_id,
        requirements_revision=result.requirements_revision,
        assignments=tuple(result.graduation_assignments),
    )


def review_bundle(
    catalog: Catalog,
    records: Records,
    requirements: Requirements,
    honor_categories: Sequence[str],
    *,
    choices: Optional[Mapping[str, str]] = None,
) -> dict:
    """A directly reviewable bundle for one student under a finalization.

    The graduation allocation is re-checked by the ordinary independent
    verifier (no solver) and the honor selection by
    :func:`verify_honor_result`.  Revisions and student identity are pinned in
    the bundle, and a deterministic fingerprint covers the binding.
    """
    from .model import content_hash

    effective = apply_choices(catalog, choices) if choices else catalog
    result = evaluate_honor(effective, records, requirements, honor_categories)
    plan = graduation_plan(result, records, requirements)
    grad_verify: VerifyResult = verify_plan(plan, effective, records, requirements)
    honor_verify = verify_honor_result(
        result, effective, records, requirements, honor_categories
    )

    binding = {
        "student_id": records.student_id,
        "catalog_revision": effective.revision,
        "records_revision": records.revision,
        "requirements_revision": requirements.revision,
        "records_hash": records.content_hash(),
        "requirements_hash": requirements.content_hash(),
        "choices": {k: choices[k] for k in sorted(choices)} if choices else {},
    }
    return {
        "format": "honor-review/1",
        "identity": binding,
        "fingerprint": content_hash(binding),
        "charter_choices": binding["choices"],
        "eligible": result.eligible,
        "ineligible_reason": result.reason,
        "honor_points": result.honor_points,
        "graduation": {
            "plan": plan.as_document(),
            "totals": result.graduation_totals,
            "verified": grad_verify.accepted,
            "verification": grad_verify.as_dict(),
        },
        "honor": {
            "selections": [
                {"record_id": s.record_id, "course_id": s.course_id, "points": s.points}
                for s in result.honor_selections
            ],
            "verified": honor_verify.accepted,
            "verification": honor_verify.as_dict(),
        },
    }


__all__ = [
    "CharterDraft",
    "apply_choices",
    "HonorSelection",
    "HonorResult",
    "evaluate_honor",
    "verify_honor_result",
    "HonorVerifyResult",
    "graduation_plan",
    "review_bundle",
]
