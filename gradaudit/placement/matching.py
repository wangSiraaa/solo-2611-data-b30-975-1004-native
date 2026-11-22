"""Exact stable matching for internship stations.

Inputs (all explicit, nothing guessed):

* each student's strict wish order over stations;
* each station's strict reception order (ranking), its quota and closed flag;
* the set of department-approved pairs (an ``eligible`` review per pair);
* a commute subsidy per (station, student) pair.

A pair is *admissible* only when the student lists the station, the station
lists the student, the station is not closed, and the department approved the
pair.  Unassigned students are placed after every acceptable destination.

Optimisation order, exactly as contracted:

1. place as many students as possible;
2. among rosters placing that many, minimise total commute subsidy;
3. among ties, take the lexicographically smallest assignment (determinism).

A roster is signable only when it has **no blocking pair**: a student who
prefers a station to their current destination while that station either has a
free seat or ranks the student ahead of one of its current admits.  Because the
optimum is searched over stable rosters only, the first stable roster found is
never returned on its own -- the search is exhaustive under the lexicographic
objective.  Multiple stable rosters are allowed.

Algorithm
---------
1. Student-proposing and station-proposing deferred acceptance bound the
   *stable lattice*: a pair appears in some stable matching exactly when the
   station it names lies between the two DA endpoints on the student's wish
   list.  The search restricts itself to these lattice edges, which removes
   every pair no stable roster can use and collapses dense instances.
2. The student-proposing result also gives the maximum placement count (all
   stable matchings match the same students), so the DFS only minimises cost
   among full-size stable rosters.
3. A memoised depth-first search walks the lattice with permanent-block and
   unassigned-reachability pruning and a deterministic lexicographic tie break.
   Stability is independently re-checked at every leaf and again by
   :mod:`gradaudit.placement.roster_verify`, which performs its own exhaustive
   optimum recomputation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..errors import SchemaError
from .documents import MAX_STATION_PICKS, Station


@dataclass
class MatchingInput:
    preferences: Dict[str, Tuple[str, ...]]
    stations: Dict[str, Station]
    approved: Dict[str, frozenset]          # student_id -> {station_id, ...}
    subsidies: Dict[str, Dict[str, int]]    # station_id -> {student_id: amount}

    def admissible_edges(self) -> Dict[str, Tuple[str, ...]]:
        edges: Dict[str, Tuple[str, ...]] = {}
        for student, wish in self.preferences.items():
            ok: List[str] = []
            for station_id in wish:
                station = self.stations.get(station_id)
                if station is None or station.closed:
                    continue
                if student not in station.ranking:
                    continue
                if station_id not in self.approved.get(student, frozenset()):
                    continue
                ok.append(station_id)
            edges[student] = tuple(ok)
        return edges


def _rank_maps(stations: Dict[str, Station]) -> Dict[str, Dict[str, int]]:
    return {sid: {student: i for i, student in enumerate(s.ranking)}
            for sid, s in stations.items()}


def build_matching_input(
    preferences: Dict[str, Tuple[str, ...]],
    stations: Dict[str, Station],
    reviews: Sequence[dict],
    subsidies: Dict[str, Dict[str, int]],
) -> MatchingInput:
    """Validate the four sources and assemble the matching input.

    ``reviews`` are raw ``internship-review/1`` documents; only accepted
    ``eligible`` decisions make a pair available.
    """
    approved: Dict[str, set] = {}
    for review in reviews:
        if review.get("decision") != "eligible":
            continue
        student, post = review["student_id"], review["post_id"]
        approved.setdefault(student, set()).add(post)

    # A station id and a post id name the same destination; the review's post
    # must therefore correspond to a known, open station to be usable.
    for student, targets in approved.items():
        if student not in preferences:
            raise SchemaError(
                f"approved student {student!r} has no preference list", location=student
            )
        for station_id in targets:
            if station_id not in stations:
                raise SchemaError(
                    f"approved pair ({student!r}, {station_id!r}) names an unknown station",
                    location=station_id,
                )

    for student, wish in preferences.items():
        if len(wish) > MAX_STATION_PICKS:
            raise SchemaError(
                f"student {student!r} lists more than {MAX_STATION_PICKS} stations", location=student
            )
        for station_id in wish:
            station = stations.get(station_id)
            if station is None:
                raise SchemaError(
                    f"student {student!r} prefers unknown station {station_id!r}", location=station_id
                )
            if student not in station.ranking:
                raise SchemaError(
                    f"student {student!r} lists station {station_id!r} which does not list them back",
                    location=station_id,
                )
            if student not in subsidies.get(station_id, {}):
                raise SchemaError(
                    f"missing commute subsidy for pair ({student!r}, {station_id!r})",
                    location=station_id,
                )

    for station in stations.values():
        for student in station.ranking:
            if student not in preferences:
                raise SchemaError(
                    f"station {station.station_id!r} ranks unknown student {student!r}",
                    location=station.station_id,
                )

    return MatchingInput(
        preferences=dict(preferences),
        stations=dict(stations),
        approved={s: frozenset(t) for s, t in approved.items()},
        subsidies={sid: dict(table) for sid, table in subsidies.items()},
    )


# --------------------------------------------------------------------------- #
# stability
# --------------------------------------------------------------------------- #
def deferred_acceptance(inp: MatchingInput, edges: Dict[str, Tuple[str, ...]]
                        ) -> Dict[str, Optional[str]]:
    """Student-proposing deferred acceptance over the admissible edges.

    Returns a stable matching.  Under the college-admissions model every stable
    matching has the same matched-student set, so its placement count is the
    maximum the DFS needs to beat; its cost is the initial branch-and-bound.
    """
    rank_of = _rank_maps(inp.stations)
    proposals: Dict[str, int] = {s: 0 for s in inp.preferences}
    free = set(inp.preferences)
    held: Dict[str, List[str]] = {sid: [] for sid in inp.stations}
    assignment: Dict[str, Optional[str]] = {s: None for s in inp.preferences}
    while free:
        student = next(iter(free))
        wish = edges.get(student, ())
        if proposals[student] >= len(wish):
            free.remove(student)            # exhausted every acceptable station
            assignment[student] = None
            continue
        station_id = wish[proposals[student]]
        proposals[student] += 1
        station = inp.stations[station_id]
        held[station_id].append(student)
        # station keeps its best-ranked admits up to quota, rejects the rest
        held[station_id].sort(key=lambda s: rank_of[station_id][s])
        for rejected in held[station_id][station.quota:]:
            held[station_id].remove(rejected)
            free.add(rejected)
            assignment[rejected] = None
        if student in held[station_id]:
            free.discard(student)
            assignment[student] = station_id
    return assignment


def station_deferred_acceptance(inp: MatchingInput, edges: Dict[str, Tuple[str, ...]]
                                ) -> Dict[str, Optional[str]]:
    """Station-proposing deferred acceptance (the other end of the lattice).

    Stations with free seats propose to their best not-yet-considered ranked,
    admissible students; a student holds the proposal from the station they
    prefer and rejects the rest.  Together with student-proposing DA this pins
    down every pair that can occur in *some* stable matching.
    """
    rank_of = _rank_maps(inp.stations)
    # next student index each station will propose to
    next_prop: Dict[str, int] = {sid: 0 for sid in inp.stations}
    # station -> currently held students
    held: Dict[str, List[str]] = {sid: [] for sid in inp.stations}
    # student -> station currently holding them (or None)
    holding: Dict[str, Optional[str]] = {s: None for s in inp.preferences}
    open_stations = {sid for sid, st in inp.stations.items()
                     if not st.closed and st.quota > 0}

    def admissible(sid: str, student: str) -> bool:
        return sid in edges.get(student, ())

    while open_stations:
        station_id = next(iter(open_stations))
        station = inp.stations[station_id]
        if len(held[station_id]) >= station.quota:
            open_stations.discard(station_id)
            continue
        ranking = station.ranking
        while next_prop[station_id] < len(ranking):
            student = ranking[next_prop[station_id]]
            next_prop[station_id] += 1
            if not admissible(station_id, student):
                continue
            current = holding[student]
            if current is None:
                holding[student] = station_id
                held[station_id].append(student)
                break
            # student prefers whichever station appears earlier in their wish
            wish = inp.preferences[student]
            if wish.index(station_id) < wish.index(current):
                held[current].remove(student)
                open_stations.add(current)
                holding[student] = station_id
                held[station_id].append(student)
                break
        else:
            open_stations.discard(station_id)

    return {s: sid for s, sid in holding.items()}


def find_blocking_pairs(
    assignment: Dict[str, Optional[str]],
    inp: MatchingInput,
    edges: Dict[str, Tuple[str, ...]],
    rank_of: Dict[str, Dict[str, int]],
    admits: Dict[str, List[str]],
) -> List[dict]:
    """Every (student, station) pair willing to privately re-contract."""
    blockers: List[dict] = []
    for student, wish in inp.preferences.items():
        current = assignment.get(student)
        current_rank = wish.index(current) if current is not None and current in wish else len(wish)
        for station_id in wish[:current_rank]:
            if station_id not in edges.get(student, ()):
                continue  # not admissible: closed, not mutually listed or not approved
            station = inp.stations[station_id]
            rank = rank_of[station_id]
            if station.quota == 0 or station.closed:
                continue  # a stopped / zero-seat station can never take anyone
            # free seat: the station takes any ranked student while under quota
            if len(admits[station_id]) < station.quota:
                blockers.append({
                    "student_id": student,
                    "station_id": station_id,
                    "reason": "station_has_free_seat",
                    "current_station_id": current,
                })
            else:
                # or the station ranks this student above its worst current admit
                worst = max(admits[station_id], key=lambda s: rank[s])
                if rank[student] < rank[worst]:
                    blockers.append({
                        "student_id": student,
                        "station_id": station_id,
                        "reason": "station_prefers_student_over_weakest_admit",
                        "current_station_id": current,
                        "displaced_student_id": worst,
                    })
    return blockers


def has_permanent_block(
    decided: Sequence[str],
    assignment: Dict[str, Optional[str]],
    inp: MatchingInput,
    edges: Dict[str, Tuple[str, ...]],
    rank_of: Dict[str, Dict[str, int]],
    admits: Dict[str, List[str]],
) -> bool:
    """A blocking pair no later decision can dissolve, restricted soundly.

    Only *decided and currently unassigned* students are inspected: once a
    student is seated, the check made at their seating time guarantees their
    preferred stations are unreachable, and later seats filling cannot make a
    reachable station reachable again.  For an unassigned decided student a
    block is permanent when any admissible station is reachable for them --
    a free seat, or a full seat where they outrank the weakest current admit.
    Their own decision is final, so such a pair can only harden.
    """
    for student in decided:
        if assignment.get(student) is not None:
            continue
        for station_id in edges.get(student, ()):
            station = inp.stations[station_id]
            held = admits[station_id]
            if len(held) < station.quota:
                return True
            rank = rank_of[station_id]
            if any(rank[student] < rank[incumbent] for incumbent in held):
                return True
    return False


def unassigned_is_stable_possible(
    student: str,
    inp: MatchingInput,
    edges: Dict[str, Tuple[str, ...]],
    rank_of: Dict[str, Dict[str, int]],
    admits: Dict[str, List[str]],
) -> bool:
    """Can this student stay unassigned without a blocking pair right now?

    Yes only if *every* admissible station on their wish list is unreachable
    for them: full with admits the station strictly prefers.  A single
    reachable station (free seat, or a full seat where this student outranks
    the weakest admit) means leaving them unassigned already creates a
    blocking pair that later decisions cannot remove -- the student is decided
    and the seat can only get more contested.
    """
    for station_id in edges.get(student, ()):
        station = inp.stations[station_id]
        held = admits[station_id]
        if len(held) < station.quota:
            return False
        rank = rank_of[station_id]
        if any(rank[student] < rank[incumbent] for incumbent in held):
            return False
    return True


# --------------------------------------------------------------------------- #
# exact search
# --------------------------------------------------------------------------- #
@dataclass
class RosterResult:
    stable: bool
    placements: int
    total_subsidy: int
    assignment: Dict[str, Optional[str]]
    admissions: Dict[str, List[str]]
    unassigned: List[str]
    blocking_pairs: List[dict] = field(default_factory=list)
    nodes_explored: int = 0
    leaves_explored: int = 0
    stable_leaves: int = 0

    def as_document(self, inp: Optional[MatchingInput] = None) -> dict:
        return {
            "format": "internship-roster/1",
            "stable": self.stable,
            "objective": {
                "placements": self.placements,
                "total_subsidy": self.total_subsidy,
            },
            "assignments": [
                {"student_id": s, "station_id": self.assignment[s]}
                for s in sorted(self.assignment)
                if self.assignment[s] is not None
            ],
            "unassigned": list(self.unassigned),
            "admissions": [
                {
                    "station_id": sid,
                    "quota": inp.stations[sid].quota if inp is not None else None,
                    "closed": inp.stations[sid].closed if inp is not None else None,
                    "admitted_students": list(self.admissions[sid]),
                }
                for sid in sorted(self.admissions)
            ],
            "blocking_pairs": list(self.blocking_pairs),
            "search": {
                "nodes_explored": self.nodes_explored,
                "leaves_explored": self.leaves_explored,
                "stable_leaves": self.stable_leaves,
            },
        }


def stable_lattice_edges(
    inp: MatchingInput, edges: Dict[str, Tuple[str, ...]]
) -> Dict[str, Tuple[str, ...]]:
    """Pairs that can occur in at least one stable matching.

    Student-proposing DA gives each student their best achievable station;
    station-proposing DA gives their worst achievable one.  Every stable
    matching assigns the student to a station between those two endpoints on
    their wish list, so restricting the search to this interval is exact: it
    keeps every stable roster and removes every pair no stable roster uses.
    """
    student_opt = deferred_acceptance(inp, edges)
    station_opt = station_deferred_acceptance(inp, edges)
    lattice: Dict[str, Tuple[str, ...]] = {}
    for student, wish in inp.preferences.items():
        best = student_opt[student]
        worst = station_opt[student]
        best_i = wish.index(best) if best in wish else len(wish)
        worst_i = wish.index(worst) if worst in wish else len(wish)
        lo, hi = min(best_i, worst_i), max(best_i, worst_i)
        lattice[student] = tuple(
            t for t in wish[lo:hi + 1] if t in edges.get(student, ())
        )
    return lattice


def solve_matching(inp: MatchingInput) -> RosterResult:
    edges = inp.admissible_edges()
    rank_of = _rank_maps(inp.stations)
    # Restrict to the stable lattice first: pairs no stable roster can use are
    # gone before any search, which collapses dense instances dramatically.
    lattice = stable_lattice_edges(inp, edges)
    # Fixed student order makes a DFS state fully described by (pointer,
    # admissions, placed), so identical states reached on different paths can
    # share one result instead of being re-explored.
    students = sorted(inp.preferences)
    station_ids = sorted(inp.stations)
    total_seats = sum(s.quota for s in inp.stations.values() if not s.closed)

    # Stage 1: DA produces a stable matching.  In the college-admissions model
    # every stable matching matches the same set of students, so its size M is
    # the maximum; the DFS only optimises cost among size-M stable rosters.
    seed = deferred_acceptance(inp, edges)
    target = sum(1 for v in seed.values() if v is not None)
    best: Dict[str, Optional[str]] = dict(seed)
    best_cost = sum(inp.subsidies[sid][s] for s, sid in seed.items() if sid is not None)
    stats = {"nodes": 0, "leaves": 0, "stable_leaves": 1}

    assignment: Dict[str, Optional[str]] = {}
    admits: Dict[str, List[str]] = {sid: [] for sid in inp.stations}
    # state key -> best additional cost already found for extending it
    memo: Dict[tuple, int] = {}

    def state_key(pointer: int, placed: int) -> tuple:
        return (
            pointer,
            placed,
            tuple(tuple(sorted(admits[sid])) for sid in station_ids),
        )

    def dfs(pointer: int, placed: int, cost: int) -> None:
        nonlocal best, best_cost
        stats["nodes"] += 1
        free_seats = total_seats - sum(len(v) for v in admits.values())
        if placed + min(len(students) - pointer, free_seats) < target:
            return

        key = state_key(pointer, placed)
        prev_best = memo.get(key)
        if prev_best is not None and cost >= prev_best:
            return
        memo[key] = cost

        if pointer == len(students):
            stats["leaves"] += 1
            if placed != target:
                return
            blockers = find_blocking_pairs(assignment, inp, edges, rank_of, admits)
            if blockers:
                return
            stats["stable_leaves"] += 1
            if cost < best_cost or (
                cost == best_cost
                and tuple(sorted(assignment.items())) < tuple(sorted(best.items()))
            ):
                best, best_cost = dict(assignment), cost
            return

        student = students[pointer]
        must_leave = placed >= target

        # Unassigned branch first: it survives only when no admissible station
        # is reachable for this decided student, which prunes the huge
        # "everyone unassigned" region immediately.
        if unassigned_is_stable_possible(student, inp, lattice, rank_of, admits):
            assignment[student] = None
            dfs(pointer + 1, placed, cost)
            del assignment[student]

        if not must_leave:
            # Cheapest seat first so the cost bound tightens early.
            seat_branches = sorted(
                lattice.get(student, ()),
                key=lambda sid: (inp.subsidies[sid][student],
                                 inp.preferences[student].index(sid)),
            )
            for station_id in seat_branches:
                if len(admits[station_id]) >= inp.stations[station_id].quota:
                    continue
                extra = inp.subsidies[station_id][student]
                admits[station_id].append(student)
                assignment[student] = station_id
                # A new seat can only create a permanent block against an
                # earlier decided unassigned student; check that set only.
                if not has_permanent_block(
                    students[:pointer + 1], assignment, inp, lattice, rank_of, admits
                ):
                    dfs(pointer + 1, placed + 1, cost + extra)
                admits[station_id].remove(student)
                del assignment[student]

    dfs(0, 0, 0)

    final_admits: Dict[str, List[str]] = {sid: [] for sid in inp.stations}
    for student, station_id in best.items():
        if station_id is not None:
            final_admits[station_id].append(student)
    for sid in final_admits:
        final_admits[sid].sort(key=lambda s: rank_of[sid][s])
    total = sum(inp.subsidies[station_id][student]
                for student, station_id in best.items() if station_id is not None)
    blockers = find_blocking_pairs(best, inp, edges, rank_of, final_admits)
    return RosterResult(
        stable=not blockers,
        placements=sum(1 for v in best.values() if v is not None),
        total_subsidy=total,
        assignment=dict(sorted(best.items())),
        admissions=final_admits,
        unassigned=sorted(s for s, v in best.items() if v is None),
        blocking_pairs=blockers,
        nodes_explored=stats["nodes"],
        leaves_explored=stats["leaves"],
        stable_leaves=stats["stable_leaves"],
    )


__all__ = ["MatchingInput", "RosterResult", "build_matching_input", "solve_matching",
           "find_blocking_pairs"]
