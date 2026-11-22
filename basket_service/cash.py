"""Cash receipts, IOUs and the shared change-cart transport office.

Two ledgers live side by side and never blur their units:

* the *exact* ledger (``sale_units`` / ``return_items``) keeps integer-cent
  contract amounts, including every per-unit refund;
* the *cash* ledger only ever moves multiples of five.  When the exact
  cumulative refund of one cash sale reaches ``R``, the cumulative cash owed is
  ``round5(R)`` and each receipt's actual cash is the difference between the
  new and the previous rounded cumulative.  A zero difference still consumes
  the original return eligibility but creates no IOU.

The transport office owns sealed bags (whole-bag delivery, at most once),
per-counter coin inventories and the IOUs counters hand out.  ``plan_day``
solves the *exact* minimum-freight assignment with a branch-and-bound search
and a deterministic largest-denomination-first coin payment; ``apply_plan``
and ``pay_iou`` are the only operations that move coins, inside write
transactions, so a retried payment never issues coins twice.
"""

from __future__ import annotations

import itertools
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .db import Database, canonical_json, json_loads, utc_now
from .errors import (
    BAG_CONFLICT,
    INSUFFICIENT_COINS,
    INCORRECT_TENDER,
    INVALID_DENOMINATION,
    INVALID_INPUT,
    IOU_ALREADY_PAID,
    IOU_CONFLICT,
    IOU_SLOT_TAKEN,
    PLAN_CONFLICT,
    PLAN_TOO_LARGE,
    UNKNOWN_BAG,
    UNKNOWN_COUNTER,
    UNKNOWN_DISBURSEMENT,
    UNKNOWN_IOU,
    UNKNOWN_PLAN,
    UNKNOWN_RECEIPT,
    BasketError,
)
from .validation import is_int, require_id

DENOMINATIONS: Tuple[int, ...] = (5, 10, 25)
PAYMENT_METHODS = ("CASH", "TRANSFER")
DEFAULT_NODE_BUDGET = 200_000


