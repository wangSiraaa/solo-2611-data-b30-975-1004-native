"""Scholarship determination over a not-yet-finalized charter.

The scholarship office collects one honor score per student (see
:mod:`gradaudit.honor`), a quota, and a fixed tie-break order.  While the
charter is still a draft it has several *legal finalizations* (one local course
per covered code, each a catalog candidate).  For each student we answer whether
they

* always win -- in **every** legal finalization (``always``),
* win only in **some** finalizations (``some``),
* never win -- ``never``.

A ``some`` conclusion is a genuine change of outcome, so we also return two
*cohort-consistent* witness finalizations -- the **same** finalization applied to
the whole cohort -- each with the actual award list computed under it.  We never
combine one student's score from one finalization with another's from a
different finalization to build a ranking.

The award list handed out is always issued under a single deterministic
finalization (:func:`canonical_finalization`).  Trial assessments are pure: they
never prepare or confirm anything in the audit library, so existing graduation
confirmations are unaffected.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .errors import SchemaError
from .honor import CharterDraft, evaluate_honor
from .model import Catalog, Records, Requirements

# Guard rails from the task's stated scale (12 codes, <=3 candidates, 40 people).
MAX_CODES = 12
MAX_CANDIDATES = 3
MAX_STUDENTS = 40


@dataclass(frozen=True)
class StudentEntry:
    """One student's pinned inputs: identity, records revision, requirements."""

    student_id: str
    records: Records
    requirements: Requirements

    def __post_init__(self) -> None:
        if self.records.student_id != self.student_id:
            raise SchemaError(
                f"entry student_id {self.student_id!r} != records student "
                f"{self.records.student_id!r}",
                location="student_id",
            )


def canonical_finalization(draft: CharterDraft) -> Dict[str, str]:
    """The deterministic finalization used to issue the award list.

    Covered codes in sorted order, each taking its first catalog candidate.
    """
    return {code: draft.candidates[code][0] for code in sorted(draft.covered)}


def _normalise_tie_order(entries: Sequence[StudentEntry], tie_order: Sequence[str]) -> List[StudentEntry]:
    ids = {e.student_id for e in entries}
    if len(ids) != len(entries):
        raise SchemaError("duplicate student_id in cohort", location="cohort")
    seen: Set[str] = set()
    order: List[StudentEntry] = []
    for sid in tie_order:
        if sid not in ids:
            raise SchemaError(f"tie order names unknown student {sid!r}", location=sid)
        if sid in seen:
            raise SchemaError(f"tie order repeats student {sid!r}", location=sid)
        seen.add(sid)
        order.append(next(e for e in entries if e.student_id == sid))
    for e in sorted(entries, key=lambda x: x.student_id):
        if e.student_id not in seen:
            order.append(e)
    return order


def _winner_indices(points: Sequence[Optional[int]], quota: int) -> List[int]:
    """Rank by honor points desc, then fixed tie order (already-normalised).

    Ineligible students (``None`` points) can never win.
    """
    ranked = [i for i, p in enumerate(points) if p is not None]
    ranked.sort(key=lambda i: (-points[i], i))  # type: ignore[operator]
    return ranked[:quota]


def _per_student_scores(
    catalog: Catalog,
    students: Sequence[StudentEntry],
    honor_categories: Sequence[str],
    choices: Dict[str, str],
) -> Tuple[List[Optional[int]], Dict[str, dict]]:
    points: List[Optional[int]] = []
    detail: Dict[str, dict] = {}
    for e in students:
        relevant = {c: choices[c] for c in choices
                    if c in {r.external_course for r in e.records.records.values()}}
        res = evaluate_honor(
            catalog, e.records, e.requirements, honor_categories, fixed_choices=relevant
        )
        points.append(res.honor_points if res.eligible else None)
        detail[e.student_id] = {
            "eligible": res.eligible,
            "honor_points": res.honor_points,
            "graduation_totals": res.graduation_totals,
            "honor_selections": [
                {"record_id": s.record_id, "course_id": s.course_id, "points": s.points}
                for s in res.honor_selections
            ],
        }
    return points, detail


def _snapshot(
    catalog: Catalog,
    students: Sequence[StudentEntry],
    honor_categories: Sequence[str],
    choices: Dict[str, str],
    quota: int,
) -> dict:
    points, detail = _per_student_scores(catalog, students, honor_categories, choices)
    winners = _winner_indices(points, quota)
    return {
        "finalization": dict(choices),
        "award_list": [
            {"student_id": students[i].student_id, "honor_points": points[i]}
            for i in winners
        ],
        "award_students": [students[i].student_id for i in winners],
        "per_student": detail,
    }


