"""Re-issue (改签) analysis: who must sign for the old credit destinations?

Baseline
--------
A re-issue starts from an *already confirmed* ``course-allocation/1`` plan (the
plan stored by the audit library's confirmation, not merely a draft
preparation).  The confirmed plan pins, for every used record, the exact
``record_id`` / ``course_id`` / ``group_id`` destination.  That pinned triple is
the thing a responsible advisor signs off on.

Rules modelled here
-------------------
* Every record **used** by the old confirmed plan has one responsible advisor
  (``advisor_of[record_id]``).
* Changing a used record's local course, changing its group, or dropping it to
  "unused" all require that advisor's consent.
* A consent set ``S`` is a set of advisors.  Records whose advisor is **not** in
  ``S`` keep their old destination verbatim; if that exact destination is no
  longer legal under the *new* catalog / requirements (course no longer a
  candidate, group gone, category no longer matching) then ``S`` is
  infeasible -- that advisor is forced to sign.
* Records that the old plan left **unused** need no consent to be activated;
  they are free to use under the new catalog / requirements.
* The new plan as a whole must pass the *new* revision's original independent
  verifier (:func:`gradaudit.verify.verify_plan`).

Output
------
All consent sets that are **inclusion-minimal** (feasible, but no proper subset
is feasible) together with one new plan per set.  If even *every* advisor
consenting cannot produce a valid plan, ``full_consent_feasible`` is ``False``
and ``note`` explains why (leveraging the exact solver's infeasibility report).

Advisors are set-valued: a name appearing on several records is listed once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from .errors import SchemaError
from .model import Assignment, Catalog, Plan, Records, Requirements, parse_plan
from .verify import verify_plan


# --------------------------------------------------------------------------- #
# result types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ReissueOption:
    """One inclusion-minimal consent set and one plan that realises it."""

    advisors: FrozenSet[str]
    plan: Plan

    @property
    def advisors_sorted(self) -> List[str]:
        return sorted(self.advisors)

    def as_dict(self) -> dict:
        return {
            "advisors": self.advisors_sorted,
            "plan": self.plan.as_document(),
        }


@dataclass
class ReissueResult:
    student_id: str
    request_id: str
    old_plan: dict
    options: List[ReissueOption] = field(default_factory=list)
    full_consent_feasible: bool = False
    all_advisors: List[str] = field(default_factory=list)
    note: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "student_id": self.student_id,
            "request_id": self.request_id,
            "old_plan": self.old_plan,
            "full_consent_feasible": self.full_consent_feasible,
            "all_advisors": list(self.all_advisors),
            "minimal_consent_sets": [o.as_dict() for o in self.options],
            "note": self.note,
        }


# --------------------------------------------------------------------------- #
# constrained exact feasibility
# --------------------------------------------------------------------------- #
def _constrain_plan(
    catalog: Catalog,
    records: Records,
    requirements: Requirements,
    fixed: Dict[str, Tuple[str, str]],
) -> Optional[Plan]:
    """Return one feasible plan honouring ``fixed`` destinations, or ``None``.

    ``fixed[record_id] = (course_id, group_id)`` records are forced to keep
    exactly that destination and must be used.  Every other record is free to
    take any legal (candidate course, matching group) pair or to stay unused.
    The DP mirrors :mod:`gradaudit.solver` (state ``(pointer, capped gains)``)
    but operates on per-record allowed destinations.
    """
    groups = list(requirements.groups)
    gids = [g.group_id for g in groups]
    group_index = {gid: i for i, gid in enumerate(gids)}
    minima = [g.minimum_points for g in groups]
    allowed = [set(g.allowed_categories) for g in groups]
    n_groups = len(groups)

    fixed_assignments: List[Assignment] = []
    fixed_gain = [0] * n_groups
    for rid, (cid, gid) in fixed.items():
        rec = records.records.get(rid)
        if rec is None:
            return None  # cannot preserve a destination for a vanished record
        if cid not in catalog.candidates(rec.external_course):
            return None  # old course is no longer a candidate under the new catalog
        course = catalog.courses.get(cid)
        if course is None:
            return None
        grp = requirements.group(gid)
        if grp is None:
            return None  # old group was removed by the new requirements
        if not (set(course.categories) & set(grp.allowed_categories)):
            return None  # category no longer feeds that group
        fixed_gain[group_index[gid]] += rec.points
        fixed_assignments.append(Assignment(rid, cid, gid))

    free_sorted = sorted(rid for rid in records.records if rid not in fixed)
    n = len(free_sorted)

    # Per free record and group: candidate course ids that would count there.
    value_options: List[List[Tuple[str, ...]]] = []
    for rid in free_sorted:
        rec = records.records[rid]
        cands = catalog.candidates(rec.external_course)
        per: List[Tuple[str, ...]] = []
        for g in range(n_groups):
            per.append(
                tuple(cid for cid in cands if allowed[g] & set(catalog.courses[cid].categories))
            )
        value_options.append(per)

    start = tuple(min(fixed_gain[i], minima[i]) for i in range(n_groups))
    layer: Dict[Tuple[int, ...], None] = {start: None}
    parents: Dict[Tuple, Tuple[Tuple, Optional[Tuple]]] = {}

    for pointer in range(n):
        nxt: Dict[Tuple[int, ...], Tuple[Tuple[int, ...], Optional[Tuple]]] = {}
        pts = records.records[free_sorted[pointer]].points
        for gains in layer:
            # Skipping a free record is always allowed.
            if gains not in nxt:
                nxt[gains] = (gains, None)
            for g in range(n_groups):
                opts = value_options[pointer][g]
                if not opts:
                    continue
                ng = min(gains[g] + pts, minima[g])
                if ng == gains[g]:
                    new_gains = gains
                else:
                    lst = list(gains)
                    lst[g] = ng
                    new_gains = tuple(lst)
                for cid in opts:
                    if new_gains not in nxt:
                        nxt[new_gains] = (gains, (pointer, cid, g))
        for new_gains, (prev_gains, action) in nxt.items():
            parents[(pointer + 1, new_gains)] = ((pointer, prev_gains), action)
        layer = {ng: None for ng in nxt}
        if not layer:
            break

    if n_groups == 0:
        return _finish_plan(fixed_assignments, [], records, requirements, catalog)

    final_pointer = n
    satisfied = [g for g in layer if all(g[i] >= minima[i] for i in range(n_groups))]
    if not satisfied:
        return None

    gains = min(satisfied, key=lambda gv: sum(gv))
    actions: List[Assignment] = []
    cur = (final_pointer, gains)
    while cur[0] > 0:
        link = parents.get(cur)
        if link is None:
            break
        prev_state, action = link
        if action is not None:
            pointer, cid, g = action
            actions.append(Assignment(free_sorted[pointer], cid, gids[g]))
        cur = prev_state

    return _finish_plan(fixed_assignments, actions, records, requirements, catalog)


def _finish_plan(
    fixed_assignments: Sequence[Assignment],
    free_actions: Sequence[Assignment],
    records: Records,
    requirements: Requirements,
    catalog: Catalog,
) -> Optional[Plan]:
    assignments = list(fixed_assignments) + list(free_actions)
    assignments.sort(key=lambda a: a.record_id)
    plan = Plan(
        student_id=records.student_id,
        catalog_revision=catalog.revision,
        records_revision=records.revision,
        request_id=requirements.request_id,
        requirements_revision=requirements.revision,
        assignments=tuple(assignments),
    )
    # Belt and braces: the plan returned to the caller is the one the original
    # independent verifier actually accepts.
    if not verify_plan(plan, catalog, records, requirements).accepted:
        return None
    return plan


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def _as_old_plan(old_plan) -> Plan:
    if isinstance(old_plan, Plan):
        return old_plan
    try:
        return parse_plan(old_plan)
    except Exception as exc:  # pragma: no cover - defensive
        raise SchemaError(f"old confirmed plan is not a valid course-allocation/1: {exc}") from exc


def _destination_still_legal(
    catalog: Catalog, records: Records, requirements: Requirements, a: Assignment
) -> bool:
    rec = records.records.get(a.record_id)
    if rec is None:
        return False
    if a.course_id not in catalog.candidates(rec.external_course):
        return False
    course = catalog.courses.get(a.course_id)
    grp = requirements.group(a.group_id)
    if course is None or grp is None:
        return False
    return bool(set(course.categories) & set(grp.allowed_categories))


def reissue_options(
    old_plan,
    catalog: Catalog,
    records: Records,
    requirements: Requirements,
    advisor_of: Dict[str, str],
) -> ReissueResult:
    """Enumerate inclusion-minimal consenting-advisor sets for one student.

    ``old_plan`` is the confirmed ``course-allocation/1`` document (dict) or a
    parsed :class:`~gradaudit.model.Plan`.  ``advisor_of`` maps every used
    record id to its responsible advisor.  The new catalog / records /
    requirements are the revisions in force now.
    """
    old = _as_old_plan(old_plan)
    if old.student_id != records.student_id:
        raise SchemaError(
            f"old plan student {old.student_id!r} != records student "
            f"{records.student_id!r}; re-issue must keep the confirmed identity",
            location="student_id",
        )
    if old.request_id != requirements.request_id:
        raise SchemaError(
            f"old plan request {old.request_id!r} != requirements request "
            f"{requirements.request_id!r}; re-issue must keep the confirmed identity",
            location="request_id",
        )

    used: Dict[str, Assignment] = {}
    for a in old.assignments:
        if a.record_id in used:
            raise SchemaError(
                f"old confirmed plan lists record {a.record_id!r} more than once; "
                f"a record has one confirmed destination",
                location=a.record_id,
            )
        used[a.record_id] = a

    for rid in used:
        advisor = advisor_of.get(rid)
        if not isinstance(advisor, str) or not advisor.strip():
            raise SchemaError(
                f"used record {rid!r} has no responsible advisor in advisor_of",
                location=rid,
            )

    advisors = frozenset(advisor_of[rid] for rid in used)

    # Advisors whose record can no longer keep its old destination must sign:
    # without consent the record is pinned to an illegal destination.
    forced = {
        advisor_of[rid]
        for rid, a in used.items()
        if not _destination_still_legal(catalog, records, requirements, a)
    }
    nonforced = sorted(advisors - forced)
    m = len(nonforced)

    # feasibility is monotone in the consent set (adding consent only frees a
    # record), so scanning masks by size and skipping supersets of a known
    # minimal set yields exactly the inclusion-minimal feasible sets.
    minimal_masks: List[int] = []
    options: List[ReissueOption] = []
    for mask in sorted(range(1 << m), key=lambda x: (x.bit_count(), x)):
        if any((mm & mask) == mm for mm in minimal_masks):
            continue  # superset of an already-minimal feasible set
        consent = set(forced)
        for i in range(m):
            if (mask >> i) & 1:
                consent.add(nonforced[i])
        fixed = {
            rid: (a.course_id, a.group_id)
            for rid, a in used.items()
            if advisor_of[rid] not in consent
        }
        plan = _constrain_plan(catalog, records, requirements, fixed)
        if plan is not None:
            minimal_masks.append(mask)
            options.append(ReissueOption(frozenset(consent), plan))

    result = ReissueResult(
        student_id=records.student_id,
        request_id=requirements.request_id,
        old_plan=old.as_document(),
        options=options,
        full_consent_feasible=bool(options),
        all_advisors=sorted(advisors),
        note=None,
    )

    if not options:
        result.note = _infeasibility_note(catalog, records, requirements)
    return result


def _infeasibility_note(catalog: Catalog, records: Records, requirements: Requirements) -> str:
    from .solver import solve  # local import: only needed for the diagnostic

    diag = solve(catalog, records, requirements)
    maxima = ", ".join(
        f"{g.group_id}: can reach {g.max_ignoring_other_groups} (needs {g.minimum_points})"
        for g in diag.groups
    )
    return (
        "even with every responsible advisor consenting, no plan satisfies the new "
        f"requirements under the new catalog/records. {maxima}."
    )


__all__ = [
    "ReissueOption",
    "ReissueResult",
    "reissue_options",
]
