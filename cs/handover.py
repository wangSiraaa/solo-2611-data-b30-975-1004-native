"""Pure shift-handover solver: assign a stack of tickets to registered desks.

The incoming shift brings one stack made of two kinds of units:

* **sealed groups** (封条): tickets sealed together that must be handed to one
  desk. Every ticket in the group must be eligible for that desk, and the
  group occupies one slot per ticket on the desk.
* **loose tickets** (散件): each is its own unit and may go its own way.

Each desk has a fixed whole-ticket quota from the incoming shift's own roster.
Moving a ticket from the desk that held it on the previous shift to a
different desk costs a given number of workhours per ticket; staying on the
same desk costs zero. A plan is only acceptable when *all* units can be
placed; then the plan with minimum total workhours wins. Equal-cost plans are
equivalent and one deterministic representative is returned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence


@dataclass(frozen=True)
class Unit:
    """One assignable block: a sealed group or a single loose ticket."""

    unit_id: str
    ticket_ids: tuple[str, ...]
    sealed: bool

    @property
    def size(self) -> int:
        return len(self.ticket_ids)


@dataclass
class HandoverProblem:
    units: list[Unit]
    eligibility: Mapping[str, frozenset[str]]
    quotas: Mapping[str, int]
    holder: Mapping[str, str]
    transfer_cost: Mapping[str, int] = field(default_factory=dict)

    def cost(self, ticket_id: str, desk_id: str) -> int:
        if self.holder.get(ticket_id) == desk_id:
            return 0
        return int(self.transfer_cost.get(ticket_id, 1))

    def unit_options(self, unit: Unit) -> list[str]:
        """Desks that may take the whole unit (every member ticket eligible)."""

        common: set[str] | None = None
        for ticket_id in unit.ticket_ids:
            desks = set(self.eligibility.get(ticket_id, frozenset()))
            common = desks if common is None else common & desks
        return sorted(common or set())


def build_units(
    ticket_ids: Sequence[str], sealed_groups: Sequence[Sequence[str]]
) -> list[Unit]:
    """Construct units, validating that seals partition a subset of tickets."""

    groups = [tuple(group) for group in sealed_groups if group]
    seen: set[str] = set()
    units: list[Unit] = []
    for index, group in enumerate(groups):
        members = tuple(group)
        if len(set(members)) != len(members):
            raise ValueError(f"sealed group {index} contains a repeated ticket")
        overlap = seen.intersection(members)
        if overlap:
            raise ValueError(f"ticket appears in two sealed groups: {sorted(overlap)}")
        unknown = [t for t in members if t not in ticket_ids]
        if unknown:
            raise ValueError(f"sealed group references unknown tickets: {unknown}")
        seen.update(members)
        units.append(Unit(unit_id=f"G{index + 1}", ticket_ids=members, sealed=True))
    for ticket_id in ticket_ids:
        if ticket_id not in seen:
            units.append(Unit(unit_id=f"L:{ticket_id}", ticket_ids=(ticket_id,), sealed=False))
    # Deterministic processing order: sealed groups first (in input order),
    # then loose tickets sorted by id.
    units.sort(key=lambda u: (0 if u.sealed else 1, u.unit_id))
    return units


def solve(problem: HandoverProblem) -> dict:
    """Return the minimum-workhours feasible plan, or an infeasible verdict.

    Depth-first branch-and-bound. At most 120 tickets, 8 sealed groups and
    8 desks, exhaustive enumeration is small; the lower-bound prune only makes
    it smaller. Desk options are tried in sorted id order and a strictly better
    cost is required to replace the incumbent, so equivalent optima resolve to
    one deterministic plan.
    """

    desk_ids = sorted(problem.quotas)
    unknown_quota = [d for d in {d for u in problem.units for d in problem.unit_options(u)}
                     if d not in problem.quotas]
    if unknown_quota:
        raise ValueError(f"eligible desk missing from roster quotas: {unknown_quota}")

    options = {u.unit_id: problem.unit_options(u) for u in problem.units}
    unit_cost = {
        u.unit_id: {
            desk: sum(problem.cost(t, desk) for t in u.ticket_ids) for desk in options[u.unit_id]
        }
        for u in problem.units
    }
    remaining_lower: list[int] = []
    for pos in range(len(problem.units)):
        bound = 0
        for later in problem.units[pos:]:
            costs = unit_cost[later.unit_id].values()
            bound += min(costs) if costs else 0
        remaining_lower.append(bound)

    used = {desk: 0 for desk in desk_ids}
    choice: dict[str, str] = {}
    best: dict[str, str] | None = None
    best_cost: int | None = None

    def visit(pos: int, spent: int) -> None:
        nonlocal best, best_cost
        if best_cost is not None and spent + remaining_lower[pos] >= best_cost:
            return
        if pos == len(problem.units):
            if best_cost is None or spent < best_cost:
                best_cost = spent
                best = dict(choice)
            return
        unit = problem.units[pos]
        for desk in options[unit.unit_id]:
            if used[desk] + unit.size > problem.quotas[desk]:
                continue
            used[desk] += unit.size
            choice[unit.unit_id] = desk
            visit(pos + 1, spent + unit_cost[unit.unit_id][desk])
            used[desk] -= unit.size
            choice.pop(unit.unit_id, None)

    visit(0, 0)

    if best is None:
        return {
            "format": "handover-plan/1",
            "feasible": False,
            "total_workhours": None,
            "assignments": [],
            "desk_totals": _desk_totals(problem, {}),
            "blockers": _blockers(problem, options),
        }

    assignments = []
    final_used = {desk: 0 for desk in desk_ids}
    for unit in problem.units:
        desk = best[unit.unit_id]
        final_used[desk] += unit.size
        per_ticket = [
            {
                "ticket_id": ticket_id,
                "from_desk": problem.holder.get(ticket_id),
                "to_desk": desk,
                "workhours": problem.cost(ticket_id, desk),
            }
            for ticket_id in unit.ticket_ids
        ]
        assignments.append(
            {
                "unit_id": unit.unit_id,
                "kind": "sealed" if unit.sealed else "loose",
                "ticket_ids": list(unit.ticket_ids),
                "desk_id": desk,
                "ticket_count": unit.size,
                "slots_used": unit.size,
                "workhours": sum(item["workhours"] for item in per_ticket),
                "per_ticket": per_ticket,
            }
        )
    return {
        "format": "handover-plan/1",
        "feasible": True,
        "total_workhours": best_cost,
        "assignments": assignments,
        "desk_totals": _desk_totals(problem, final_used),
        "blockers": [],
    }


def _desk_totals(problem: HandoverProblem, used: Mapping[str, int]) -> list[dict]:
    return [
        {
            "desk_id": desk,
            "capacity": problem.quotas[desk],
            "used": int(used.get(desk, 0)),
            "remaining": problem.quotas[desk] - int(used.get(desk, 0)),
        }
        for desk in sorted(problem.quotas)
    ]


def _blockers(problem: HandoverProblem, options: Mapping[str, list[str]]) -> list[dict]:
    blockers: list[dict] = []
    structural = False
    for unit in problem.units:
        desks = options[unit.unit_id]
        if not desks:
            structural = True
            blockers.append(
                {
                    "unit_id": unit.unit_id,
                    "reason": "no-eligible-desk",
                    "detail": "no desk is eligible for every ticket in this unit",
                    "ticket_ids": list(unit.ticket_ids),
                }
            )
            continue
        tight = [
            {
                "desk_id": desk,
                "capacity": problem.quotas.get(desk),
                "required_slots": unit.size,
            }
            for desk in desks
            if problem.quotas.get(desk, 0) < unit.size
        ]
        if len(tight) == len(desks):
            structural = True
            blockers.append(
                {
                    "unit_id": unit.unit_id,
                    "reason": "insufficient-capacity",
                    "detail": "every eligible desk is short of the slots this unit needs",
                    "eligible_desks": tight,
                    "ticket_ids": list(unit.ticket_ids),
                }
            )
    if not blockers:
        # Every unit fits some desk on its own; the failure only appears when
        # their competing demands are combined against the shared quotas.
        blockers.append(
            {
                "unit_id": None,
                "reason": "capacity-conflict",
                "detail": (
                    "each unit has an eligible desk in isolation, but no combined "
                    "assignment respects every quota simultaneously"
                ),
            }
        )
    elif not structural:  # pragma: no cover - defensive: blockers implies structural
        pass
    return blockers
