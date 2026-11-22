"""Physical coupon paper: current pieces, tear sequences and group planning.

This module owns everything that turns a *sheet spec* (see :mod:`sheets`) into
physical actions and into basket totals:

* :class:`PaperState` - the pieces a sheet currently consists of.  It is the
  single source of truth for what is still on paper; committed hand-overs are
  subtracted from it and it never stores duplicates.
* :func:`tear_plan` - a *real* ordered tear sequence that isolates one or more
  target rectangles simultaneously, or ``None`` when no legal sequence exists.
  Plans are computed on a pure copy, so trials never move paper.
* :func:`plan_group` - the joint optimisation over several baskets paid
  together: maximise total discount subject to per-ticket physical paper, one
  redemption per basket per promotion, the old per-basket deterministic
  ordering, and a deterministic cross-basket split.

Trial planning is side-effect free.  Only the service layer commits a plan,
and it does so by re-checking the current pieces inside one transaction.
"""

from __future__ import annotations

import itertools
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import optimizer
from . import sheets
from .errors import INVALID_GROUP, NO_FEASIBLE_PLAN, BasketError
from .sheets import Edge, Rect
from .validation import allocate_report, require_id, validate_basket
from .db import canonical_json

# Hard ceiling for the exact cross-basket search.  The brief's realistic
# instances (a few accepted coupon types, or few sheets with many conflicting
# coupons) stay far below it; it only guards pathological inputs.
SEARCH_BUDGET = 500_000

MAX_BASKETS_PER_GROUP = 5
MAX_UNITS_PER_BASKET = 10
MAX_DISTINCT_TYPES_PER_BASKET = 16
PLAN_WORK_BUDGET = 400_000

TicketInstance = Dict[str, Any]


# ---------------------------------------------------------------------------
# physical state
# ---------------------------------------------------------------------------


class PaperState:
    """The current rectangles of one sheet, plus its effective punches."""

    __slots__ = ("sheet", "edges", "pieces")

    def __init__(self, sheet: Mapping[str, Any], edges: FrozenSet[Edge], pieces: Sequence[Rect]):
        self.sheet = sheet
        self.edges = edges
        self.pieces: List[Rect] = sorted((tuple(p) for p in pieces), key=sheets.rect_key)  # type: ignore[arg-type]

    @classmethod
    def whole(cls, sheet: Mapping[str, Any], corrections: Iterable[Any] = ()) -> "PaperState":
        return cls(sheet, sheets.effective_edges(sheet, corrections), [sheets.whole_sheet_rect(sheet["rows"], sheet["cols"])])

    @classmethod
    def from_json(cls, spec: Mapping[str, Any], corrections: Iterable[Any], pieces: Sequence[Sequence[int]]) -> "PaperState":
        return cls(spec, sheets.effective_edges(spec, corrections), [tuple(p) for p in pieces])  # type: ignore[arg-type]

    def copy(self) -> "PaperState":
        return PaperState(self.sheet, self.edges, list(self.pieces))

    def to_json(self) -> List[List[int]]:
        return [list(p) for p in self.pieces]

    def area(self) -> int:
        return sum(sheets.rect_area(p) for p in self.pieces)

    def owner(self, rect: Rect) -> Optional[Rect]:
        for piece in self.pieces:
            if sheets.rect_contains(piece, rect):
                return piece
        return None

    def has_piece(self, rect: Rect) -> bool:
        return any(tuple(p) == tuple(rect) for p in self.pieces)

    def remove_rect(self, rect: Rect) -> None:
        self.pieces = [p for p in self.pieces if tuple(p) != tuple(rect)]
        self.pieces.sort(key=sheets.rect_key)

    def consistency_error(self) -> Optional[str]:
        whole = sheets.whole_sheet_rect(self.sheet["rows"], self.sheet["cols"])
        for i, piece in enumerate(self.pieces):
            if not sheets.rect_contains(whole, piece):
                return f"piece {list(piece)} outside sheet"
            for other in self.pieces[i + 1:]:
                if not sheets.rects_disjoint(piece, other):
                    return f"pieces {list(piece)} and {list(other)} overlap"
        # Remaining paper need not tile the sheet: handed-over pieces leave
        # genuine holes.  Overlap/out-of-bounds are the invariants that matter.
        return None


