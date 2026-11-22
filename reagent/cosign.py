"""Pure co-signing (会签) model: eligibility, mutual recognition, planning.

This module is deliberately free of SQLite: it works on plain dataclasses so
the combinatorial rules can be reasoned about and tested in isolation.

Vocabulary
----------
seat
    A named signer position. ``ordinary`` seats are single-owner, so they can
    vouch for at most one settlement number per seal. ``deputy`` seats carry
    an independent paper book; two windows may each hold a copy, so a deputy
    alone cannot stop two settlements.

group
    A named set of seats that must *all* be on duty, plus a one-off activation
    fee. A group activated once serves any number of shifts.

shift
    A duty roster (the set of seats on duty for that window/shift). Every
    possible shift must be able to assemble at least one activated group.

The plan chooses a minimum-fee set of activated groups such that

* every activated group *covers* every sealed run (shares at least one
  trusted witness seat with it);
* every shift contains at least one activated group entirely; and
* any two activated groups (including a group with itself) share an
  *ordinary* seat, and each shares an ordinary seat with every still-effective
  historical group.  This is the mutual-recognition rule that guarantees at
  most one settlement per seal across offline windows.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

ORDINARY = "ordinary"
DEPUTY = "deputy"
SEAT_TYPES = (ORDINARY, DEPUTY)


@dataclass(frozen=True)
class Group:
    name: str
    seats: frozenset[str]
    fee: int = 0

    def as_dict(self) -> dict:
        return {"name": self.name, "seats": sorted(self.seats), "fee": self.fee}


@dataclass(frozen=True)
class Shift:
    shift_id: str
    on_duty: frozenset[str]

    def as_dict(self) -> dict:
        return {"shift_id": self.shift_id, "on_duty": sorted(self.on_duty)}


@dataclass(frozen=True)
class HistoricalGroup:
    group_id: str
    seats: frozenset[str]
    recalled: bool = False


@dataclass
class Plan:
    feasible: bool
    activated: list[str] = field(default_factory=list)
    total_fee: Optional[int] = None
    per_shift: dict[str, list[str]] = field(default_factory=dict)
    eligible: list[str] = field(default_factory=list)
    ineligible: dict[str, list[str]] = field(default_factory=dict)
    blocked: dict[str, str] = field(default_factory=dict)
    unsatisfiable_shifts: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    reason: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "activated_groups": list(self.activated),
            "total_fee": self.total_fee,
            "per_shift": {k: list(v) for k, v in self.per_shift.items()},
            "eligible_groups": list(self.eligible),
            "ineligible_groups": {k: list(v) for k, v in self.ineligible.items()},
            "blocked_groups": dict(self.blocked),
            "unsatisfiable_shifts": list(self.unsatisfiable_shifts),
            "conflicts": list(self.conflicts),
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# construction helpers (validating plain JSON-ish input)
# ---------------------------------------------------------------------------
def parse_groups(raw: Sequence[Mapping]) -> list[Group]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("groups must be a non-empty list")
    out: list[Group] = []
    seen: set[str] = set()
    for item in raw:
        name = _text(item.get("name"), "group name")
        if name in seen:
            raise ValueError(f"duplicate group name {name!r}")
        seen.add(name)
        seats = _seats(item.get("seats"), f"group {name} seats")
        fee = item.get("fee", 0)
        if not isinstance(fee, int) or isinstance(fee, bool) or fee < 0:
            raise ValueError(f"group {name} fee must be a non-negative integer")
        out.append(Group(name, seats, fee))
    return out


def parse_shifts(raw: Sequence[Mapping]) -> list[Shift]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("shifts must be a non-empty list")
    out: list[Shift] = []
    seen: set[str] = set()
    for item in raw:
        sid = _text(item.get("shift_id"), "shift_id")
        if sid in seen:
            raise ValueError(f"duplicate shift_id {sid!r}")
        seen.add(sid)
        out.append(Shift(sid, _seats(item.get("on_duty"), f"shift {sid} on_duty")))
    return out


def parse_historical(raw: Sequence[Mapping]) -> list[HistoricalGroup]:
    out: list[HistoricalGroup] = []
    for item in raw:
        gid = _text(item.get("group_id"), "historical group_id")
        out.append(
            HistoricalGroup(
                gid,
                _seats(item.get("seats"), f"historical group {gid} seats"),
                bool(item.get("recalled", False)),
            )
        )
    return out


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _seats(value: object, name: str) -> frozenset[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError(f"{name} must be a list of seat ids")
    seats = frozenset(_text(s, name) for s in value)
    if not seats:
        raise ValueError(f"{name} must not be empty")
    return seats


# ---------------------------------------------------------------------------
# eligibility + recognition predicates
# ---------------------------------------------------------------------------
def eligible_groups(
    groups: Iterable[Group],
    runs: Sequence[str],
    witnesses: Mapping[str, Iterable[str]],
) -> tuple[list[Group], dict[str, list[str]]]:
    """Split groups into those covering every run and those that miss runs."""
    witness_sets = {r: frozenset(witnesses.get(r, ())) for r in runs}
    eligible: list[Group] = []
    ineligible: dict[str, list[str]] = {}
    for group in groups:
        missing = [r for r in runs if not (group.seats & witness_sets[r])]
        if missing:
            ineligible[group.name] = missing
        else:
            eligible.append(group)
    return eligible, ineligible


def shares_ordinary(a: frozenset[str], b: frozenset[str], ordinary: frozenset[str]) -> bool:
    return bool((a & b) & ordinary)


def recognition_violations(
    groups: Sequence[Group],
    ordinary: frozenset[str],
    historical: Sequence[HistoricalGroup] = (),
) -> list[str]:
    """Return human-readable violations of the mutual-recognition rules.

    Covers the self-intersection rule (a group must contain an ordinary seat),
    pairwise ordinary-intersection among activated groups, and ordinary
    intersection with every still-effective historical group.
    """
    violations: list[str] = []
    for group in groups:
        if not (group.seats & ordinary):
            violations.append(
                f"group {group.name} has no ordinary seat (self-intersection)"
            )
    for a, b in itertools.combinations(groups, 2):
        if not shares_ordinary(a.seats, b.seats, ordinary):
            violations.append(
                f"groups {a.name} and {b.name} only share deputy seats"
            )
    effective = [h for h in historical if not h.recalled]
    for group in groups:
        for old in effective:
            if not shares_ordinary(group.seats, old.seats, ordinary):
                violations.append(
                    f"group {group.name} lacks an ordinary seat shared with "
                    f"effective historical group {old.group_id}"
                )
    return violations


# ---------------------------------------------------------------------------
# planner
# ---------------------------------------------------------------------------
def plan_scheme(
    groups: Sequence[Group],
    shifts: Sequence[Shift],
    runs: Sequence[str],
    witnesses: Mapping[str, Iterable[str]],
    ordinary: Iterable[str],
    historical: Sequence[HistoricalGroup] = (),
) -> Plan:
    """Choose the minimum-fee activated group set satisfying all rules."""
    ordinary = frozenset(ordinary)
    runs = list(runs)
    eligible, ineligible = eligible_groups(groups, runs, witnesses)
    eligible_names = [g.name for g in eligible]

    # A group with no ordinary seat can never be activated (self rule).
    # A group not contained in any shift can never help duty coverage, and
    # since the fee is positive it is never worth activating.
    blocked: dict[str, str] = {}
    for group in eligible:
        if not (group.seats & ordinary):
            blocked[group.name] = "no ordinary seat (self-intersection rule)"

    effective_hist = [h for h in historical if not h.recalled]
    for group in eligible:
        if group.name in blocked:
            continue
        for old in effective_hist:
            if not shares_ordinary(group.seats, old.seats, ordinary):
                blocked[group.name] = (
                    f"lacks an ordinary seat shared with effective historical "
                    f"group {old.group_id}"
                )
                break

    # groups (eligible, not blocked) whose whole seat set lies inside a shift
    inside: dict[str, list[Group]] = {}
    for shift in shifts:
        hit = [g for g in eligible if g.seats <= shift.on_duty]
        inside[shift.shift_id] = hit

    activatable = [
        g for g in eligible if g.name not in blocked
    ]

    # Coverage map: which shifts each activatable group can serve.
    cover_map: dict[str, frozenset[str]] = {}
    for group in activatable:
        cover_map[group.name] = frozenset(
            shift.shift_id for shift in shifts if group.seats <= shift.on_duty
        )

    unsatisfiable = [
        shift.shift_id
        for shift in shifts
        if not any(g.name in cover_map for g in inside[shift.shift_id])
    ]

    if unsatisfiable:
        return Plan(
            feasible=False,
            eligible=eligible_names,
            ineligible=ineligible,
            blocked=blocked,
            unsatisfiable_shifts=unsatisfiable,
            reason=(
                "a shift has no activatable group (eligible, with an ordinary "
                "seat, and recognised by effective historical groups) fully on "
                "duty: " + ", ".join(unsatisfiable)
            ),
        )

    all_shifts = frozenset(shift.shift_id for shift in shifts)
    by_name = {g.name: g for g in activatable}

    # groups usable for the chosen uncovered shift, sorted deterministically
    def candidates_for(shift_id: str, chosen: list[Group]) -> list[Group]:
        out = []
        for g in inside[shift_id]:
            if g.name not in cover_map:
                continue
            if any(
                not shares_ordinary(g.seats, h.seats, ordinary) for h in chosen
            ):
                continue
            out.append(g)
        out.sort(key=lambda g: (g.fee, len(g.seats), g.name))
        return out

    best_key: Optional[tuple[int, int, tuple[str, ...]]] = None
    best_sel: Optional[list[str]] = None

    def better(sel: list[Group], fee: int) -> None:
        nonlocal best_key, best_sel
        key = (fee, len(sel), tuple(sorted(g.name for g in sel)))
        if best_key is None or key < best_key:
            best_key = key
            best_sel = [g.name for g in sel]

    def search(
        chosen: list[Group],
        chosen_names: set[str],
        covered: frozenset[str],
        fee: int,
    ) -> None:
        if best_key is not None and fee > best_key[0]:
            return
        if covered == all_shifts:
            better(chosen, fee)
            return
        # pick the uncovered shift with the fewest feasible candidates
        uncovered = all_shifts - covered
        pick: Optional[str] = None
        pick_cands: list[Group] = []
        for sid in sorted(uncovered):
            cands = [
                g for g in candidates_for(sid, chosen) if g.name not in chosen_names
            ]
            if not cands:
                return  # this branch cannot cover sid
            if pick is None or len(cands) < len(pick_cands):
                pick, pick_cands = sid, cands
        assert pick is not None
        for group in pick_cands:
            search(
                chosen + [group],
                chosen_names | {group.name},
                covered | cover_map[group.name],
                fee + group.fee,
            )

    search([], set(), frozenset(), 0)

    if best_sel is None:
        conflicts = _pair_conflicts(activatable, ordinary)
        return Plan(
            feasible=False,
            eligible=eligible_names,
            ineligible=ineligible,
            blocked=blocked,
            conflicts=conflicts,
            reason=(
                "duty coverage and mutual recognition cannot be satisfied "
                "together (an unavoidable pair of groups shares only deputy "
                "seats)"
                if conflicts
                else "no group activation covers every shift"
            ),
        )

    activated = sorted(best_sel)
    total_fee = sum(by_name[n].fee for n in activated)
    per_shift = {
        shift.shift_id: [
            n for n in activated if by_name[n].seats <= shift.on_duty
        ]
        for shift in shifts
    }
    return Plan(
        feasible=True,
        activated=activated,
        total_fee=total_fee,
        per_shift=per_shift,
        eligible=eligible_names,
        ineligible=ineligible,
        blocked=blocked,
    )


def _pair_conflicts(groups: Sequence[Group], ordinary: frozenset[str]) -> list[str]:
    out: list[str] = []
    for a, b in itertools.combinations(groups, 2):
        if not shares_ordinary(a.seats, b.seats, ordinary):
            out.append(
                f"groups {a.name} and {b.name} share no ordinary seat"
            )
    return out


# ---------------------------------------------------------------------------
# settlement signature check (pure part)
# ---------------------------------------------------------------------------
def signed_groups(
    signatures: Iterable[str],
    activated_names: Iterable[str],
    groups_by_name: Mapping[str, Group],
) -> list[str]:
    """Activated groups whose every member appears among ``signatures``."""
    signed = frozenset(signatures)
    out: list[str] = []
    for name in activated_names:
        group = groups_by_name.get(name)
        if group is not None and group.seats <= signed:
            out.append(name)
    return sorted(out)
