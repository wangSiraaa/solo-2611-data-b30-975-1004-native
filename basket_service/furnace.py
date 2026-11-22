"""Furnace runs: assign indivisible cakes to sample orders, maximising plate value.

Each cake is atomic — it goes into at most one furnace order, and the factory
may *not* pool all its red and white and scoop freely.  An order is filled by
a set of cakes whose summed portions satisfy:

* total portions within ``[min_total, max_total]`` (the furnace capacity), and
* the red share of the total inside a rational closed interval
  ``[red_min, red_max]``.

A filled order yields one plate worth its declared positive integer value.
With at most 24 cakes and 8 orders the optimum is computed exactly: identical
recipes are grouped (same-recipe small cakes usually arrive in batches), and a
dynamic program over the remaining-count vector of each group enumerates each
order's feasible takes with rational pruning.  The state space is the product
of ``(count + 1)`` per distinct recipe; a guard rejects pathological spreads
instead of hanging.

Runs are persisted and idempotent: retrying a run id with the same orders
replays the stored plan (a lost response never double-consumes a cake), the
same id with different orders is a conflict, and cakes left unused stay in
stock for later runs.
"""

from __future__ import annotations

from math import gcd
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .db import Database, canonical_json, json_loads, utc_now
from .errors import (
    FURNACE_COMPLEXITY,
    FURNACE_CONFLICT,
    INVALID_ORDER,
    UNKNOWN_RUN,
    BasketError,
)
from .validation import is_int, require_id

MAX_ORDERS = 8
MAX_STATE_SPACE = 3_000_000