# ---------------------------------------------------------------------------
# tearing
# ---------------------------------------------------------------------------


def _can_split(piece: Rect, kind: str, at: int, edges: FrozenSet[Edge]) -> bool:
    return all(e in edges for e in sheets.cut_edges(piece, kind, at))


def _step(piece: Rect, kind: str, at: int, left: Rect, right: Rect, edges: FrozenSet[Edge]) -> Dict[str, Any]:
    return {
        "piece": list(piece),
        "kind": kind,
        "at": at,
        "required_edges": [sheets.edge_str(e) for e in sheets.cut_edges(piece, kind, at) if e in edges],
        "produces": [list(left), list(right)],
    }


def _targets_in(piece: Rect, targets: Sequence[Rect]) -> List[Rect]:
    return [t for t in targets if sheets.rect_contains(piece, t)]


def _cut_is_safe(piece: Rect, piece_targets: Sequence[Rect], kind: str, at: int) -> bool:
    """A cut is safe when every target inside ``piece`` stays whole on one side."""

    left, right = sheets.split_rect(piece, kind, at)
    for target in piece_targets:
        if target == left or target == right:
            continue
        if not (sheets.rect_contains(left, target) or sheets.rect_contains(right, target)):
            return False
    return True


def _plan_pieces(
    pieces: Sequence[Rect],
    targets: Sequence[Rect],
    edges: FrozenSet[Edge],
    memo,
) -> Optional[Tuple[List[Rect], List[Dict[str, Any]]]]:
    """DFS: split pieces (without ever cutting across a target) until each
    target equals an existing piece.  Returns ``(final_pieces, steps)``."""

    pending = [t for t in targets if not any(tuple(p) == tuple(t) for p in pieces)]
    if not pending:
        return sorted(pieces, key=sheets.rect_key), []
    key = (tuple(sorted(pieces, key=sheets.rect_key)), tuple(sorted(pending, key=sheets.rect_key)), edges)
    if key in memo:
        return memo[key]

    # Choose the tightest piece that still needs work (fewest cells) to keep
    # the branching small and deterministic.  A piece that already equals one
    # of its pending targets cannot happen (such a target is no longer pending);
    # a piece with no pending target inside is left as remainder.
    candidates = []
    for piece in pieces:
        inside = _targets_in(piece, pending)
        if not inside:
            continue
        candidates.append((sheets.rect_area(piece), piece, inside))
    candidates.sort(key=lambda c: (c[0], sheets.rect_key(c[1])))
    if not candidates:
        memo[key] = None
        return None
    _, piece, inside = candidates[0]

    r0, c0, r1, c1 = piece
    options: List[Tuple[int, str, int]] = []
    for at in range(r0, r1):
        if _cut_is_safe(piece, inside, "H", at) and _can_split(piece, "H", at, edges):
            left, right = sheets.split_rect(piece, "H", at)
            options.append((0 if any(t in (left, right) for t in inside) else 1, "H", at))
    for at in range(c0, c1):
        if _cut_is_safe(piece, inside, "V", at) and _can_split(piece, "V", at, edges):
            left, right = sheets.split_rect(piece, "V", at)
            options.append((0 if any(t in (left, right) for t in inside) else 1, "V", at))
    options.sort()
    for _, kind, at in options:
        left, right = sheets.split_rect(piece, kind, at)
        nxt = [p for p in pieces if tuple(p) != tuple(piece)] + [left, right]
        found = _plan_pieces(nxt, targets, edges, memo)
        if found is not None:
            final, steps = found
            memo[key] = (final, [_step(piece, kind, at, left, right, edges)] + steps)
            return memo[key]
    memo[key] = None
    return None


