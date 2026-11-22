"""Registrar batch scheduling for re-issue signatures.

Each re-issued student needs *one* of their minimal consent sets present on the
signing day.  A consent set is a package deal: every advisor in it must be
physically present.  We choose one package per student and one attendance set,
paying ``price[advisor]`` per distinct attending head, and minimise the total.

Day constraints
---------------
* ``fixed_attendees`` -- already-committed attendees; present and charged even
  if no chosen package needs them.
* ``companion_groups`` -- "travel together" groups: if any member attends, all
  members attend (and are charged).
* ``incompatible_pairs`` -- pairs who cannot be on campus the same day.

A package that itself spans an incompatible pair is unusable; a student with
no usable package makes the whole round infeasible for that day (reported with
the offending advisors).  A fixed/companion set that already collides is also
infeasible.

The optimiser is exact: the advisor universe is at most 16 names (instance
contract), so it enumerates every attendance subset, keeps the consistent,
companion-closed ones that cover a package for every student, and returns the
minimum-fee one.  Ties are broken deterministically, so equal-cost alternatives
are all valid and any one may be reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from .errors import SchemaError
from .resign import ReissueResult

MAX_ADVISORS = 16


@dataclass(frozen=True)
class Package:
    """A minimal consent set, offered to one student."""

    student_id: str
    advisors: FrozenSet[str]
    plan: Optional[dict] = None

    def as_dict(self) -> dict:
        out: dict = {"advisors": sorted(self.advisors)}
        if self.plan is not None:
            out["plan"] = self.plan
        return out


@dataclass
class BatchResult:
    feasible: bool
    total_cost: Optional[int]
    attendees: List[str] = field(default_factory=list)
    assignments: Dict[str, dict] = field(default_factory=dict)
    per_student: Dict[str, List[dict]] = field(default_factory=dict)
    fixed_attendees: List[str] = field(default_factory=list)
    conflicts: List[dict] = field(default_factory=list)
    note: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "total_cost": self.total_cost,
            "attendees": list(self.attendees),
            "assignments": dict(self.assignments),
            "per_student": {sid: list(v) for sid, v in self.per_student.items()},
            "fixed_attendees": list(self.fixed_attendees),
            "conflicts": list(self.conflicts),
            "note": self.note,
        }


# --------------------------------------------------------------------------- #
# set helpers
# --------------------------------------------------------------------------- #
def _closure(members: Iterable[str], companion_groups: Sequence[Sequence[str]]) -> FrozenSet[str]:
    """Expand ``members`` through companion groups until stable."""
    closed: Set[str] = set(members)
    changed = True
    while changed:
        changed = False
        for group in companion_groups:
            gs = set(group)
            if closed & gs and not gs <= closed:
                closed |= gs
                changed = True
    return frozenset(closed)


def _first_conflict(
    members: Iterable[str], incompatible_pairs: Sequence[Sequence[str]]
) -> Optional[Tuple[str, str]]:
    s = set(members)
    for pair in incompatible_pairs:
        if pair[0] in s and pair[1] in s:
            return pair[0], pair[1]
    return None


def _fee(members: Iterable[str], price: Dict[str, int]) -> int:
    return sum(price.get(a, 0) for a in members)


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
def _packages_from_reissue(res: ReissueResult) -> List[Package]:
    return [
        Package(res.student_id, opt.advisors, opt.plan.as_document())
        for opt in res.options
    ]


def _normalise_packages(students: Dict[str, Iterable[Package]]) -> Dict[str, List[Package]]:
    norm: Dict[str, List[Package]] = {}
    for sid, pkgs in students.items():
        seen: Set[FrozenSet[str]] = set()
        kept: List[Package] = []
        for p in sorted(pkgs, key=lambda p: (len(p.advisors), sorted(p.advisors))):
            if p.advisors in seen:
                continue
            seen.add(p.advisors)
            kept.append(Package(sid, p.advisors, p.plan))
        norm[sid] = kept
    return norm


# --------------------------------------------------------------------------- #
# exact optimiser
# --------------------------------------------------------------------------- #
def _solve_batch(
    students: Dict[str, List[Package]],
    price: Dict[str, int],
    fixed_attendees: Sequence[str],
    companion_groups: Sequence[Sequence[str]],
    incompatible_pairs: Sequence[Sequence[str]],
) -> BatchResult:
    fixed = list(dict.fromkeys(fixed_attendees))
    order = sorted(students)
    per_student_raw = {sid: [p.as_dict() for p in students[sid]] for sid in order}

    # An empty round: only fixed attendees' fees, provided they are consistent.
    if not order:
        fixed_closed = _closure(fixed, companion_groups)
        missing = sorted(a for a in fixed_closed if a not in price)
        if missing:
            raise SchemaError(
                f"no admission price given for committed advisor(s) {missing}",
                location="price",
            )
        pair = _first_conflict(fixed_closed, incompatible_pairs)
        if pair is not None:
            return BatchResult(
                feasible=False, total_cost=None, fixed_attendees=fixed,
                conflicts=[{"code": "attendance_infeasible",
                            "message": f"committed attendees {pair[0]!r} and {pair[1]!r} "
                                       f"cannot attend the same day"}],
                note="the fixed attendance set already contains an incompatible pair",
            )
        return BatchResult(
            feasible=True, total_cost=_fee(fixed_closed, price),
            attendees=sorted(fixed_closed), fixed_attendees=fixed, per_student={},
        )

    # Drop packages that internally span a forbidden pair.
    usable: Dict[str, List[Package]] = {}
    for sid in order:
        usable[sid] = [p for p in students[sid]
                       if _first_conflict(p.advisors, incompatible_pairs) is None]

    no_option = [
        {
            "code": "no_package",
            "student_id": sid,
            "message": f"every consent set for {sid} contains an advisor pair that "
                       f"cannot attend together",
            "candidate_advisors": sorted({a for p in students[sid] for a in p.advisors}),
        }
        for sid in order if not usable[sid]
    ]
    if no_option:
        return BatchResult(
            feasible=False, total_cost=None, fixed_attendees=fixed,
            per_student=per_student_raw, conflicts=no_option,
            note="at least one student has no consent set usable on this day",
        )

    # Universe: every advisor named by a usable package, plus fixed attendees,
    # plus companion-group members reachable from those (closure).
    base: Set[str] = set(fixed)
    for sid in order:
        for p in usable[sid]:
            base |= p.advisors
    universe = _closure(base, companion_groups)
    universe_sorted = sorted(universe)

    missing_price = sorted(a for a in universe_sorted if a not in price)
    if missing_price:
        # Never silently bill an attending head at 0: a missing price would
        # understate the round's cost.
        raise SchemaError(
            f"no admission price given for advisor(s) {missing_price}; every "
            f"potentially attending advisor needs a price",
            location="price",
        )

    if len(universe_sorted) > MAX_ADVISORS:
        # Exact enumeration is still correct; the guard exists so a caller
        # cannot hand us an unbounded 2^n blow-up silently.
        raise SchemaError(
            f"advisor universe {len(universe_sorted)} exceeds supported {MAX_ADVISORS} "
            f"for exact batch optimisation",
            location="batch",
        )

    # Precompute per-student requirement masks over the universe indices.
    index = {a: i for i, a in enumerate(universe_sorted)}
    pkg_masks: Dict[str, List[Tuple[int, Package]]] = {}
    for sid in order:
        pkg_masks[sid] = []
        for p in usable[sid]:
            m = 0
            for a in p.advisors:
                m |= 1 << index[a]
            pkg_masks[sid].append((m, p))

    full_mask = (1 << len(universe_sorted)) - 1
    fixed_mask = 0
    for a in fixed:
        fixed_mask |= 1 << index[a]

    # Companion-closure masks and incompatibility masks, in index space.
    companion_masks = []
    for group in companion_groups:
        gm = 0
        for a in group:
            if a in index:
                gm |= 1 << index[a]
        if gm:
            companion_masks.append(gm)
    conflict_masks = []
    for pair in incompatible_pairs:
        a, b = pair[0], pair[1]
        if a in index and b in index:
            conflict_masks.append((1 << index[a]) | (1 << index[b]))

    def is_closed(mask: int) -> bool:
        for gm in companion_masks:
            if mask & gm and (mask & gm) != gm:
                return False
        return True

    def has_conflict(mask: int) -> bool:
        return any((mask & cm) == cm for cm in conflict_masks)

    def coverage(mask: int):
        """(covers_every_student, chosen {sid: package}) for one attendance mask."""
        chosen: Dict[str, Package] = {}
        for sid in order:
            pick = None
            for pm, p in pkg_masks[sid]:
                if pm & ~mask == 0:  # package advisors all present
                    pick = p
                    break  # packages pre-sorted cheapest/smallest
            if pick is None:
                return False, chosen
            chosen[sid] = pick
        return True, chosen

    best: Optional[Tuple[int, int]] = None  # (cost, mask)
    for mask in range(full_mask + 1):
        if (mask & fixed_mask) != fixed_mask:
            continue
        if not is_closed(mask):
            continue
        if has_conflict(mask):
            continue
        covers, _ = coverage(mask)
        if not covers:
            continue
        cost = _fee((universe_sorted[i] for i in range(len(universe_sorted))
                     if (mask >> i) & 1), price)
        # deterministic tie-break: lower cost, then lexicographically smaller set
        key = (cost, mask)
        if best is None or key < best:
            best = key

    if best is None:
        return BatchResult(
            feasible=False, total_cost=None, fixed_attendees=fixed,
            per_student=per_student_raw,
            conflicts=_diagnose(students, usable, fixed, companion_groups,
                               incompatible_pairs, index, order),
            note="no attendance set covers one consent set for every student under "
                 "the fixed / companion / incompatible constraints",
        )

    cost, mask = best
    attendees = [universe_sorted[i] for i in range(len(universe_sorted))
                 if (mask >> i) & 1]
    _, chosen = coverage(mask)
    assignments = {
        sid: {"advisors": sorted(chosen[sid].advisors), "plan": chosen[sid].plan}
        for sid in order
    }
    return BatchResult(
        feasible=True,
        total_cost=cost,
        attendees=attendees,
        assignments=assignments,
        per_student=per_student_raw,
        fixed_attendees=fixed,
        conflicts=[],
        note=None,
    )


def _diagnose(students, usable, fixed, companion_groups, incompatible_pairs,
              index, order) -> List[dict]:
    """Explain why no attendance set works (for the infeasible report)."""
    out: List[dict] = []
    fixed_closed = _closure(fixed, companion_groups)
    pair = _first_conflict(fixed_closed, incompatible_pairs)
    if pair is not None:
        out.append({
            "code": "attendance_infeasible",
            "message": f"fixed/companion attendees {pair[0]!r} and {pair[1]!r} "
                       f"cannot attend together",
        })
        return out
    # Otherwise: find students whose available packages cannot coexist.
    for sid in order:
        block = []
        for p in usable[sid]:
            blocker = _first_conflict(p.advisors, incompatible_pairs)
            if blocker is not None:
                block.append({"advisors": sorted(p.advisors),
                              "blocked_by": list(blocker)})
        if block:
            out.append({
                "code": "package_conflict",
                "student_id": sid,
                "message": f"all consent sets for {sid} force a cannot-attend pair",
                "options": block,
            })
    if not out:
        out.append({
            "code": "coverage_conflict",
            "message": "the cheapest constraints interact so no single attendance set "
                       "covers a consent set for every student (e.g. companion groups "
                       "force in advisors that conflict with another student's package)",
        })
    return out


# --------------------------------------------------------------------------- #
# public entry points
# --------------------------------------------------------------------------- #
def schedule_batch(
    reissues: Dict[str, ReissueResult],
    *,
    price: Dict[str, int],
    fixed_attendees: Sequence[str] = (),
    companion_groups: Sequence[Sequence[str]] = (),
    incompatible_pairs: Sequence[Sequence[str]] = (),
) -> BatchResult:
    """Optimise one signing-day round over per-student re-issue results.

    Students that cannot be re-issued even with full consent have no package;
    they are reported as conflicts (the round cannot cover them) rather than
    silently dropped -- a measurement must never masquerade as a re-issue.
    """
    students: Dict[str, List[Package]] = {}
    blocked: Dict[str, dict] = {}
    for sid, res in reissues.items():
        if not res.full_consent_feasible:
            blocked[sid] = {
                "code": "cannot_reissue",
                "student_id": sid,
                "message": res.note or "no consent set yields a valid plan",
            }
            continue
        students[sid] = _packages_from_reissue(res)
    result = _solve_batch(_normalise_packages(students), price,
                          fixed_attendees, companion_groups, incompatible_pairs)
    if blocked:
        result.feasible = False
        result.conflicts = list(result.conflicts) + [blocked[s] for s in sorted(blocked)]
        result.total_cost = None
        result.attendees = []
        result.assignments = {}
        result.note = ("one or more students cannot be re-issued at all; the round "
                       "cannot be scheduled as a genuine re-issue")
    return result


def schedule_packages(
    students: Dict[str, Iterable[Package]],
    *,
    price: Dict[str, int],
    fixed_attendees: Sequence[str] = (),
    companion_groups: Sequence[Sequence[str]] = (),
    incompatible_pairs: Sequence[Sequence[str]] = (),
) -> BatchResult:
    """Lower-level entry point taking explicit packages per student."""
    for sid, pkgs in students.items():
        if not list(pkgs):
            raise SchemaError(f"student {sid!r} has no consent-set package", location=sid)
    norm = _normalise_packages({sid: list(pkgs) for sid, pkgs in students.items()})
    return _solve_batch(norm, price, fixed_attendees, companion_groups, incompatible_pairs)


__all__ = [
    "Package",
    "BatchResult",
    "schedule_batch",
    "schedule_packages",
]