def round5(cents: int) -> int:
    """Round integer cents to the nearest multiple of five.

    Remainders 0, 1 and 2 round down; 3 and 4 round up (the brief's rule).
    """

    if not is_int(cents) or cents < 0:
        raise BasketError(INVALID_INPUT, "round5 expects a non-negative integer amount", {"value": cents})
    return 5 * ((cents + 2) // 5)


def require_slot(value: Any, field: str = "slot") -> int:
    if not is_int(value) or value < 0:
        raise BasketError(INVALID_INPUT, f"{field} must be a non-negative integer", {"value": value})
    return value


def validate_coins(coins: Any, *, allow_empty: bool = False) -> Dict[int, int]:
    """Canonicalise a denomination -> quantity mapping.

    Accepts integer denominations directly and the string-keyed form produced
    by a JSON round trip (``{"5": 1}`` -> ``{5: 1}``).
    """

    if not isinstance(coins, Mapping):
        raise BasketError(INVALID_INPUT, "coins must be a denomination -> quantity mapping")
    canonical: Dict[int, int] = {}
    for denomination, qty in coins.items():
        if isinstance(denomination, str) and denomination.isdigit():
            denomination = int(denomination)
        if not is_int(denomination) or denomination not in DENOMINATIONS:
            raise BasketError(
                INVALID_DENOMINATION,
                "the teaching store only mints 5, 10 and 25 cent coins",
                {"denomination": denomination},
            )
        if not is_int(qty) or qty < 0:
            raise BasketError(INVALID_INPUT, "coin quantity must be a non-negative integer", {"denomination": denomination, "qty": qty})
        if qty:
            canonical[denomination] = qty
    if not allow_empty and not canonical:
        raise BasketError(INVALID_INPUT, "at least one coin is required")
    return {d: canonical[d] for d in DENOMINATIONS if d in canonical}


def coin_total(coins: Mapping[int, int]) -> int:
    return sum(d * q for d, q in coins.items())


def coins_from_json(value: str) -> Dict[int, int]:
    """JSON object keys are strings; coin maps use integer denominations."""

    raw = json_loads(value)
    return {int(d): q for d, q in raw.items() if q}


def coin_combine(amount: int, available: Mapping[int, int]) -> Optional[Dict[int, int]]:
    """Find an exact coin set for ``amount`` from ``available``.

    Deterministic: denominations are tried largest first, each with the
    largest feasible count first, so the first solution spends big coins and
    preserves small change (paying a 10 IOU with a 10 coin never eats the two
    5 coins a later 5 IOU needs).  Full backtracking guarantees that when no
    greedy cut works but an exact set exists, it is still found; ``None``
    means the amount genuinely cannot be made.
    """

    if amount <= 0:
        return {} if amount == 0 else None
    denoms = [d for d in sorted(DENOMINATIONS, reverse=True) if available.get(d, 0) > 0]

    def search(index: int, remaining: int, chosen: Dict[int, int]) -> Optional[Dict[int, int]]:
        if remaining == 0:
            return dict(chosen)
        if index == len(denoms):
            return None
        denomination = denoms[index]
        max_count = min(available[denomination], remaining // denomination)
        for count in range(max_count, -1, -1):
            if count:
                chosen[denomination] = count
            # A quick reachability prune on the smaller denominations.
            smaller_value = sum(d * available.get(d, 0) for d in denoms[index + 1:])
            if remaining - count * denomination <= smaller_value:
                found = search(index + 1, remaining - count * denomination, chosen)
                if found is not None:
                    return found
            if count:
                del chosen[denomination]
        return None

    return search(0, amount, {})


class CashOffice:
    """Counters, sealed bags, IOUs, coin inventory and the day planner."""

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------- counters
    def register_counter(self, counter_id: str) -> Dict[str, Any]:
        counter_id = require_id(counter_id, INVALID_INPUT, "counter_id")
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT counter_id FROM counters WHERE counter_id = ?", (counter_id,)).fetchone()
            if existing is not None:
                return {"ok": True, "counter_id": counter_id, "repeated": True}
            conn.execute("INSERT INTO counters (counter_id, created_at) VALUES (?, ?)", (counter_id, utc_now()))
            return {"ok": True, "counter_id": counter_id, "repeated": False}

    def list_counters(self) -> List[str]:
        with self.db.connection() as conn:
            return [row["counter_id"] for row in conn.execute("SELECT counter_id FROM counters ORDER BY counter_id")]

    def _require_counter(self, conn, counter_id: str) -> None:
        row = conn.execute("SELECT 1 FROM counters WHERE counter_id = ?", (counter_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_COUNTER, "unknown cash counter", {"counter_id": counter_id})

    # --------------------------------------------------------- opening stock
    def opening_balance(self, event_id: str, counter_id: str, denomination: Any, qty: Any) -> Dict[str, Any]:
        event_id = require_id(event_id, INVALID_INPUT, "event_id")
        counter_id = require_id(counter_id, INVALID_INPUT, "counter_id")
        if not is_int(denomination) or denomination not in DENOMINATIONS:
            raise BasketError(INVALID_DENOMINATION, "denomination must be one of 5, 10, 25", {"denomination": denomination})
        if not is_int(qty) or qty < 1:
            raise BasketError(INVALID_INPUT, "opening qty must be a positive integer", {"qty": qty})
        with self.db.transaction() as conn:
            self._require_counter(conn, counter_id)
            duplicate = conn.execute(
                "SELECT 1 FROM cash_office_events WHERE source_id = ?", (event_id,)
            ).fetchone()
            if duplicate is not None:
                row = conn.execute(
                    "SELECT denomination, delta FROM cash_office_events WHERE source_id = ? AND event_type = 'OPENING'",
                    (event_id,),
                ).fetchone()
                if row is None or row["denomination"] != denomination or row["delta"] != qty:
                    raise BasketError(PLAN_CONFLICT, "event_id reused with different content", {"event_id": event_id})
                return {"ok": True, "event_id": event_id, "repeated": True,
                        "balance": self._balance(conn, counter_id)}
            self._apply_event(conn, "OPENING", counter_id, denomination, qty, event_id)
            return {"ok": True, "event_id": event_id, "repeated": False,
                    "counter_id": counter_id, "denomination": denomination, "qty": qty,
                    "balance": self._balance(conn, counter_id)}

    # ----------------------------------------------------------------- bags
    def register_bag(
        self,
        bag_id: str,
        coins: Any,
        delivers_to: Any,
        arrival_slot: Any,
        freight: Any,
    ) -> Dict[str, Any]:
        bag_id = require_id(bag_id, INVALID_INPUT, "bag_id")
        canonical_coins = validate_coins(coins)
        if not isinstance(delivers_to, Sequence) or isinstance(delivers_to, (str, bytes)):
            raise BasketError(INVALID_INPUT, "delivers_to must be a list of counter ids")
        counters = sorted({require_id(c, INVALID_INPUT, "delivers_to[]") for c in delivers_to})
        if not counters:
            raise BasketError(INVALID_INPUT, "a bag must be deliverable to at least one counter")
        if len(counters) != len(list(delivers_to)):
            raise BasketError(INVALID_INPUT, "duplicate counter in delivers_to")
        require_slot(arrival_slot, "arrival_slot")
        if not is_int(freight) or freight < 0:
            raise BasketError(INVALID_INPUT, "freight must be a non-negative integer", {"freight": freight})
        with self.db.transaction() as conn:
            for counter_id in counters:
                self._require_counter(conn, counter_id)
            existing = conn.execute("SELECT * FROM cash_bags WHERE bag_id = ?", (bag_id,)).fetchone()
            if existing is not None:
                current = {
                    "coins": json_loads(existing["coins_json"]),
                    "delivers_to": json_loads(existing["delivers_json"]),
                    "arrival_slot": existing["arrival_slot"],
                    "freight": existing["freight"],
                }
                if current != {"coins": canonical_coins, "delivers_to": counters,
                               "arrival_slot": arrival_slot, "freight": freight}:
                    raise BasketError(BAG_CONFLICT, "bag_id already sealed with different content", {"bag_id": bag_id})
                return {"ok": True, "bag_id": bag_id, "repeated": True}
            conn.execute(
                "INSERT INTO cash_bags (bag_id, coins_json, delivers_json, arrival_slot, freight, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (bag_id, canonical_json(canonical_coins), canonical_json(counters), arrival_slot, freight, utc_now()),
            )
            return {"ok": True, "bag_id": bag_id, "repeated": False,
                    "coins": canonical_coins, "delivers_to": counters,
                    "arrival_slot": arrival_slot, "freight": freight}

    def list_bags(self) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute("SELECT * FROM cash_bags ORDER BY bag_id").fetchall()
        return [self._bag_row(row) for row in rows]

    def _bag_row(self, row) -> Dict[str, Any]:
        return {
            "bag_id": row["bag_id"],
            "coins": coins_from_json(row["coins_json"]),
            "delivers_to": json_loads(row["delivers_json"]),
            "arrival_slot": row["arrival_slot"],
            "freight": row["freight"],
        }

    # ------------------------------------------------------------- balances
    def _balance(self, conn, counter_id: Optional[str] = None) -> Dict[str, Dict[int, int]]:
        result: Dict[str, Dict[int, int]] = {}
        rows = conn.execute(
            "SELECT counter_id, denomination, qty FROM cash_office_balances ORDER BY counter_id, denomination"
        ).fetchall()
        for row in rows:
            if counter_id is not None and row["counter_id"] != counter_id:
                continue
            if row["qty"] <= 0:
                continue
            result.setdefault(row["counter_id"], {})[row["denomination"]] = row["qty"]
        return result

    def counter_balance(self, counter_id: Optional[str] = None) -> Dict[str, Dict[int, int]]:
        with self.db.connection() as conn:
            if counter_id is not None:
                self._require_counter(conn, counter_id)
            return self._balance(conn, counter_id)

    def _apply_event(self, conn, event_type: str, counter_id: str, denomination: int,
                     delta: int, source_id: str, iou_id: Optional[str] = None) -> None:
        conn.execute(
            "INSERT INTO cash_office_events (event_type, counter_id, denomination, delta, source_id, iou_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_type, counter_id, denomination, delta, source_id, iou_id, utc_now()),
        )
        conn.execute(
            """
            INSERT INTO cash_office_balances (counter_id, denomination, qty) VALUES (?, ?, ?)
            ON CONFLICT(counter_id, denomination) DO UPDATE SET qty = qty + excluded.qty
            """,
            (counter_id, denomination, delta),
        )

    # ------------------------------------------------------ cash receipt log
    def record_cash_refund(
        self,
        conn,
        *,
        request_id: str,
        sale_id: str,
        counter_id: str,
        slot: int,
        items: Sequence[Mapping[str, Any]],
        exact_refund: int,
        exact_cumulative: int,
    ) -> Dict[str, Any]:
        """Insert a receipt (and, when positive, the IOU) inside a ledger txn.

        ``conn`` is the ledger's open write transaction so the exact refund,
        the receipt and the IOU commit atomically.
        """

        receipt_id = f"CR-{request_id}"
        cash_cumulative = round5(exact_cumulative)
        cash_before = round5(exact_cumulative - exact_refund)
        cash_due = cash_cumulative - cash_before
        assert cash_due >= 0
        conn.execute(
            """
            INSERT INTO cash_receipts
                (receipt_id, request_id, sale_id, counter_id, slot, exact_refund,
                 exact_cumulative, cash_before, cash_cumulative, cash_due, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (receipt_id, request_id, sale_id, counter_id, slot, exact_refund,
             exact_cumulative, cash_before, cash_cumulative, cash_due, utc_now()),
        )
        for item in items:
            conn.execute(
                "INSERT INTO cash_receipt_items (receipt_id, unit_id, sku, original_paid, refund) VALUES (?, ?, ?, ?, ?)",
                (receipt_id, item["unit_id"], item["sku"], item["original_paid"], item["refund"]),
            )
        iou_id: Optional[str] = None
        if cash_due > 0:
            iou_id = f"IOU-{request_id}"
            try:
                conn.execute(
                    """
                    INSERT INTO cash_ious (iou_id, receipt_id, sale_id, counter_id, slot, amount, status)
                    VALUES (?, ?, ?, ?, ?, ?, 'PENDING')
                    """,
                    (iou_id, receipt_id, sale_id, counter_id, slot, cash_due),
                )
            except Exception as exc:  # sqlite3.IntegrityError
                if getattr(exc, "sqlite_errorname", "") == "SQLITE_CONSTRAINT_UNIQUE" or "UNIQUE" in str(exc):
                    raise BasketError(
                        IOU_SLOT_TAKEN,
                        "this counter already has a cash IOU for the slot",
                        {"counter_id": counter_id, "slot": slot},
                    ) from exc
                raise
        return {
            "receipt_id": receipt_id,
            "counter_id": counter_id,
            "slot": slot,
            "exact_refund": exact_refund,
            "exact_cumulative": exact_cumulative,
            "cash_before": cash_before,
            "cash_cumulative": cash_cumulative,
            "cash_due": cash_due,
            "iou_id": iou_id,
            "items": [dict(item) for item in items],
        }

    def get_receipt(self, request_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM cash_receipts WHERE request_id = ?", (request_id,)).fetchone()
            if row is None:
                raise BasketError(UNKNOWN_RECEIPT, "no cash receipt for this return request", {"request_id": request_id})
            return self._receipt_dict(conn, row)

    def list_receipts(self, sale_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            if sale_id is None:
                rows = conn.execute("SELECT * FROM cash_receipts ORDER BY seq").fetchall()
            else:
                rows = conn.execute("SELECT * FROM cash_receipts WHERE sale_id = ? ORDER BY seq", (sale_id,)).fetchall()
            return [self._receipt_dict(conn, row) for row in rows]

    def _receipt_dict(self, conn, row) -> Dict[str, Any]:
        items = [dict(r) for r in conn.execute(
            "SELECT unit_id, sku, original_paid, refund FROM cash_receipt_items WHERE receipt_id = ? ORDER BY unit_id",
            (row["receipt_id"],),
        ).fetchall()]
        iou = conn.execute("SELECT iou_id, status, amount FROM cash_ious WHERE receipt_id = ?", (row["receipt_id"],)).fetchone()
        return {
            "receipt_id": row["receipt_id"],
            "request_id": row["request_id"],
            "sale_id": row["sale_id"],
            "counter_id": row["counter_id"],
            "slot": row["slot"],
            "exact_refund": row["exact_refund"],
            "exact_cumulative": row["exact_cumulative"],
            "cash_before": row["cash_before"],
            "cash_cumulative": row["cash_cumulative"],
            "cash_due": row["cash_due"],
            "iou_id": iou["iou_id"] if iou else None,
            "iou_status": iou["status"] if iou else None,
            "items": items,
        }

    # ------------------------------------------------------------------ IOUs
    def list_ious(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status is not None and status not in ("PENDING", "PAID"):
            raise BasketError(INVALID_INPUT, "status must be PENDING or PAID", {"status": status})
        query = "SELECT * FROM cash_ious"
        params: Tuple[Any, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY slot, iou_id"
        with self.db.connection() as conn:
            return [self._iou_dict(row) for row in conn.execute(query, params).fetchall()]

    def get_iou(self, iou_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM cash_ious WHERE iou_id = ?", (iou_id,)).fetchone()
            if row is None:
                raise BasketError(UNKNOWN_IOU, "unknown cash IOU", {"iou_id": iou_id})
            return self._iou_dict(row)

    def _iou_dict(self, row) -> Dict[str, Any]:
        result = {
            "iou_id": row["iou_id"],
            "receipt_id": row["receipt_id"],
            "sale_id": row["sale_id"],
            "counter_id": row["counter_id"],
            "slot": row["slot"],
            "amount": row["amount"],
            "status": row["status"],
            "paid_at": row["paid_at"],
        }
        return result

    # ------------------------------------------------------------- planner
    def plan_day(self, *, node_budget: int = DEFAULT_NODE_BUDGET) -> Dict[str, Any]:
        """Exact minimum-freight plan for every currently pending IOU.

        The search is exhaustive over (bag subset, counter) assignments and
        coin payments; branch-and-bound keeps the minimum freight and, on
        equal freight, the lexicographically smallest deterministic plan.
        ``PLAN_TOO_LARGE`` is raised rather than returning an unverified plan
        if the node budget is exhausted.
        """

        with self.db.connection() as conn:
            ious = [dict(r) for r in conn.execute(
                "SELECT * FROM cash_ious WHERE status = 'PENDING' ORDER BY slot, iou_id"
            ).fetchall()]
            delivered_bag_ids = {
                row["bag_id"] for row in conn.execute("SELECT bag_id FROM bag_deliveries").fetchall()
            }
            bags = [self._bag_row(row) for row in conn.execute(
                "SELECT * FROM cash_bags ORDER BY bag_id"
            ).fetchall() if row["bag_id"] not in delivered_bag_ids]
            balances = self._balance(conn)

        balances = {c: dict(m) for c, m in balances.items()}
        for counter_id in {i["counter_id"] for i in ious}:
            balances.setdefault(counter_id, {})
        bags = [b for b in bags if b["arrival_slot"] <= max((i["slot"] for i in ious), default=-1)]

        if not ious:
            return self._plan_payload(feasible=True, deliveries=[], payments=[],
                                      balances=balances, total_freight=0, reason=None)

        # Even the union of every bag a counter could ever receive must cover
        # each IOU; otherwise the day is infeasible and we name the blocker.
        for iou in ious:
            reachable = dict(balances.get(iou["counter_id"], {}))
            for bag in bags:
                if iou["counter_id"] in bag["delivers_to"] and bag["arrival_slot"] <= iou["slot"]:
                    for d, q in bag["coins"].items():
                        reachable[d] = reachable.get(d, 0) + q
            if coin_combine(iou["amount"], reachable) is None:
                return self._plan_payload(
                    feasible=False, deliveries=[], payments=[], balances=balances,
                    total_freight=None,
                    reason=f"IOU {iou['iou_id']} at counter {iou['counter_id']} slot {iou['slot']} "
                           f"cannot be made even with every eligible bag",
                )

        eligible: Dict[str, List[Dict[str, Any]]] = {}
        for iou in ious:
            choices = [
                bag for bag in bags
                if iou["counter_id"] in bag["delivers_to"]
                and bag["arrival_slot"] <= iou["slot"]
            ]
            choices.sort(key=lambda b: (b["freight"], b["bag_id"]))
            eligible[iou["iou_id"]] = choices

        budget = {"nodes": 0}
        best: Dict[str, Any] = {"key": None, "deliveries": [], "payments": [], "balances": None}

        def delivery_subsets(choices: Sequence[Dict[str, Any]], delivered: frozenset) -> List[Tuple[Tuple[Dict[str, Any], ...], int]]:
            fresh = [b for b in choices if b["bag_id"] not in delivered]
            grouped: List[Tuple[Tuple[Dict[str, Any], ...], int]] = []
            for size in range(1, len(fresh) + 1):
                for subset in itertools.combinations(fresh, size):
                    grouped.append((subset, sum(b["freight"] for b in subset)))
            grouped.sort(key=lambda pair: (pair[1], tuple(b["bag_id"] for b in pair[0])))
            return grouped

        def dfs(index: int, balances_now: Dict[str, Dict[int, int]], delivered: frozenset,
                freight: int, deliveries_out: List[Dict[str, Any]],
                payments_out: List[Dict[str, Any]]) -> None:
            budget["nodes"] += 1
            if budget["nodes"] > node_budget:
                raise BasketError(PLAN_TOO_LARGE, "day planning exceeded the node budget",
                                  {"nodes": budget["nodes"], "budget": node_budget})
            if index == len(ious):
                key = self._plan_key(freight, deliveries_out, payments_out)
                if best["key"] is None or key < best["key"]:
                    best.update({
                        "key": key,
                        "deliveries": [dict(d) for d in deliveries_out],
                        "payments": [dict(p) for p in payments_out],
                        "balances": {c: dict(m) for c, m in balances_now.items()},
                    })
                return
            iou = ious[index]
            counter = iou["counter_id"]
            held = balances_now.setdefault(counter, {})

            # Option 0: pay from coins already at the counter (always cheapest).
            combo = coin_combine(iou["amount"], held)
            if combo is not None:
                self._spend(balances_now, counter, combo)
                payments_out.append(self._payment_record(iou, combo))
                dfs(index + 1, balances_now, delivered, freight, deliveries_out, payments_out)
                payments_out.pop()
                self._unspend(balances_now, counter, combo)

            # Options: receive one or more sealed bags, whole, then pay.
            for subset, subset_freight in delivery_subsets(eligible[iou["iou_id"]], delivered):
                new_freight = freight + subset_freight
                if best["key"] is not None and new_freight > best["key"][0]:
                    break  # subsets are freight ordered; nothing later can beat it
                added: List[Dict[str, int]] = []
                for bag in subset:
                    for d, q in bag["coins"].items():
                        held[d] = held.get(d, 0) + q
                    added.append(bag)
                    deliveries_out.append({
                        "bag_id": bag["bag_id"], "counter_id": counter,
                        "slot": bag["arrival_slot"], "freight": bag["freight"],
                        "coins": dict(bag["coins"]),
                    })
                combo = coin_combine(iou["amount"], held)
                if combo is not None:
                    self._spend(balances_now, counter, combo)
                    payments_out.append(self._payment_record(iou, combo))
                    new_delivered = delivered | {b["bag_id"] for b in subset}
                    dfs(index + 1, balances_now, new_delivered, new_freight, deliveries_out, payments_out)
                    payments_out.pop()
                    self._unspend(balances_now, counter, combo)
                for _ in added:
                    deliveries_out.pop()
                for bag in subset:
                    for d, q in bag["coins"].items():
                        held[d] -= q
                        if held[d] == 0:
                            del held[d]

        dfs(0, balances, frozenset(), 0, [], [])
        if best["key"] is None:  # pragma: no cover - reachability pre-check catches this
            return self._plan_payload(feasible=False, deliveries=[], payments=[], balances=balances,
                                      total_freight=None, reason="no assignment covers every IOU")
        return self._plan_payload(
            feasible=True,
            deliveries=best["deliveries"],
            payments=best["payments"],
            balances=best["balances"],
            total_freight=best["key"][0],
            reason=None,
        )

    @staticmethod
    def _spend(balances: Dict[str, Dict[int, int]], counter: str, coins: Mapping[int, int]) -> None:
        for d, q in coins.items():
            balances[counter][d] -= q
            if balances[counter][d] == 0:
                del balances[counter][d]

    @staticmethod
    def _unspend(balances: Dict[str, Dict[int, int]], counter: str, coins: Mapping[int, int]) -> None:
        for d, q in coins.items():
            balances[counter][d] = balances[counter].get(d, 0) + q

    @staticmethod
    def _payment_record(iou: Mapping[str, Any], coins: Mapping[int, int]) -> Dict[str, Any]:
        return {
            "iou_id": iou["iou_id"],
            "counter_id": iou["counter_id"],
            "slot": iou["slot"],
            "amount": iou["amount"],
            "coins": {d: coins[d] for d in DENOMINATIONS if d in coins},
        }

    @staticmethod
    def _plan_key(freight: int, deliveries: Sequence[Mapping[str, Any]],
                  payments: Sequence[Mapping[str, Any]]) -> Tuple[Any, ...]:
        delivery_key = tuple(sorted((d["bag_id"], d["counter_id"]) for d in deliveries))
        payment_key = tuple(
            tuple(p["coins"].get(d, 0) for d in DENOMINATIONS) for p in payments
        )
        return (freight, delivery_key, payment_key)

    def _plan_payload(self, *, feasible: bool, deliveries: List[Dict[str, Any]],
                      payments: List[Dict[str, Any]], balances: Dict[str, Dict[int, int]],
                      total_freight: Optional[int], reason: Optional[str]) -> Dict[str, Any]:
        return {
            "feasible": feasible,
            "total_freight": total_freight,
            "deliveries": sorted(deliveries, key=lambda d: d["bag_id"]),
            "payments": payments,
            "final_balances": {c: {d: m[d] for d in sorted(m)} for c, m in sorted(balances.items())},
            "unpaid_iou_ids": [] if feasible else None,
            "reason": reason,
        }

    # ------------------------------------------------------- plan persistence
    def save_plan(self, plan_id: str, plan: Mapping[str, Any]) -> Dict[str, Any]:
        plan_id = require_id(plan_id, INVALID_INPUT, "plan_id")
        if not isinstance(plan, Mapping) or "feasible" not in plan:
            raise BasketError(INVALID_INPUT, "plan must be a plan_day result object")
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT plan_json FROM day_plans WHERE plan_id = ?", (plan_id,)).fetchone()
            if existing is not None:
                if existing["plan_json"] != canonical_json(plan):
                    raise BasketError(PLAN_CONFLICT, "plan_id already stores a different plan", {"plan_id": plan_id})
                return {"ok": True, "plan_id": plan_id, "repeated": True}
            conn.execute(
                "INSERT INTO day_plans (plan_id, plan_json, created_at) VALUES (?, ?, ?)",
                (plan_id, canonical_json(plan), utc_now()),
            )
            return {"ok": True, "plan_id": plan_id, "repeated": False}

    def get_plan(self, plan_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT plan_json FROM day_plans WHERE plan_id = ?", (plan_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_PLAN, "unknown day plan", {"plan_id": plan_id})
        return json_loads(row["plan_json"])

    # ---------------------------------------------------------- plan execution
    def apply_plan(self, plan: Optional[Mapping[str, Any]] = None, *, plan_id: Optional[str] = None) -> Dict[str, Any]:
        """Execute a saved or supplied plan atomically; retries never re-issue.

        The plan is re-simulated against the current database before any coin
        moves, so a plan replayed after the office drifted is rejected rather
        than partially applied.
        """

        if plan is None:
            if plan_id is None:
                raise BasketError(INVALID_INPUT, "apply_plan needs a plan object or a stored plan_id")
            plan = self.get_plan(plan_id)
        if not isinstance(plan, Mapping) or not plan.get("feasible"):
            raise BasketError(PLAN_CONFLICT, "only feasible plans can be applied", {"reason": plan.get("reason") if isinstance(plan, Mapping) else None})

        deliveries = [
            {**d, "coins": validate_coins(d["coins"], allow_empty=False)}
            for d in plan["deliveries"]
        ]
        payments = [
            {**p, "coins": validate_coins(p["coins"], allow_empty=False)}
            for p in plan["payments"]
        ]
        with self.db.transaction() as conn:
            applied_deliveries = {
                row["bag_id"]: dict(row) for row in conn.execute("SELECT * FROM bag_deliveries").fetchall()
            }
            iou_rows = {row["iou_id"]: dict(row) for row in conn.execute("SELECT * FROM cash_ious").fetchall()}
            balances = {c: dict(m) for c, m in self._balance(conn).items()}

            # Revalidate every delivery against the sealed bag record.
            for delivery in deliveries:
                bag_row = conn.execute("SELECT * FROM cash_bags WHERE bag_id = ?", (delivery["bag_id"],)).fetchone()
                if bag_row is None:
                    raise BasketError(UNKNOWN_BAG, "plan references an unknown bag", {"bag_id": delivery["bag_id"]})
                bag = self._bag_row(bag_row)
                if delivery["counter_id"] not in bag["delivers_to"]:
                    raise BasketError(PLAN_CONFLICT, "bag cannot deliver to this counter",
                                      {"bag_id": bag["bag_id"], "counter_id": delivery["counter_id"]})
                if delivery["slot"] != bag["arrival_slot"] or delivery["freight"] != bag["freight"]:
                    raise BasketError(PLAN_CONFLICT, "plan delivery disagrees with the sealed bag",
                                      {"bag_id": bag["bag_id"]})
                if delivery["coins"] != bag["coins"]:
                    raise BasketError(PLAN_CONFLICT, "plan delivery opens a different coin set",
                                      {"bag_id": bag["bag_id"]})
                already = applied_deliveries.get(delivery["bag_id"])
                if already is not None and already["counter_id"] != delivery["counter_id"]:
                    raise BasketError(PLAN_CONFLICT, "bag already delivered to another counter",
                                      {"bag_id": delivery["bag_id"]})

            # Revalidate every payment against the live IOU and its amount.
            for payment in payments:
                iou = iou_rows.get(payment["iou_id"])
                if iou is None:
                    raise BasketError(UNKNOWN_IOU, "plan references an unknown IOU", {"iou_id": payment["iou_id"]})
                if iou["amount"] != payment["amount"] or iou["counter_id"] != payment["counter_id"]:
                    raise BasketError(IOU_CONFLICT, "plan payment disagrees with the stored IOU",
                                      {"iou_id": payment["iou_id"]})
                coins = payment["coins"]
                if coin_total(coins) != payment["amount"]:
                    raise BasketError(INCORRECT_TENDER, "plan coins do not sum to the IOU amount",
                                      {"iou_id": payment["iou_id"], "coins": coins})
                already = conn.execute(
                    "SELECT coins_json FROM disbursements WHERE iou_id = ?", (payment["iou_id"],)
                ).fetchone()
                if already is not None and coins_from_json(already["coins_json"]) != coins:
                    raise BasketError(IOU_CONFLICT, "IOU already paid with a different coin set",
                                      {"iou_id": payment["iou_id"]})

            simulation = {c: dict(m) for c, m in balances.items()}
            events = sorted(
                [("D", d) for d in deliveries] + [("P", p) for p in payments],
                key=lambda pair: (pair[1]["slot"], 0 if pair[0] == "D" else 1),
            )
            done_deliveries: List[str] = []
            done_disbursements: List[str] = []
            for kind, record in events:
                if kind == "D":
                    delivery_id = f"DLV-{record['bag_id']}"
                    if record["bag_id"] in applied_deliveries:
                        continue  # idempotent replay of an already applied plan
                    counter = record["counter_id"]
                    simulation.setdefault(counter, {})
                    for d, q in record["coins"].items():
                        simulation[counter][d] = simulation[counter].get(d, 0) + q
                    done_deliveries.append(delivery_id)
                else:
                    disbursement_id = f"DISB-{record['iou_id']}"
                    existing = conn.execute(
                        "SELECT 1 FROM disbursements WHERE disbursement_id = ?", (disbursement_id,)
                    ).fetchone()
                    if existing is not None:
                        # Idempotent replay: the matching coin set was already
                        # issued, so it must not move the simulated inventory again.
                        continue
                    iou = iou_rows[record["iou_id"]]
                    if iou["status"] == "PAID":
                        # A different disbursement already settled this IOU; never issue coins twice.
                        raise BasketError(IOU_ALREADY_PAID, "IOU already paid under a different disbursement",
                                          {"iou_id": record["iou_id"]})
                    counter = record["counter_id"]
                    held = simulation.setdefault(counter, {})
                    combo = record["coins"]
                    if coin_combine(record["amount"], held) is None or coin_total(combo) != record["amount"]:
                        raise BasketError(INSUFFICIENT_COINS, "plan is no longer payable from current inventory",
                                          {"iou_id": record["iou_id"]})
                    for d, q in combo.items():
                        if held.get(d, 0) < q:
                            raise BasketError(INSUFFICIENT_COINS, "plan is no longer payable from current inventory",
                                              {"iou_id": record["iou_id"], "denomination": d})
                    done_disbursements.append(disbursement_id)

            # Re-simulation succeeded: commit the deliveries and disbursements.
            for delivery in deliveries:
                delivery_id = f"DLV-{delivery['bag_id']}"
                if delivery["bag_id"] in applied_deliveries:
                    continue
                conn.execute(
                    "INSERT INTO bag_deliveries (delivery_id, bag_id, counter_id, slot, freight, coins_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (delivery_id, delivery["bag_id"], delivery["counter_id"], delivery["slot"],
                     delivery["freight"], canonical_json(delivery["coins"]), utc_now()),
                )
                for d, q in delivery["coins"].items():
                    self._apply_event(conn, "BAG_DELIVERY", delivery["counter_id"], d, q, delivery_id)

            for payment in payments:
                disbursement_id = f"DISB-{payment['iou_id']}"
                if conn.execute("SELECT 1 FROM disbursements WHERE disbursement_id = ?", (disbursement_id,)).fetchone():
                    continue
                combo = validate_coins(payment["coins"])
                self._disburse(conn, iou_rows[payment["iou_id"]], disbursement_id, combo)

            result = {
                "ok": True,
                "deliveries": [f"DLV-{d['bag_id']}" for d in deliveries],
                "disbursements": [f"DISB-{p['iou_id']}" for p in payments],
                "total_freight": plan["total_freight"],
                "balances": self._balance(conn),
                "repeated": not done_deliveries and not done_disbursements,
            }
            return result

    # --------------------------------------------------------- manual payout
    def pay_iou(self, iou_id: str, disbursement_id: str, coins: Optional[Any] = None) -> Dict[str, Any]:
        iou_id = require_id(iou_id, INVALID_INPUT, "iou_id")
        disbursement_id = require_id(disbursement_id, INVALID_INPUT, "disbursement_id")
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM cash_ious WHERE iou_id = ?", (iou_id,)).fetchone()
            if row is None:
                raise BasketError(UNKNOWN_IOU, "unknown cash IOU", {"iou_id": iou_id})
            iou = dict(row)
            duplicate = conn.execute(
                "SELECT * FROM disbursements WHERE disbursement_id = ?", (disbursement_id,)
            ).fetchone()
            if duplicate is not None:
                if duplicate["iou_id"] != iou_id:
                    raise BasketError(IOU_CONFLICT, "disbursement_id reused for a different IOU",
                                      {"disbursement_id": disbursement_id})
                return self._disbursement_result(conn, iou, repeated=True)
            if iou["status"] == "PAID":
                # The IOU is settled under its own disbursement.  A new
                # disbursement id must never issue coins a second time.
                raise BasketError(IOU_ALREADY_PAID, "IOU already paid; a retry must reuse the original disbursement id",
                                  {"iou_id": iou_id})

            held = self._balance(conn, iou["counter_id"]).get(iou["counter_id"], {})
            if coins is None:
                combo = coin_combine(iou["amount"], held)
                if combo is None:
                    raise BasketError(INSUFFICIENT_COINS, "counter cannot make the exact amount",
                                      {"iou_id": iou_id, "available": held})
            else:
                combo = validate_coins(coins)
                # Availability first: asking for coins the drawer does not hold
                # is an inventory failure regardless of the tender total.
                for d, q in combo.items():
                    if held.get(d, 0) < q:
                        raise BasketError(INSUFFICIENT_COINS, "counter lacks the tendered coins",
                                          {"iou_id": iou_id, "denomination": d, "needed": q, "available": held.get(d, 0)})
                if coin_total(combo) != iou["amount"]:
                    raise BasketError(INCORRECT_TENDER, "tender must equal the IOU amount exactly; customers take no change",
                                      {"iou_id": iou_id, "tender_total": coin_total(combo), "amount": iou["amount"]})
            self._disburse(conn, iou, disbursement_id, combo)
            return self._disbursement_result(conn, iou, repeated=False)

    def _disburse(self, conn, iou: Mapping[str, Any], disbursement_id: str, coins: Mapping[int, int]) -> None:
        for d, q in coins.items():
            cursor = conn.execute(
                "UPDATE cash_office_balances SET qty = qty - ? WHERE counter_id = ? AND denomination = ? AND qty >= ?",
                (q, iou["counter_id"], d, q),
            )
            if cursor.rowcount != 1:  # pragma: no cover - rechecked above
                raise BasketError(INSUFFICIENT_COINS, "coin inventory changed concurrently",
                                  {"iou_id": iou["iou_id"], "denomination": d})
            conn.execute(
                "INSERT INTO cash_office_events (event_type, counter_id, denomination, delta, source_id, iou_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("DISBURSEMENT", iou["counter_id"], d, -q, disbursement_id, iou["iou_id"], utc_now()),
            )
        try:
            conn.execute(
                "INSERT INTO disbursements (disbursement_id, iou_id, counter_id, amount, coins_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (disbursement_id, iou["iou_id"], iou["counter_id"], iou["amount"], canonical_json(coins), utc_now()),
            )
        except Exception as exc:  # sqlite3.IntegrityError on the unique ids
            if "UNIQUE" in str(exc) or getattr(exc, "sqlite_errorname", "") == "SQLITE_CONSTRAINT_UNIQUE":
                raise BasketError(IOU_ALREADY_PAID,
                                  "concurrent payout: the IOU or disbursement id is already settled",
                                  {"iou_id": iou["iou_id"], "disbursement_id": disbursement_id}) from exc
            raise
        cursor = conn.execute(
            "UPDATE cash_ious SET status = 'PAID', paid_at = ? WHERE iou_id = ? AND status = 'PENDING'",
            (utc_now(), iou["iou_id"]),
        )
        if cursor.rowcount != 1:  # pragma: no cover - unique insert above guards this
            raise BasketError(IOU_ALREADY_PAID, "IOU settled concurrently", {"iou_id": iou["iou_id"]})

    def _disbursement_result(self, conn, iou: Mapping[str, Any], *, repeated: bool, disbursement=None) -> Dict[str, Any]:
        if disbursement is None:
            disbursement = conn.execute(
                "SELECT * FROM disbursements WHERE iou_id = ? ORDER BY disbursement_id", (iou["iou_id"],)
            ).fetchone()
        if disbursement is None:  # pragma: no cover - defensive
            raise BasketError(UNKNOWN_DISBURSEMENT, "paid IOU has no disbursement record", {"iou_id": iou["iou_id"]})
        return {
            "ok": True,
            "repeated": repeated,
            "disbursement_id": disbursement["disbursement_id"],
            "iou_id": iou["iou_id"],
            "counter_id": iou["counter_id"],
            "amount": iou["amount"],
            "coins": coins_from_json(disbursement["coins_json"]),
            "balance": self._balance(conn, iou["counter_id"]).get(iou["counter_id"], {}),
        }

    # -------------------------------------------------------------- audits
    def audit_cash(self) -> Dict[str, Any]:
        """Recompute every coin balance and every cash invariant from raw rows."""

        with self.db.connection() as conn:
            stored = self._balance(conn)
            recomputed: Dict[str, Dict[int, int]] = {}
            for row in conn.execute(
                "SELECT counter_id, denomination, SUM(delta) AS qty FROM cash_office_events GROUP BY counter_id, denomination"
            ).fetchall():
                if row["qty"] <= 0:
                    continue
                recomputed.setdefault(row["counter_id"], {})[row["denomination"]] = row["qty"]
            counters = [r["counter_id"] for r in conn.execute("SELECT counter_id FROM counters ORDER BY counter_id")]
            balance_ok = all(stored.get(c, {}) == recomputed.get(c, {}) for c in counters)

            receipts = [self._receipt_dict(conn, row)
                        for row in conn.execute("SELECT * FROM cash_receipts ORDER BY seq").fetchall()]
            invariant_lines = []
            invariant_ok = True
            for sale_id in {r["sale_id"] for r in receipts}:
                chain = [r for r in receipts if r["sale_id"] == sale_id]
                exact_sum = 0
                cash_sum = 0
                prior_cum = 0
                for receipt in chain:
                    exact_sum = receipt["exact_cumulative"]
                    expected_cum = round5(exact_sum)
                    expected_due = expected_cum - prior_cum
                    ok = (
                        receipt["cash_cumulative"] == expected_cum
                        and receipt["cash_before"] == prior_cum
                        and receipt["cash_due"] == expected_due
                        and receipt["exact_refund"] == sum(i["refund"] for i in receipt["items"])
                        and expected_due % 5 == 0
                    )
                    invariant_ok &= ok
                    invariant_lines.append({
                        "receipt_id": receipt["receipt_id"],
                        "sale_id": sale_id,
                        "stored": {"cash_before": receipt["cash_before"], "cash_cumulative": receipt["cash_cumulative"],
                                   "cash_due": receipt["cash_due"], "exact_cumulative": receipt["exact_cumulative"]},
                        "recomputed": {"cash_cumulative": expected_cum, "cash_due": expected_due},
                        "ok": ok,
                    })
                    prior_cum = expected_cum
                    cash_sum += receipt["cash_due"]

            ious = [self._iou_dict(row) for row in conn.execute("SELECT * FROM cash_ious ORDER BY slot, iou_id")]
            disbursements = [dict(row) for row in conn.execute("SELECT * FROM disbursements ORDER BY disbursement_id")]
            for disbursement in disbursements:
                disbursement["coins"] = coins_from_json(disbursement["coins_json"])
            payout_ok = all(coin_total(d["coins"]) == d["amount"] for d in disbursements)
            iou_totals = sum(i["amount"] for i in ious)
            disbursed_totals = sum(d["amount"] for d in disbursements)
            freight = conn.execute("SELECT COALESCE(SUM(freight), 0) AS f FROM bag_deliveries").fetchone()["f"]
            slot_ok = len(ious) == len({(i["counter_id"], i["slot"]) for i in ious})
            positive_only = all(r["cash_due"] > 0 for r in receipts if r["iou_id"]) and all(
                r["iou_id"] is None for r in receipts if r["cash_due"] == 0
            )
            return {
                "ok": balance_ok and invariant_ok and payout_ok and slot_ok and positive_only
                and iou_totals == disbursed_totals + sum(i["amount"] for i in ious if i["status"] == "PENDING"),
                "counters": counters,
                "balances": {"stored": stored, "recomputed_from_events": recomputed},
                "receipt_invariants": invariant_lines,
                "iou_total": iou_totals,
                "disbursed_total": disbursed_totals,
                "pending_total": sum(i["amount"] for i in ious if i["status"] == "PENDING"),
                "delivery_freight_total": freight,
                "slot_uniqueness_ok": slot_ok,
                "positive_due_has_iou": positive_only,
                "receipts": receipts,
                "ious": ious,
                "disbursements": disbursements,
            }

    def close_day(self) -> Dict[str, Any]:
        """Independent re-open of the cash books to the cent, per receipt."""

        audit = self.audit_cash()
        with self.db.connection() as conn:
            receipt_lines = []
            for row in conn.execute("SELECT * FROM cash_receipts ORDER BY seq").fetchall():
                receipt = self._receipt_dict(conn, row)
                exact_from_items = conn.execute(
                    "SELECT COALESCE(SUM(refund), 0) AS s FROM cash_receipt_items WHERE receipt_id = ?",
                    (row["receipt_id"],),
                ).fetchone()["s"]
                receipt_lines.append({
                    "receipt_id": receipt["receipt_id"],
                    "request_id": receipt["request_id"],
                    "sale_id": receipt["sale_id"],
                    "counter_id": receipt["counter_id"],
                    "slot": receipt["slot"],
                    "per_unit_exact": receipt["items"],
                    "exact_refund": receipt["exact_refund"],
                    "exact_refund_recomputed": exact_from_items,
                    "exact_cumulative": receipt["exact_cumulative"],
                    "cash_before": receipt["cash_before"],
                    "cash_cumulative": receipt["cash_cumulative"],
                    "cash_due": receipt["cash_due"],
                    "iou_id": receipt["iou_id"],
                    "iou_status": receipt["iou_status"],
                })
        totals = {
            "exact_refund_total": sum(line["exact_refund"] for line in receipt_lines),
            "cash_due_total": sum(line["cash_due"] for line in receipt_lines),
            "receipt_count": len(receipt_lines),
            "iou_count": len([line for line in receipt_lines if line["iou_id"]]),
            "zero_due_count": len([line for line in receipt_lines if line["cash_due"] == 0]),
        }
        return {"ok": audit["ok"], "receipts": receipt_lines, "totals": totals, "audit": audit}


__all__ = ["CashOffice", "DENOMINATIONS", "round5", "coin_combine", "coin_total", "validate_coins"]