def tear_plan(state: PaperState, targets: Sequence[Rect]) -> Optional[List[Dict[str, Any]]]:
    """Real ordered tear sequence isolating every target simultaneously.

    Returns the empty list for an empty target set.  Returns ``None`` when the
    current punches cannot isolate all targets, or when two targets share one
    rectangle (front and back of one piece cannot back two claims).
    """

    wanted = [tuple(t) for t in targets]  # type: ignore[misc]
    if not wanted:
        return []
    if len(set(wanted)) != len(wanted):
        return None
    whole = sheets.whole_sheet_rect(state.sheet["rows"], state.sheet["cols"])
    for target in wanted:
        if not sheets.rect_contains(whole, target):
            return None
    for i in range(len(wanted)):
        for j in range(i + 1, len(wanted)):
            if not sheets.rects_disjoint(wanted[i], wanted[j]):
                return None
    found = _plan_pieces(list(state.pieces), wanted, state.edges, {})
    if found is None:
        return None
    return found[1]


def isolate(state: PaperState, rect: Rect) -> Optional[List[Dict[str, Any]]]:
    return tear_plan(state, [rect])


def apply_tears(state: PaperState, targets: Sequence[Rect]) -> Optional[Tuple[List[Rect], List[Dict[str, Any]]]]:
    """Tear ``targets`` off a copy; return ``(remaining_pieces, steps)``."""

    wanted = [tuple(t) for t in targets]  # type: ignore[misc]
    found = _plan_pieces(list(state.pieces), wanted, state.edges, {})
    if found is None:
        return None
    final, steps = found
    remaining = [p for p in final if tuple(p) not in {tuple(t) for t in wanted}]
    scratch = PaperState(state.sheet, state.edges, remaining)
    if scratch.consistency_error():  # pragma: no cover - defensive
        return None
    return scratch.pieces, steps


def submission_rects(sheet: Mapping[str, Any], ticket_ids: Sequence[str]) -> List[Rect]:
    """Resolve a customer-set trial (ticket ids) into target rectangles."""

    by_id = {t["ticket_id"]: t for t in sheet["tickets"]}
    rects: List[Rect] = []
    for ticket_id in ticket_ids:
        ticket = by_id.get(ticket_id)
        if ticket is None:
            raise BasketError(INVALID_GROUP, "unknown ticket in submission", {"sheet_id": sheet["sheet_id"], "ticket_id": ticket_id})
        rects.append(tuple(ticket["rect"]))  # type: ignore[arg-type]
    if len(set(rects)) != len(rects):
        # Two tickets share a rectangle (front/back): one hand-over cannot back
        # two separate claims.
        raise BasketError(INVALID_GROUP, "submission asks for two tickets on one rectangle", {"sheet_id": sheet["sheet_id"]})
    return rects


# ---------------------------------------------------------------------------
# tickets -> basket quote options
# ---------------------------------------------------------------------------


def _coupon_def(ticket: Mapping[str, Any]) -> Dict[str, Any]:
    return {"coupon_id": ticket["coupon_id"], "D": ticket["D"], "requires": dict(ticket["requires"])}


def _instance_available(state: PaperState, ticket: Mapping[str, Any]) -> bool:
    return state.owner(tuple(ticket["rect"])) is not None  # type: ignore[arg-type]