def assess_scholarship(
    catalog: Catalog,
    entries: Sequence[StudentEntry],
    honor_categories: Sequence[str],
    draft: CharterDraft,
    *,
    quota: int,
    tie_order: Sequence[str],
) -> dict:
    """Assess every student across all legal finalizations of ``draft``.

    ``catalog`` is the enrolled catalog; ``draft`` restricts covered codes to a
    single candidate each.  Returns a report with per-student status and, for
    conditional students, two cohort-consistent witness finalizations.
    """
    if not isinstance(quota, int) or isinstance(quota, bool) or quota < 1:
        raise SchemaError("quota must be a positive integer", location="quota")
    if not entries:
        raise SchemaError("cohort must be non-empty", location="cohort")
    if len(entries) > MAX_STUDENTS:
        raise SchemaError(
            f"at most {MAX_STUDENTS} students are supported (got {len(entries)})",
            location="cohort",
        )
    if len(draft.covered) > MAX_CODES:
        raise SchemaError(
            f"at most {MAX_CODES} covered codes are supported (got {len(draft.covered)})",
            location="covered_codes",
        )
    for code in draft.covered:
        if len(draft.candidates[code]) > MAX_CANDIDATES:
            raise SchemaError(
                f"code {code!r} has {len(draft.candidates[code])} candidates; at most "
                f"{MAX_CANDIDATES} are supported",
                location=code,
            )

    students = _normalise_tie_order(entries, tie_order)  # tie order == index order
    n = len(students)

    covered = sorted(draft.covered)
    # Only covered codes that actually occur in some record can change a score.
    used_codes: Set[str] = set()
    for e in students:
        used_codes.update(r.external_course for r in e.records.records.values())
    active = [c for c in covered if c in used_codes]

    # Per-student relevant active-code positions, for caching.
    entry_relevant = [
        tuple(pos for pos, c in enumerate(active)
              if c in {r.external_course for r in e.records.records.values()})
        for e in students
    ]
    caches: List[Dict[Tuple[int, ...], Optional[int]]] = [dict() for _ in range(n)]

    def score(i: int, combo: Tuple[int, ...]) -> Optional[int]:
        rel = entry_relevant[i]
        key = tuple(combo[p] for p in rel)
        if key in caches[i]:
            return caches[i][key]
        fixed = {active[p]: draft.candidates[active[p]][combo[p]] for p in rel}
        res = evaluate_honor(
            catalog, students[i].records, students[i].requirements, honor_categories,
            fixed_choices=fixed,
        )
        val = res.honor_points if res.eligible else None
        caches[i][key] = val
        return val

    canonical = canonical_finalization(draft)
    canonical_combo = tuple(
        draft.candidates[c].index(canonical[c]) for c in active
    )

    # Enumerate the active finalizations only; inactive covered codes are
    # equivalent everywhere, so they use their canonical choice.
    option_ranges = [range(len(draft.candidates[c])) for c in active]
    seen_win = [False] * n
    seen_lose = [False] * n
    first_win: List[Optional[Tuple[int, ...]]] = [None] * n
    first_lose_ok: List[Optional[Tuple[int, ...]]] = [None] * n
    first_lose_any: List[Optional[Tuple[int, ...]]] = [None] * n

    active_count = 0
    for combo in itertools.product(*option_ranges):
        active_count += 1
        pts = [score(i, combo) for i in range(n)]
        winners = set(_winner_indices(pts, quota))
        for i in range(n):
            if i in winners:
                seen_win[i] = True
                if first_win[i] is None:
                    first_win[i] = combo
            else:
                seen_lose[i] = True
                if first_lose_any[i] is None:
                    first_lose_any[i] = combo
                if pts[i] is not None and first_lose_ok[i] is None:
                    first_lose_ok[i] = combo

    def full_choices(combo: Tuple[int, ...]) -> Dict[str, str]:
        out = dict(canonical)
        for pos, c in enumerate(active):
            out[c] = draft.candidates[c][combo[pos]]
        return out

    canonical_pts = [score(i, canonical_combo) for i in range(n)]
    canonical_winners = set(_winner_indices(canonical_pts, quota))

    reports = []
    for i, e in enumerate(students):
        if seen_win[i] and seen_lose[i]:
            status, text = "some", "wins in some but not all legal finalizations"
        elif seen_win[i]:
            status, text = "always", "wins in every legal finalization"
        else:
            status, text = "never", "never wins in any legal finalization"
        rep = {
            "student_id": e.student_id,
            "status": status,
            "status_text": text,
            "records_revision": e.records.revision,
            "records_hash": e.records.content_hash(),
            "requirements_revision": e.requirements.revision,
            "requirements_hash": e.requirements.content_hash(),
            "canonical_honor_points": canonical_pts[i],
            "canonical_eligible": canonical_pts[i] is not None,
            "canonical_wins": i in canonical_winners,
        }
        if status == "some":
            win_combo = first_win[i]
            lose_combo = first_lose_ok[i] if first_lose_ok[i] is not None else first_lose_any[i]
            assert win_combo is not None and lose_combo is not None
            rep["witness"] = {
                "outcome_changes": True,
                "available_under": _snapshot(
                    catalog, students, honor_categories, full_choices(win_combo), quota
                ),
                "not_available_under": _snapshot(
                    catalog, students, honor_categories, full_choices(lose_combo), quota
                ),
            }
        reports.append(rep)

    legal_total = 1
    for c in covered:
        legal_total *= len(draft.candidates[c])

    return {
        "format": "scholarship-assessment/1",
        "quota": quota,
        "tie_order": [e.student_id for e in students],
        "covered_codes": covered,
        "legal_finalizations": legal_total,
        "evaluated_active_finalizations": active_count,
        "canonical_finalization": canonical,
        "canonical_award_list": [
            {"student_id": students[i].student_id, "honor_points": canonical_pts[i]}
            for i in _winner_indices(canonical_pts, quota)
        ],
        "students": reports,
        "note": (
            "Trial assessments are read-only; they never prepare or confirm "
            "anything in the audit library, so existing graduation confirmations "
            "are unaffected. Each witness award list is computed under a single "
            "whole-cohort finalization; scores from different finalizations are "
            "never mixed."
        ),
    }


__all__ = [
    "StudentEntry",
    "canonical_finalization",
    "assess_scholarship",
]