# ------------------------------------------------------------------ orders
def _fraction(value: Any, field: str) -> Tuple[int, int]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != 2
        or not is_int(value[0])
        or not is_int(value[1])
        or value[1] < 1
        or value[0] < 0
        or value[0] > value[1]
    ):
        raise BasketError(
            INVALID_ORDER,
            f"{field} must be [numerator, denominator] with 0 <= numerator <= denominator",
            {"field": field, "value": value},
        )
    num, den = int(value[0]), int(value[1])
    common = gcd(num, den)
    return (num // common, den // common)


def validate_orders(orders: Any) -> List[Dict[str, Any]]:
    if not isinstance(orders, Sequence) or isinstance(orders, (str, bytes)):
        raise BasketError(INVALID_ORDER, "orders must be a list")
    if not 1 <= len(orders) <= MAX_ORDERS:
        raise BasketError(INVALID_ORDER, f"a furnace run takes 1..{MAX_ORDERS} orders", {"orders": len(orders)})
    canonical: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for index, order in enumerate(orders):
        if not isinstance(order, Mapping):
            raise BasketError(INVALID_ORDER, "each order must be an object", {"index": index})
        order_id = require_id(order.get("order_id"), INVALID_ORDER, f"orders[{index}].order_id")
        if order_id in seen:
            raise BasketError(INVALID_ORDER, "duplicate order_id", {"order_id": order_id})
        seen.add(order_id)
        fraction = order.get("red_fraction")
        if not isinstance(fraction, Mapping):
            raise BasketError(INVALID_ORDER, "red_fraction must be an object with min/max", {"order_id": order_id})
        red_min = _fraction(fraction.get("min"), f"orders[{index}].red_fraction.min")
        red_max = _fraction(fraction.get("max"), f"orders[{index}].red_fraction.max")
        if red_min[0] * red_max[1] > red_max[0] * red_min[1]:
            raise BasketError(INVALID_ORDER, "red_fraction min exceeds max", {"order_id": order_id})
        min_total = order.get("min_total")
        max_total = order.get("max_total")
        value = order.get("value")
        if not is_int(min_total) or min_total < 1:
            raise BasketError(INVALID_ORDER, "min_total must be a positive integer", {"order_id": order_id})
        if not is_int(max_total) or max_total < min_total:
            raise BasketError(INVALID_ORDER, "max_total must be an integer >= min_total", {"order_id": order_id})
        if not is_int(value) or value < 1:
            raise BasketError(INVALID_ORDER, "value must be a positive integer", {"order_id": order_id})
        canonical.append(
            {
                "order_id": order_id,
                "red_fraction": {"min": list(red_min), "max": list(red_max)},
                "min_total": min_total,
                "max_total": max_total,
                "value": value,
            }
        )
    return canonical


# ------------------------------------------------------------------ solver
def _feasible_takes(
    order: Mapping[str, Any],
    state: Tuple[int, ...],
    reds: Sequence[int],
    totals: Sequence[int],
    suffix_red: Sequence[int],
    suffix_total: Sequence[int],
) -> List[Tuple[int, ...]]:
    """Every count vector within ``state`` that fills the order exactly."""

    (lo_num, lo_den), (hi_num, hi_den) = order["red_fraction"]["min"], order["red_fraction"]["max"]
    min_total, max_total = order["min_total"], order["max_total"]
    n_groups = len(state)
    takes: List[Tuple[int, ...]] = []
    take = [0] * n_groups

    def rec(group: int, red: int, total: int) -> None:
        if total > max_total:
            return
        if total + suffix_total[group] < min_total:
            return
        # Conservative rational pruning of the red-share interval: even the
        # reddest completion must reach lo, and the least red one must not
        # exceed hi, over the attainable total range.
        if (red + suffix_red[group]) * lo_den < lo_num * max(total, min_total):
            return
        if red * hi_den > hi_num * min(total + suffix_total[group], max_total):
            return
        if group == n_groups:
            if lo_num * total <= red * lo_den and red * hi_den <= hi_num * total:
                takes.append(tuple(take))
            return
        available = state[group]
        for k in range(available + 1):
            take[group] = k
            rec(group + 1, red + k * reds[group], total + k * totals[group])
        take[group] = 0

    rec(0, 0, 0)
    return takes


def solve(cakes: Sequence[Mapping[str, Any]], orders: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Exact optimum: assign whole cakes to orders, maximising total plate value.

    ``cakes`` are ``{"batch_no", "red", "white"}``; ``orders`` are validated
    canonical orders.  Returns the chosen plate assignment (order id -> sorted
    batch numbers) and the leftover stock.  Deterministic: identical inputs
    always yield the identical plan.
    """

    groups: Dict[Tuple[int, int], List[str]] = {}
    for cake in cakes:
        groups.setdefault((cake["red"], cake["white"]), []).append(cake["batch_no"])
    recipes = sorted(groups)
    for batch_nos in groups.values():
        batch_nos.sort()
    counts = tuple(len(groups[recipe]) for recipe in recipes)
    n_groups = len(recipes)
    reds = [recipe[0] for recipe in recipes]
    totals = [recipe[0] + recipe[1] for recipe in recipes]

    state_space = 1
    for count in counts:
        state_space *= count + 1
    if state_space > MAX_STATE_SPACE:
        raise BasketError(
            FURNACE_COMPLEXITY,
            "too many distinct cake recipes for an exact plan; split the run",
            {"distinct_recipes": n_groups, "state_space": state_space, "limit": MAX_STATE_SPACE},
        )

    # dp: remaining-count vector -> best total plate value achievable so far.
    dp: Dict[Tuple[int, ...], int] = {counts: 0}
    parents: List[Dict[Tuple[int, ...], Tuple[Tuple[int, ...], Tuple[int, ...]]]] = []
    for order in orders:
        nxt = dict(dp)  # the order may also be left unfilled
        parent: Dict[Tuple[int, ...], Tuple[Tuple[int, ...], Tuple[int, ...]]] = {}
        for state, value in dp.items():
            suffix_red = [0] * (n_groups + 1)
            suffix_total = [0] * (n_groups + 1)
            for i in range(n_groups - 1, -1, -1):
                suffix_red[i] = suffix_red[i + 1] + state[i] * reds[i]
                suffix_total[i] = suffix_total[i + 1] + state[i] * totals[i]
            for take in _feasible_takes(order, state, reds, totals, suffix_red, suffix_total):
                new_state = tuple(state[i] - take[i] for i in range(n_groups))
                new_value = value + order["value"]
                if new_value > nxt.get(new_state, -1):
                    nxt[new_state] = new_value
                    parent[new_state] = (state, take)
        parents.append(parent)
        dp = nxt

    best_value = max(dp.values())
    best_state = min(state for state, value in dp.items() if value == best_value)

    takes_by_order: List[Optional[Tuple[int, ...]]] = [None] * len(orders)
    state = best_state
    for index in range(len(orders) - 1, -1, -1):
        edge = parents[index].get(state)
        if edge is not None:
            state, take = edge
            takes_by_order[index] = take

    remaining = {recipe: list(groups[recipe]) for recipe in recipes}
    plates: List[Dict[str, Any]] = []
    for index, take in enumerate(takes_by_order):
        if take is None:
            continue
        batch_nos: List[str] = []
        for group_index, k in enumerate(take):
            if k:
                batch_nos.extend(remaining[recipes[group_index]][:k])
                del remaining[recipes[group_index]][:k]
        plates.append({"order_id": orders[index]["order_id"], "batch_nos": sorted(batch_nos)})
    leftover = sorted(batch_no for batch_nos in remaining.values() for batch_no in batch_nos)
    return {"total_value": best_value, "plates": plates, "leftover": leftover}


# ------------------------------------------------------------------ service
class FurnaceService:
    def __init__(self, db: Database):
        self.db = db

    def run(self, run_id: str, orders: Any) -> Dict[str, Any]:
        run_id = require_id(run_id, INVALID_ORDER, "run_id")
        canonical_orders = validate_orders(orders)
        orders_json = canonical_json(canonical_orders)
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT orders_json, result_json FROM furnace_runs WHERE run_id = ?", (run_id,)).fetchone()
            if existing is not None:
                if existing["orders_json"] != orders_json:
                    raise BasketError(
                        FURNACE_CONFLICT,
                        "run_id reused with different orders",
                        {"run_id": run_id},
                    )
                return json_loads(existing["result_json"])

            stock = conn.execute(
                "SELECT batch_no, red, white FROM cakes WHERE status = 'IN_STOCK' ORDER BY batch_no"
            ).fetchall()
            cakes = [{"batch_no": row["batch_no"], "red": row["red"], "white": row["white"]} for row in stock]
            plan = solve(cakes, canonical_orders)

            content_by_batch = {
                row["batch_no"]: json_loads(row["content_json"])
                for row in conn.execute("SELECT batch_no, content_json FROM cakes").fetchall()
            }
            plates: List[Dict[str, Any]] = []
            used: List[str] = []
            for plate in plan["plates"]:
                order = next(order for order in canonical_orders if order["order_id"] == plate["order_id"])
                plate_cakes = []
                items: List[Dict[str, Any]] = []
                red = white = 0
                for batch_no in plate["batch_nos"]:
                    content = content_by_batch[batch_no]
                    red += content["red"]
                    white += content["white"]
                    plate_cakes.append(
                        {"batch_no": batch_no, "kind": content["kind"], "red": content["red"], "white": content["white"]}
                    )
                    items.extend(self._items_of(content))
                    used.append(batch_no)
                total = red + white
                share = gcd(red, total)
                plates.append(
                    {
                        "order_id": plate["order_id"],
                        "value": order["value"],
                        "red": red,
                        "white": white,
                        "total": total,
                        "red_fraction": [red // share, total // share],
                        "cakes": plate_cakes,
                        # The physical requisition record: which returned items
                        # (sale, unit, return request) were burned into this plate.
                        "items": sorted(items, key=lambda item: (item["sale_id"], item["unit_id"])),
                    }
                )

            for batch_no in used:
                cursor = conn.execute(
                    "UPDATE cakes SET status = 'REQUISITIONED', requisition_id = ? WHERE batch_no = ? AND status = 'IN_STOCK'",
                    (run_id, batch_no),
                )
                if cursor.rowcount != 1:  # pragma: no cover - defensive
                    raise BasketError(FURNACE_CONFLICT, "cake changed concurrently", {"batch_no": batch_no})

            filled = {plate["order_id"] for plate in plates}
            result = {
                "ok": True,
                "run_id": run_id,
                "orders": canonical_orders,
                "total_value": plan["total_value"],
                "plates": plates,
                "unfilled_orders": [order["order_id"] for order in canonical_orders if order["order_id"] not in filled],
                "stock_remaining": [
                    {"batch_no": batch_no, "red": content_by_batch[batch_no]["red"], "white": content_by_batch[batch_no]["white"]}
                    for batch_no in plan["leftover"]
                ],
                "repeated": False,
            }
            conn.execute(
                "INSERT INTO furnace_runs (run_id, orders_json, result_json, created_at) VALUES (?, ?, ?, ?)",
                (run_id, orders_json, canonical_json(result), utc_now()),
            )
            return result

    @staticmethod
    def _items_of(content: Mapping[str, Any]) -> List[Dict[str, Any]]:
        if content["kind"] == "UNIT":
            return [{"sale_id": content["sale_id"], "unit_id": content["unit_id"], "request_id": content["request_id"]}]
        return [
            {"sale_id": content["sale_id"], "unit_id": member["unit_id"], "request_id": member["request_id"]}
            for member in content["members"]
        ]

    def get_run(self, run_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT result_json FROM furnace_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_RUN, "unknown furnace run", {"run_id": run_id})
        result = json_loads(row["result_json"])
        result["repeated"] = True
        return result