def collect_instances(
    states: Mapping[str, PaperState],
    accepted_coupons: Optional[Iterable[str]] = None,
) -> Tuple[Dict[str, List[TicketInstance]], Dict[str, Dict[str, Any]]]:
    """Ticket instances physically present, grouped by coupon id.

    Also returns the canonical coupon definition per coupon id; tickets that
    share a coupon id must agree on ``D`` and ``requires`` (they are the same
    promotion printed more than once).
    """

    accepted = None if accepted_coupons is None else set(accepted_coupons)
    instances: Dict[str, List[TicketInstance]] = {}
    defs: Dict[str, Dict[str, Any]] = {}
    for sheet_id in sorted(states):
        state = states[sheet_id]
        for ticket in state.sheet["tickets"]:
            if accepted is not None and ticket["coupon_id"] not in accepted:
                continue
            if not _instance_available(state, ticket):
                continue
            definition = _coupon_def(ticket)
            existing = defs.get(ticket["coupon_id"])
            if existing is not None and canonical_json(existing) != canonical_json(definition):
                raise BasketError(
                    INVALID_GROUP,
                    "the same promotion is printed with different terms",
                    {"coupon_id": ticket["coupon_id"]},
                )
            defs[ticket["coupon_id"]] = definition
            instances.setdefault(ticket["coupon_id"], []).append(
                {
                    "coupon_id": ticket["coupon_id"],
                    "sheet_id": sheet_id,
                    "ticket_id": ticket["ticket_id"],
                    "side": ticket["side"],
                    "rect": tuple(ticket["rect"]),
                }
            )
    for values in instances.values():
        values.sort(key=lambda i: (i["sheet_id"], i["ticket_id"], i["rect"]))
    return instances, defs


def _requirements_met(units: Sequence[Mapping[str, Any]], coupon: Mapping[str, Any]) -> bool:
    have: Dict[str, int] = {}
    for unit in units:
        have[unit["sku"]] = have.get(unit["sku"], 0) + 1
    for sku, count in coupon["requires"].items():
        if have.get(sku, 0) < count:
            return False
    return True


def _individually_feasible(units: Sequence[Mapping[str, Any]], coupon: Mapping[str, Any]) -> bool:
    if not _requirements_met(units, coupon):
        return False
    masks, _ = optimizer.solve(units, [coupon])
    return bool(masks)


