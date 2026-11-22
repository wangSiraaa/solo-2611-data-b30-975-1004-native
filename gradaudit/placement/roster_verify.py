"""Independent verifier for an ``internship-roster/1`` roster.

It does not import :mod:`gradaudit.placement.matching`; every fact is
re-derived from the station profile, preferences, subsidies and the approved
review documents:

* assignments respect quotas, the closed flag, mutual listing and approvals;
* subsidies are recomputed from the subsidy document (roster money is ignored);
* the unassigned list is exactly the students without an assignment;
* every blocking pair is enumerated independently;
* optimality is re-checked with this module's own exhaustive stable search, so
  a stable-but-suboptimal roster (e.g. the first stable roster someone signed)
  is located as ``roster_not_cheapest`` / ``roster_not_maximal``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .documents import Station


@dataclass
class RosterVerifyResult:
    accepted: bool
    stable: bool
    placements: int
    recomputed_subsidy: int
    reasons: List[dict] = field(default_factory=list)
    blocking_pairs: List[dict] = field(default_factory=list)
    optimum: Optional[dict] = None

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "stable": self.stable,
            "placements": self.placements,
            "recomputed_subsidy": self.recomputed_subsidy,
            "optimum": self.optimum,
            "blocking_pairs": list(self.blocking_pairs),
            "reasons": list(self.reasons),
        }


def _reason(code: str, message: str, *, location: Optional[str] = None, detail=None) -> dict:
    r = {"code": code, "message": message}
    if location is not None:
        r["location"] = location
    if detail is not None:
        r["detail"] = detail
    return r


def _approved_pairs(reviews: Sequence[dict]) -> Dict[str, frozenset]:
    approved: Dict[str, set] = {}
    for review in reviews:
        if review.get("decision") == "eligible":
            approved.setdefault(review["student_id"], set()).add(review["post_id"])
    return {s: frozenset(t) for s, t in approved.items()}


def _blocking_pairs(
    assignment: Dict[str, Optional[str]],
    preferences: Dict[str, Tuple[str, ...]],
    stations: Dict[str, Station],
    edges: Dict[str, Tuple[str, ...]],
    admits: Dict[str, List[str]],
) -> List[dict]:
    rank_of = {sid: {student: i for i, student in enumerate(s.ranking)}
               for sid, s in stations.items()}
    blockers: List[dict] = []
    for student, wish in preferences.items():
        current = assignment.get(student)
        current_rank = wish.index(current) if current is not None and current in wish else len(wish)
        for station_id in wish[:current_rank]:
            if station_id not in edges.get(student, ()):
                continue
            station = stations[station_id]
            if station.quota == 0 or station.closed:
                continue
            if len(admits[station_id]) < station.quota:
                blockers.append({
                    "student_id": student,
                    "station_id": station_id,
                    "reason": "station_has_free_seat",
                    "current_station_id": current,
                })
            else:
                worst = max(admits[station_id], key=lambda s: rank_of[station_id][s])
                if rank_of[station_id][student] < rank_of[station_id][worst]:
                    blockers.append({
                        "student_id": student,
                        "station_id": station_id,
                        "reason": "station_prefers_student_over_weakest_admit",
                        "current_station_id": current,
                        "displaced_student_id": worst,
                    })
    return blockers


def _optimal_stable(
    preferences: Dict[str, Tuple[str, ...]],
    stations: Dict[str, Station],
    edges: Dict[str, Tuple[str, ...]],
    subsidies: Dict[str, Dict[str, int]],
) -> Tuple[int, int]:
    """Independent exhaustive search -> (max placements, min subsidy at max)."""
    students = sorted(preferences)
    total_seats = sum(s.quota for s in stations.values() if not s.closed)
    admits: Dict[str, List[str]] = {sid: [] for sid in stations}
    assignment: Dict[str, Optional[str]] = {}
    best = {"placed": -1, "cost": 0}

    def dfs(pending: List[str], placed: int, cost: int) -> None:
        free = total_seats - sum(len(v) for v in admits.values())
        if placed + min(len(pending), free) < best["placed"]:
            return
        if not pending:
            if _blocking_pairs(assignment, preferences, stations, edges, admits):
                return
            key = (placed, cost)
            if best["placed"] < 0 or key < (best["placed"], best["cost"]):
                best["placed"], best["cost"] = placed, cost
            return
        student = pending[0]
        rest = pending[1:]
        for station_id in list(edges.get(student, ())) + [None]:
            if station_id is None:
                assignment[student] = None
                dfs(rest, placed, cost)
                del assignment[student]
                continue
            if len(admits[station_id]) >= stations[station_id].quota:
                continue
            admits[station_id].append(student)
            assignment[student] = station_id
            dfs(rest, placed + 1, cost + subsidies[station_id][student])
            admits[station_id].remove(student)
            del assignment[student]

    dfs(students, 0, 0)
    return best["placed"], best["cost"]


def verify_roster(
    roster_doc: dict,
    preferences: Dict[str, Tuple[str, ...]],
    stations: Dict[str, Station],
    subsidies: Dict[str, Dict[str, int]],
    reviews: Sequence[dict],
) -> RosterVerifyResult:
    reasons: List[dict] = []
    approved = _approved_pairs(reviews)

    edges: Dict[str, Tuple[str, ...]] = {}
    for student, wish in preferences.items():
        ok = []
        for station_id in wish:
            station = stations.get(station_id)
            if station is None or station.closed:
                continue
            if student not in station.ranking:
                continue
            if station_id not in approved.get(student, frozenset()):
                continue
            ok.append(station_id)
        edges[student] = tuple(ok)

    raw_assignments = roster_doc.get("assignments", [])
    assignment: Dict[str, Optional[str]] = {s: None for s in preferences}
    seen_students: set = set()
    admits: Dict[str, List[str]] = {sid: [] for sid in stations}
    recomputed = 0

    for idx, entry in enumerate(raw_assignments):
        loc = f"assignments[{idx}]"
        student, station_id = entry.get("student_id"), entry.get("station_id")
        if student not in preferences:
            reasons.append(_reason(
                "roster_unknown_student", f"student {student!r} has no preference document",
                location=loc,
            ))
            continue
        if student in seen_students:
            reasons.append(_reason(
                "roster_student_assigned_twice", f"student {student!r} appears more than once",
                location=student,
            ))
            continue
        seen_students.add(student)
        station = stations.get(station_id)
        if station is None:
            reasons.append(_reason(
                "roster_unknown_station", f"station {station_id!r} is not in the station profile",
                location=loc,
            ))
            continue
        if station.closed:
            reasons.append(_reason(
                "roster_closed_station_used",
                f"station {station_id!r} has stopped recruiting", location=station_id,
            ))
        if student not in station.ranking:
            reasons.append(_reason(
                "roster_station_does_not_list_student",
                f"station {station_id!r} does not rank student {student!r}", location=station_id,
            ))
        if station_id not in preferences[student]:
            reasons.append(_reason(
                "roster_student_does_not_list_station",
                f"student {student!r} does not wish for station {station_id!r}", location=student,
            ))
        if station_id not in approved.get(student, frozenset()):
            reasons.append(_reason(
                "roster_pair_not_approved",
                f"pair ({student!r}, {station_id!r}) has no eligible department review",
                location=loc,
            ))
        amount = subsidies.get(station_id, {}).get(student)
        if amount is None:
            reasons.append(_reason(
                "roster_missing_subsidy",
                f"no subsidy recorded for pair ({student!r}, {station_id!r})", location=loc,
            ))
        else:
            recomputed += amount
        if len(admits[station_id]) >= station.quota:
            reasons.append(_reason(
                "roster_quota_exceeded",
                f"station {station_id!r} quota {station.quota} exceeded",
                location=station_id, detail={"quota": station.quota},
            ))
        admits[station_id].append(student)
        assignment[student] = station_id

    declared_unassigned = set(roster_doc.get("unassigned", []))
    actual_unassigned = {s for s, v in assignment.items() if v is None}
    if declared_unassigned != actual_unassigned:
        reasons.append(_reason(
            "roster_unassigned_mismatch",
            "the unassigned list does not match the students without an assignment",
            location="unassigned",
            detail={"declared": sorted(declared_unassigned), "actual": sorted(actual_unassigned)},
        ))

    placements = sum(1 for v in assignment.values() if v is not None)
    declared_total = roster_doc.get("objective", {}).get("total_subsidy")
    if declared_total != recomputed:
        reasons.append(_reason(
            "roster_subsidy_mismatch",
            "the roster's total subsidy disagrees with the subsidy document",
            location="objective.total_subsidy",
            detail={"declared": declared_total, "recomputed": recomputed},
        ))

    blockers = _blocking_pairs(assignment, preferences, stations, edges, admits)
    for b in blockers:
        reasons.append(_reason(
            "roster_blocking_pair",
            f"student {b['student_id']!r} and station {b['station_id']!r} would privately "
            f"re-contract ({b['reason']})",
            location=b["student_id"],
            detail=b,
        ))

    optimum = None
    if not reasons:
        max_placed, min_cost = _optimal_stable(preferences, stations, edges, subsidies)
        optimum = {"max_placements": max_placed, "minimum_subsidy_at_max": min_cost}
        if placements < max_placed:
            reasons.append(_reason(
                "roster_not_maximal",
                f"roster places {placements} students but a stable roster places {max_placed}",
                detail={"placements": placements, "max_placements": max_placed},
            ))
        elif recomputed > min_cost:
            reasons.append(_reason(
                "roster_not_cheapest",
                f"roster subsidy {recomputed} is not minimal among rosters placing {placements}; "
                f"minimum is {min_cost} -- the first stable roster was taken as cheapest",
                detail={"subsidy": recomputed, "minimum_subsidy": min_cost},
            ))

    return RosterVerifyResult(
        accepted=not reasons,
        stable=not blockers,
        placements=placements,
        recomputed_subsidy=recomputed,
        reasons=reasons,
        blocking_pairs=blockers,
        optimum=optimum,
    )


__all__ = ["RosterVerifyResult", "verify_roster"]