def basket_plans(
    units: Sequence[Mapping[str, Any]],
    defs: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """All feasible fully-used coupon subsets for one basket.

    Each plan carries the official old-rule quote (assignments, proportional
    allocation, total) so the result can be bound by the ledger unchanged.
    """

    usable = []
    for coupon_id in sorted(defs):
        if _individually_feasible(units, defs[coupon_id]):
            usable.append(coupon_id)
    if len(usable) > MAX_DISTINCT_TYPES_PER_BASKET:
        raise BasketError(
            INVALID_GROUP,
            f"at most {MAX_DISTINCT_TYPES_PER_BASKET} distinct accepted coupon types can be planned exactly",
            {"count": len(usable), "limit": MAX_DISTINCT_TYPES_PER_BASKET},
        )

    plans: List[Dict[str, Any]] = []
    max_units = len(units)
    work = 0
    for size in range(0, min(len(usable), max_units) + 1):
        for combo in itertools.combinations(usable, size):
            work += 1
            if work > PLAN_WORK_BUDGET:
                raise BasketError(INVALID_GROUP, "basket plan enumeration exceeded its work budget", {"budget": PLAN_WORK_BUDGET})
            coupons = [dict(defs[c]) for c in combo]
            masks, total = optimizer.solve(units, coupons)
            used = frozenset(masks)
            if used != frozenset(combo):
                # The optimizer dropped a coupon; the same quote is reachable
                # from the smaller subset and will be enumerated there.
                continue
            plans.append(_make_plan(units, coupons, masks, total))
    # Deterministic: most discount first, then smaller coupon-id tuple.
    plans.sort(key=lambda p: (-p["discount"], p["coupon_ids"]))
    return plans


def _make_plan(
    units: Sequence[Mapping[str, Any]],
    coupons: Sequence[Mapping[str, Any]],
    masks: Mapping[str, int],
    total: int,
) -> Dict[str, Any]:
    mapping = {
        coupon_id: sorted(units[i]["unit_id"] for i in range(len(units)) if mask >> i & 1)
        for coupon_id, mask in masks.items()
    }
    mapping = {cid: ids for cid, ids in sorted(mapping.items())}
    discounts = {c["coupon_id"]: c["D"] for c in coupons}
    allocation = allocate_report(units, masks, discounts)
    return {
        "coupon_ids": tuple(sorted(masks)),
        "coupons": [dict(c) for c in coupons],
        "assignments": mapping,
        "allocation": allocation,
        "total": total,
        "discount": sum(discounts.values()),
    }


# ---------------------------------------------------------------------------
# cross-basket optimisation + physical assignment
# ---------------------------------------------------------------------------


class _SearchBudget:
    __slots__ = ("left",)

    def __init__(self, budget: int):
        self.left = budget

    def spend(self) -> None:
        self.left -= 1
        if self.left < 0:
            raise BasketError(INVALID_GROUP, "exact group search exceeded its budget", {"budget": SEARCH_BUDGET})


def assign_instances(
    usages: Sequence[Tuple[str, str]],
    instances: Mapping[str, Sequence[TicketInstance]],
    states: Mapping[str, PaperState],
) -> Optional[Dict[str, List[TicketInstance]]]:
    """Choose one distinct physical ticket instance per usage.

    ``usages`` is a deterministic list of ``(basket_id, coupon_id)``.  The
    chosen rectangles must be pairwise disjoint and simultaneously tearable on
    every sheet.  Returns ``coupon_id -> instances aligned with its usages``.
    """

    by_coupon: Dict[str, List[str]] = {}
    for basket_id, coupon_id in usages:
        by_coupon.setdefault(coupon_id, []).append(basket_id)
    coupon_order = sorted(by_coupon)
    assigned: Dict[str, List[TicketInstance]] = {}
    chosen_by_sheet: Dict[str, List[Rect]] = {}

    def free(instance: TicketInstance) -> bool:
        sheet_id = instance["sheet_id"]
        rect = instance["rect"]
        return all(sheets.rects_disjoint(rect, other) for other in chosen_by_sheet.get(sheet_id, ()))

    def tearable_so_far() -> bool:
        for sheet_id, rects in chosen_by_sheet.items():
            if tear_plan(states[sheet_id], rects) is None:
                return False
        return True

    def backtrack(position: int, budget: _SearchBudget) -> bool:
        if position == len(coupon_order):
            return tearable_so_far()
        coupon_id = coupon_order[position]
        picks = by_coupon[coupon_id]
        pool = list(instances.get(coupon_id, ()))
        if len(pool) < len(picks):
            return False
        for combo in itertools.combinations(range(len(pool)), len(picks)):
            budget.spend()
            picked = [pool[i] for i in combo]
            if not all(free(instance) for instance in picked):
                continue
            for instance in picked:
                chosen_by_sheet.setdefault(instance["sheet_id"], []).append(instance["rect"])
            assigned[coupon_id] = picked
            if tearable_so_far() and backtrack(position + 1, budget):
                return True
            for instance in picked:
                chosen_by_sheet[instance["sheet_id"]].remove(instance["rect"])
            del assigned[coupon_id]
        return False

    if backtrack(0, _SearchBudget(SEARCH_BUDGET)):
        return assigned
    return None


def plan_group(
    baskets: Sequence[Mapping[str, Any]],
    states: Mapping[str, PaperState],
    *,
    accepted_coupons: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Jointly optimal physical quote for several baskets paid together.

    ``baskets`` is a list of ``{"basket_id": str, "units": [...]}``.  Units are
    validated with the original basket rules.  ``accepted_coupons`` restricts
    which printed promotions the merchant honours this session.  The result is
    a *trial*: callers must commit it through the service to move any paper.
    """

    if isinstance(baskets, (str, bytes)) or not isinstance(baskets, Sequence) or not baskets:
        raise BasketError(INVALID_GROUP, "baskets must be a non-empty list")
    if len(baskets) > MAX_BASKETS_PER_GROUP:
        raise BasketError(
            INVALID_GROUP,
            f"a group pays at most {MAX_BASKETS_PER_GROUP} baskets together",
            {"limit": MAX_BASKETS_PER_GROUP, "count": len(baskets)},
        )
    canonical_baskets: List[Dict[str, Any]] = []
    seen_basket_ids: set[str] = set()
    for index, item in enumerate(baskets):
        if not isinstance(item, Mapping):
            raise BasketError(INVALID_GROUP, "basket entries must be objects", {"index": index})
        basket_id = require_id(item.get("basket_id"), INVALID_GROUP, f"baskets[{index}].basket_id")
        if basket_id in seen_basket_ids:
            raise BasketError(INVALID_GROUP, "duplicate basket_id", {"basket_id": basket_id})
        seen_basket_ids.add(basket_id)
        units = validate_basket(item.get("units"))
        if len(units) > MAX_UNITS_PER_BASKET:
            raise BasketError(
                INVALID_GROUP,
                f"a basket holds at most {MAX_UNITS_PER_BASKET} units",
                {"basket_id": basket_id, "limit": MAX_UNITS_PER_BASKET},
            )
        canonical_baskets.append({"basket_id": basket_id, "units": units})
    canonical_baskets.sort(key=lambda b: b["basket_id"])

    if accepted_coupons is not None:
        accepted_coupons = sorted(set(accepted_coupons))
    instances, defs = collect_instances(states, accepted_coupons)
    # No accepted/physical promotion is not an error: every basket simply pays
    # full price and no paper moves.  (This is the "many sheets, few honoured
    # coupon types" case where the honoured set can be empty.)

    per_basket: List[List[Dict[str, Any]]] = [basket_plans(b["units"], defs) for b in canonical_baskets]
    suffix_max = [0] * (len(canonical_baskets) + 1)
    for i in range(len(canonical_baskets) - 1, -1, -1):
        suffix_max[i] = suffix_max[i + 1] + (per_basket[i][0]["discount"] if per_basket[i] else 0)

    budget = _SearchBudget(SEARCH_BUDGET)
    best: Dict[str, Any] = {"discount": -1, "plans": None, "assignment": None}

    counts: Dict[str, int] = {}

    def dfs(index: int, discount: int, chosen: List[Dict[str, Any]]) -> None:
        budget.spend()
        if best["plans"] is not None and discount + suffix_max[index] <= best["discount"]:
            return
        if index == len(canonical_baskets):
            usages = [(canonical_baskets[i]["basket_id"], cid) for i, plan in enumerate(chosen) for cid in plan["coupon_ids"]]
            assignment = assign_instances(usages, instances, states)
            if assignment is None:
                return
            best["discount"] = discount
            best["plans"] = list(chosen)
            best["assignment"] = assignment
            return
        for plan in per_basket[index]:
            if discount + plan["discount"] + suffix_max[index + 1] <= best["discount"] and best["plans"] is not None:
                break
            feasible = True
            for coupon_id in plan["coupon_ids"]:
                if counts.get(coupon_id, 0) + 1 > len(instances.get(coupon_id, ())):
                    feasible = False
                    break
            if not feasible:
                continue
            for coupon_id in plan["coupon_ids"]:
                counts[coupon_id] = counts.get(coupon_id, 0) + 1
            chosen.append(plan)
            dfs(index + 1, discount + plan["discount"], chosen)
            chosen.pop()
            for coupon_id in plan["coupon_ids"]:
                counts[coupon_id] -= 1
                if counts[coupon_id] == 0:
                    del counts[coupon_id]

    dfs(0, 0, [])
    if best["plans"] is None:
        raise BasketError(NO_FEASIBLE_PLAN, "no physically consistent way to honour these baskets", {})

    return _build_result(canonical_baskets, best["plans"], best["assignment"], instances, states, defs)


def _build_result(
    canonical_baskets: Sequence[Mapping[str, Any]],
    plans: Sequence[Mapping[str, Any]],
    assignment: Mapping[str, Sequence[TicketInstance]],
    instances: Mapping[str, Sequence[TicketInstance]],
    states: Mapping[str, PaperState],
    defs: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    # Each coupon's chosen instances are handed out to the baskets that use it
    # in the deterministic basket order used while searching.
    cursor: Dict[str, int] = {}
    basket_rows: List[Dict[str, Any]] = []
    handovers: List[Dict[str, Any]] = []
    targets_by_sheet: Dict[str, List[Rect]] = {}
    total_paid = 0
    original_total = 0
    total_discount = 0
    for basket, plan in zip(canonical_baskets, plans):
        paper = []
        for coupon_id in plan["coupon_ids"]:
            position = cursor.get(coupon_id, 0)
            instance = assignment[coupon_id][position]
            cursor[coupon_id] = position + 1
            rect = tuple(instance["rect"])
            targets_by_sheet.setdefault(instance["sheet_id"], []).append(rect)
            paper.append(
                {
                    "coupon_id": coupon_id,
                    "sheet_id": instance["sheet_id"],
                    "ticket_id": instance["ticket_id"],
                    "side": instance["side"],
                    "rect": list(rect),
                }
            )
            handovers.append(
                {
                    "basket_id": basket["basket_id"],
                    "coupon_id": coupon_id,
                    "sheet_id": instance["sheet_id"],
                    "ticket_id": instance["ticket_id"],
                    "side": instance["side"],
                    "rect": list(rect),
                    # Taking this piece hands over both faces; record every
                    # ticket printed on the same rectangle as consumed.
                    "consumes_ticket_ids": sorted(
                        t["ticket_id"] for t in states[instance["sheet_id"]].sheet["tickets"] if tuple(t["rect"]) == rect
                    ),
                }
            )
        quote = _official_quote(basket, plan)
        basket_rows.append(
            {
                "basket_id": basket["basket_id"],
                "coupon_ids": list(plan["coupon_ids"]),
                "total": quote["total"],
                "original_total": quote["original_total"],
                "total_discount": quote["total_discount"],
                "quote": quote,
                "paper": paper,
            }
        )
        total_paid += quote["total"]
        original_total += quote["original_total"]
        total_discount += quote["total_discount"]

    tears: Dict[str, List[Dict[str, Any]]] = {}
    projected_remaining: Dict[str, List[List[int]]] = {}
    for sheet_id, rects in targets_by_sheet.items():
        steps = tear_plan(states[sheet_id], rects)
        if steps is None:  # pragma: no cover - assignment already checked
            raise BasketError(NO_FEASIBLE_PLAN, "chosen tickets are not simultaneously tearable", {"sheet_id": sheet_id})
        tears[sheet_id] = steps
        applied = apply_tears(states[sheet_id], rects)
        if applied is None:  # pragma: no cover - defensive
            raise BasketError(NO_FEASIBLE_PLAN, "tear application failed", {"sheet_id": sheet_id})
        projected_remaining[sheet_id] = applied[0]

    result = {
        "ok": True,
        "baskets": basket_rows,
        "handovers": sorted(handovers, key=lambda h: (h["sheet_id"], h["ticket_id"], h["basket_id"])),
        "tears": tears,
        "projected_remaining": projected_remaining,
        "original_total": original_total,
        "total_paid": total_paid,
        "total_discount": total_discount,
    }
    return result


def _official_quote(basket: Mapping[str, Any], plan: Mapping[str, Any]) -> Dict[str, Any]:
    units = basket["units"]
    coupons = [dict(c) for c in plan["coupons"]]
    discounts = {c["coupon_id"]: c["D"] for c in coupons}
    masks = {
        coupon_id: sum(1 << i for i, unit in enumerate(units) if unit["unit_id"] in set(ids))
        for coupon_id, ids in plan["assignments"].items()
    }
    allocation = allocate_report(units, masks, discounts)
    original_total = sum(u["price"] for u in units)
    return {
        "request_id": f"paper:{basket['basket_id']}",
        "snapshot_id": "paper",
        "basket": [dict(u) for u in units],
        "coupons": coupons,
        "assignments": {cid: list(ids) for cid, ids in plan["assignments"].items()},
        "coupon_ids": sorted(plan["assignments"]),
        "allocation": allocation,
        "total": plan["total"],
        "original_total": original_total,
        "total_discount": sum(discounts.values()),
    }


__all__ = [
    "PaperState",
    "tear_plan",
    "isolate",
    "apply_tears",
    "submission_rects",
    "collect_instances",
    "basket_plans",
    "assign_instances",
    "plan_group",
]
