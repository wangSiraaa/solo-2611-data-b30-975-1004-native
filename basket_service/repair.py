"""Repair shop scheduling against warranty cover and technician shifts.

The shop schedules confirmed work orders with a small, exact optimiser rather
than a greedy first-fit rule:

* only the *start day* must remain inside the unit's warranty cover (work may
  finish after expiry) and may not precede the delivery day;
* an accepted order occupies one *qualified* technician for the method's whole
  consecutive duration, and one technician takes at most one unit per day;
* each order may use any of several factory-approved methods, each with its own
  duration and required technician specialty;
* when capacity is oversubscribed the shop maximises the total repair value of
  the orders it accepts; deterministic tie-breaking then fixes the exact days,
  methods and technicians so re-runs are byte-stable.

Historical schedules are pinned to the warranty version that was current when
the order was raised; later back-dated outages never silently move an old
booking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .db import Database, canonical_json, deep_copy, json_loads, utc_now
from .errors import (
    BOOKING_CONFLICT,
    INVALID_HORIZON,
    INVALID_INPUT,
    INVALID_SPECIALTY,
    UNKNOWN_BOOKING,
    UNKNOWN_METHOD,
    UNKNOWN_TECHNICIAN,
    UNKNOWN_WARRANTY,
    UNKNOWN_WORK_ORDER,
    WORK_ORDER_CONFLICT,
    BasketError,
)
from .validation import is_int, require_id

# Rejection reasons emitted for orders the shop cannot take.
REJECT_NOT_COVERED = "NO_COVERED_START_DAY"
REJECT_NO_QUALIFIED_TECH = "NO_QUALIFIED_TECHNICIAN"
REJECT_NO_SLOT_IN_HORIZON = "NO_SLOT_WITHIN_HORIZON"
REJECT_PREEMPTED = "PREEMPTED_BY_HIGHER_VALUE"


def _require_day(value: Any, field: str) -> int:
    if not is_int(value) or value < 0:
        raise BasketError(INVALID_INPUT, f"{field} must be a non-negative integer business day", {"field": field, "value": value})
    return value


@dataclass(frozen=True)
class Candidate:
    """One feasible (order, method, technician, start-day) placement."""

    order_index: int
    method_id: str
    technician_id: str
    start_day: int
    end_day: int
    days: Tuple[int, ...]
    value: int


class RepairShop:
    def __init__(self, db: Database):
        self.db = db

    # ----------------------------------------------------------- catalogue -
    def register_method(self, method_id: str, sku: str, duration_days: Any, specialty: str) -> Dict[str, Any]:
        method_id = require_id(method_id, INVALID_INPUT, "method_id")
        sku = require_id(sku, INVALID_INPUT, "sku")
        specialty = require_id(specialty, INVALID_SPECIALTY, "specialty")
        if not is_int(duration_days) or duration_days < 1:
            raise BasketError(INVALID_INPUT, "duration_days must be a positive integer", {"duration_days": duration_days})
        content = {
            "method_id": method_id,
            "sku": sku,
            "duration_days": duration_days,
            "specialty": specialty,
        }
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT content_json FROM repair_methods WHERE method_id = ?", (method_id,)).fetchone()
            if existing is not None:
                if existing["content_json"] != canonical_json(content):
                    raise BasketError(WORK_ORDER_CONFLICT, "method_id already registered differently", {"method_id": method_id})
                return deep_copy(content)
            conn.execute(
                "INSERT INTO repair_methods (method_id, sku, duration_days, specialty, content_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (method_id, sku, duration_days, specialty, canonical_json(content), utc_now()),
            )
        return deep_copy(content)

    def register_technician(
        self,
        technician_id: str,
        specialty: str,
        *,
        unavailable: Optional[Sequence[Tuple[int, int]]] = None,
    ) -> Dict[str, Any]:
        technician_id = require_id(technician_id, INVALID_INPUT, "technician_id")
        specialty = require_id(specialty, INVALID_SPECIALTY, "specialty")
        intervals: List[Tuple[int, int]] = []
        for index, interval in enumerate(unavailable or []):
            if not isinstance(interval, (list, tuple)) or len(interval) != 2:
                raise BasketError(INVALID_INPUT, "unavailable entries must be [start_day, end_day)", {"index": index})
            start, end = interval
            if not is_int(start) or not is_int(end) or start < 0 or end <= start:
                raise BasketError(INVALID_INPUT, "unavailable interval must be half-open non-negative days", {"index": index})
            intervals.append((start, end))
        content = {"technician_id": technician_id, "specialty": specialty, "unavailable": sorted(intervals)}
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT content_json FROM technicians WHERE technician_id = ?", (technician_id,)).fetchone()
            if existing is not None:
                if existing["content_json"] != canonical_json(content):
                    raise BasketError(
                        BOOKING_CONFLICT,
                        "technician_id already registered differently",
                        {"technician_id": technician_id},
                    )
                return deep_copy(content)
            conn.execute(
                "INSERT INTO technicians (technician_id, specialty, content_json, created_at) VALUES (?, ?, ?, ?)",
                (technician_id, specialty, canonical_json(content), utc_now()),
            )
            for start, end in intervals:
                conn.execute(
                    "INSERT INTO technician_unavailable (technician_id, start_day, end_day, created_at) VALUES (?, ?, ?, ?)",
                    (technician_id, start, end, utc_now()),
                )
        return deep_copy(content)

    def _technician(self, conn, technician_id: str) -> Dict[str, Any]:
        row = conn.execute("SELECT content_json FROM technicians WHERE technician_id = ?", (technician_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_TECHNICIAN, "unknown technician", {"technician_id": technician_id})
        return json_loads(row["content_json"])

    def _method(self, conn, method_id: str) -> Dict[str, Any]:
        row = conn.execute("SELECT content_json FROM repair_methods WHERE method_id = ?", (method_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_METHOD, "unknown repair method", {"method_id": method_id})
        return json_loads(row["content_json"])

    # --------------------------------------------------------- work orders --
    def open_work_order(
        self,
        work_order_id: str,
        sale_id: str,
        unit_id: str,
        delivery_day: Any,
        repair_value: Any,
        method_ids: Any,
    ) -> Dict[str, Any]:
        work_order_id = require_id(work_order_id, INVALID_INPUT, "work_order_id")
        sale_id = require_id(sale_id, INVALID_INPUT, "sale_id")
        unit_id = require_id(unit_id, INVALID_INPUT, "unit_id")
        delivery_day = _require_day(delivery_day, "delivery_day")
        if not is_int(repair_value) or repair_value < 0:
            raise BasketError(INVALID_INPUT, "repair_value must be a non-negative integer", {"repair_value": repair_value})
        if not isinstance(method_ids, Sequence) or isinstance(method_ids, (str, bytes)) or not method_ids:
            raise BasketError(INVALID_INPUT, "method_ids must be a non-empty list")
        method_list = sorted({require_id(m, INVALID_INPUT, "method_id") for m in method_ids})
        with self.db.transaction() as conn:
            warranty = conn.execute(
                "SELECT sku, current_version FROM warranties WHERE sale_id = ? AND unit_id = ?",
                (sale_id, unit_id),
            ).fetchone()
            if warranty is None:
                raise BasketError(
                    UNKNOWN_WARRANTY,
                    "work order requires an enrolled warranty",
                    {"sale_id": sale_id, "unit_id": unit_id},
                )
            for method_id in method_list:
                method = self._method(conn, method_id)
                if method["sku"] != warranty["sku"]:
                    raise BasketError(
                        UNKNOWN_METHOD,
                        "method is not approved for the unit's sku",
                        {"method_id": method_id, "sku": warranty["sku"]},
                    )
            existing = conn.execute("SELECT * FROM work_orders WHERE work_order_id = ?", (work_order_id,)).fetchone()
            if existing is not None:
                if existing["sale_id"] != sale_id or existing["unit_id"] != unit_id:
                    raise BasketError(WORK_ORDER_CONFLICT, "work_order_id reused for another unit", {"work_order_id": work_order_id})
                return self._work_order(conn, work_order_id)
            duplicate = conn.execute(
                "SELECT work_order_id FROM work_orders WHERE sale_id = ? AND unit_id = ? AND status = 'OPEN'",
                (sale_id, unit_id),
            ).fetchone()
            if duplicate is not None:
                raise BasketError(
                    WORK_ORDER_CONFLICT,
                    "an open work order already exists for this unit",
                    {"work_order_id": duplicate["work_order_id"]},
                )
            conn.execute(
                """
                INSERT INTO work_orders
                    (work_order_id, sale_id, unit_id, sku, delivery_day, repair_value, method_ids_json,
                     warranty_version, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
                """,
                (
                    work_order_id,
                    sale_id,
                    unit_id,
                    warranty["sku"],
                    delivery_day,
                    repair_value,
                    canonical_json(method_list),
                    warranty["current_version"],
                    utc_now(),
                ),
            )
            return self._work_order(conn, work_order_id)

    def get_work_order(self, work_order_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT 1 FROM work_orders WHERE work_order_id = ?", (work_order_id,)).fetchone()
            if row is None:
                raise BasketError(UNKNOWN_WORK_ORDER, "unknown work order", {"work_order_id": work_order_id})
            return self._work_order(conn, work_order_id)

    def _work_order(self, conn, work_order_id: str) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM work_orders WHERE work_order_id = ?", (work_order_id,)).fetchone()
        return {
            "work_order_id": row["work_order_id"],
            "sale_id": row["sale_id"],
            "unit_id": row["unit_id"],
            "sku": row["sku"],
            "delivery_day": row["delivery_day"],
            "repair_value": row["repair_value"],
            "method_ids": json_loads(row["method_ids_json"]),
            "warranty_version": row["warranty_version"],
            "status": row["status"],
        }

    # ------------------------------------------------------------ planning --
    def plan_batch(self, batch_id: str, work_order_ids: Sequence[str], horizon_end_day: Any) -> Dict[str, Any]:
        """Optimise one batch; persist decisions and technician reservations.

        Re-running the same ``batch_id`` replays the stored result.  Every
        order in the batch must still be open (a unit booked in an earlier
        batch is rejected up front rather than double-booked).
        """

        batch_id = require_id(batch_id, INVALID_INPUT, "batch_id")
        if not isinstance(work_order_ids, Sequence) or isinstance(work_order_ids, (str, bytes)) or not work_order_ids:
            raise BasketError(INVALID_INPUT, "work_order_ids must be a non-empty list")
        horizon_end_day = _require_day(horizon_end_day, "horizon_end_day")
        if horizon_end_day < 1:
            raise BasketError(
                INVALID_HORIZON,
                "horizon_end_day must be at least 1 so a start day can exist inside it",
                {"horizon_end_day": horizon_end_day},
            )
        ordered_ids = sorted({require_id(w, INVALID_INPUT, "work_order_id") for w in work_order_ids})
        with self.db.transaction() as conn:
            stored = conn.execute("SELECT result_json FROM booking_batches WHERE batch_id = ?", (batch_id,)).fetchone()
            if stored is not None:
                return json_loads(stored["result_json"])
            orders = [self._load_order_for_planning(conn, wid) for wid in ordered_ids]
            technicians = {
                r["technician_id"]: json_loads(r["content_json"])
                for r in conn.execute("SELECT technician_id, content_json FROM technicians").fetchall()
            }
            methods = {
                r["method_id"]: json_loads(r["content_json"])
                for r in conn.execute("SELECT method_id, content_json FROM repair_methods").fetchall()
            }
            existing_bookings = self._existing_reservations(conn)
            result = self._optimise(orders, technicians, methods, existing_bookings, horizon_end_day)
            self._persist_batch(conn, batch_id, horizon_end_day, result)
            return result

    def get_batch(self, batch_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT result_json FROM booking_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_BOOKING, "unknown booking batch", {"batch_id": batch_id})
        return json_loads(row["result_json"])

    def get_decision(self, work_order_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT result_json FROM booking_decisions WHERE work_order_id = ? ORDER BY rowid DESC LIMIT 1",
                (work_order_id,),
            ).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_BOOKING, "work order has never been scheduled", {"work_order_id": work_order_id})
        return json_loads(row["result_json"])

    def technician_calendar(self, technician_id: str) -> List[Dict[str, int]]:
        with self.db.connection() as conn:
            self._technician(conn, technician_id)
            rows = conn.execute(
                "SELECT work_order_id, day FROM technician_bookings WHERE technician_id = ? ORDER BY day",
                (technician_id,),
            ).fetchall()
        return [{"work_order_id": r["work_order_id"], "day": r["day"]} for r in rows]

    # ------------------------------------------------------------- engine ---
    def _load_order_for_planning(self, conn, work_order_id: str) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM work_orders WHERE work_order_id = ?", (work_order_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_WORK_ORDER, "unknown work order", {"work_order_id": work_order_id})
        if row["status"] != "OPEN":
            raise BasketError(
                WORK_ORDER_CONFLICT,
                "work order is not open (already booked)",
                {"work_order_id": work_order_id, "status": row["status"]},
            )
        version_row = conn.execute(
            "SELECT * FROM warranty_versions WHERE sale_id = ? AND unit_id = ? AND version = ?",
            (row["sale_id"], row["unit_id"], row["warranty_version"]),
        ).fetchone()
        if version_row is None:  # pragma: no cover - foreign-key style guard
            raise BasketError(UNKNOWN_WARRANTY, "warranty version pinned by the work order is missing", {"work_order_id": work_order_id})
        return {
            "work_order_id": row["work_order_id"],
            "sale_id": row["sale_id"],
            "unit_id": row["unit_id"],
            "sku": row["sku"],
            "delivery_day": row["delivery_day"],
            "repair_value": row["repair_value"],
            "method_ids": json_loads(row["method_ids_json"]),
            "warranty_version": row["warranty_version"],
            "latest_start_day": version_row["latest_start_day"],
            "end_reason": version_row["end_reason"],
        }

    def _existing_reservations(self, conn) -> Set[Tuple[str, int]]:
        return {
            (r["technician_id"], r["day"])
            for r in conn.execute("SELECT technician_id, day FROM technician_bookings").fetchall()
        }

    def _generate_candidates(
        self,
        orders: Sequence[Mapping[str, Any]],
        technicians: Mapping[str, Mapping[str, Any]],
        methods: Mapping[str, Mapping[str, Any]],
        occupied: Set[Tuple[str, int]],
        horizon_end_day: int,
    ) -> Tuple[List[List[Candidate]], List[Dict[str, str]]]:
        """All feasible placements per order, plus deterministic reject reasons."""

        candidates_per_order: List[List[Candidate]] = []
        rejects: List[Dict[str, str]] = []
        for index, order in enumerate(orders):
            latest_start = order["latest_start_day"]
            earliest = order["delivery_day"]
            if latest_start is None or latest_start < earliest:
                rejects.append(
                    {
                        "work_order_id": order["work_order_id"],
                        "accepted": False,
                        "reason_code": REJECT_NOT_COVERED,
                        "reason_detail": (
                            "no covered day on/after delivery"
                            if latest_start is None
                            else f"warranty latest_start_day={latest_start} precedes delivery_day={earliest}"
                        ),
                        "warranty_version": order["warranty_version"],
                    }
                )
                candidates_per_order.append([])
                continue
            candidates: List[Candidate] = []
            qualified_tech_exists = False
            for method_id in order["method_ids"]:
                method = methods[method_id]
                duration = method["duration_days"]
                qualified = [
                    tid
                    for tid, tech in sorted(technicians.items())
                    if tech["specialty"] == method["specialty"]
                ]
                if qualified:
                    qualified_tech_exists = True
                # Work may finish beyond the horizon; only the start must be
                # within both the horizon and warranty cover.
                last_start = min(latest_start, horizon_end_day - 1)
                for technician_id in qualified:
                    unavailable = technicians[technician_id]["unavailable"]
                    for start_day in range(earliest, last_start + 1):
                        days = tuple(range(start_day, start_day + duration))
                        if any(s <= d < e for s, e in unavailable for d in days):
                            continue
                        if any((technician_id, d) in occupied for d in days):
                            continue
                        candidates.append(
                            Candidate(
                                order_index=index,
                                method_id=method_id,
                                technician_id=technician_id,
                                start_day=start_day,
                                end_day=start_day + duration,
                                days=days,
                                value=order["repair_value"],
                            )
                        )
            if not candidates:
                if not qualified_tech_exists:
                    rejects.append(
                        {
                            "work_order_id": order["work_order_id"],
                            "accepted": False,
                            "reason_code": REJECT_NO_QUALIFIED_TECH,
                            "reason_detail": "no technician matches any approved method specialty",
                            "warranty_version": order["warranty_version"],
                        }
                    )
                else:
                    rejects.append(
                        {
                            "work_order_id": order["work_order_id"],
                            "accepted": False,
                            "reason_code": REJECT_NO_SLOT_IN_HORIZON,
                            "reason_detail": (
                                f"qualified technicians have no free consecutive slot in "
                                f"[{earliest}, {min(latest_start, horizon_end_day - 1) + 1})"
                            ),
                            "warranty_version": order["warranty_version"],
                        }
                    )
                candidates_per_order.append([])
            else:
                # Deterministic ordering for the optimiser's tie-breaking.
                candidates.sort(
                    key=lambda c: (c.start_day, c.method_id, c.technician_id, c.end_day)
                )
                candidates_per_order.append(candidates)
        return candidates_per_order, rejects

    def _optimise(
        self,
        orders: Sequence[Mapping[str, Any]],
        technicians: Mapping[str, Mapping[str, Any]],
        methods: Mapping[str, Mapping[str, Any]],
        occupied: Set[Tuple[str, int]],
        horizon_end_day: int,
    ) -> Dict[str, Any]:
        candidates_per_order, hard_rejects = self._generate_candidates(
            orders, technicians, methods, occupied, horizon_end_day
        )
        schedulable = [i for i, cands in enumerate(candidates_per_order) if cands]
        values = [orders[i]["repair_value"] for i in schedulable]

        # Flatten every candidate; conflicts live at flat-candidate level:
        # two candidates conflict when they belong to the same work order (at
        # most one placement per order) or share a technician on any day.
        flat: List[Tuple[int, Candidate]] = []
        for local_index, order_index in enumerate(schedulable):
            for candidate in candidates_per_order[order_index]:
                flat.append((local_index, candidate))
        flat_conflicts: List[Set[int]] = [set() for _ in flat]
        flat_index_to_local = [local for local, _ in flat]
        for a in range(len(flat)):
            local_a, cand_a = flat[a]
            slots_a = {(cand_a.technician_id, d) for d in cand_a.days}
            for b in range(a + 1, len(flat)):
                local_b, cand_b = flat[b]
                slots_b = {(cand_b.technician_id, d) for d in cand_b.days}
                if local_a == local_b or slots_a & slots_b:
                    flat_conflicts[a].add(b)
                    flat_conflicts[b].add(a)

        candidates_of_local = {
            local: [idx for idx, owner in enumerate(flat_index_to_local) if owner == local]
            for local in range(len(schedulable))
        }

        best_value = -1
        best_signature: Optional[Tuple[Any, ...]] = None
        best_choice: Dict[int, int] = {}

        def upper_bound(chosen_value: int, remaining_locals: Set[int]) -> int:
            return chosen_value + sum(values[local] for local in remaining_locals)

        def signature_of(choice: Mapping[int, int]) -> Tuple[Any, ...]:
            # Lexicographic, fully deterministic tie-break across equal totals:
            # per work-order id compare start day, then method, then technician.
            parts: List[Tuple[str, int, str, str]] = []
            for local, flat_idx in choice.items():
                candidate = flat[flat_idx][1]
                parts.append(
                    (
                        orders[schedulable[local]]["work_order_id"],
                        candidate.start_day,
                        candidate.method_id,
                        candidate.technician_id,
                    )
                )
            parts.sort()
            return tuple(parts)

        def dfs(
            chosen_value: int,
            blocked: Set[int],
            remaining: Set[int],
            choice: Dict[int, int],
        ) -> None:
            nonlocal best_value, best_signature, best_choice
            if upper_bound(chosen_value, remaining) < best_value:
                return
            if not remaining:
                sig = signature_of(choice)
                if chosen_value > best_value or (
                    chosen_value == best_value and (best_signature is None or sig < best_signature)
                ):
                    best_value = chosen_value
                    best_signature = sig
                    best_choice = dict(choice)
                return
            # MRV: branch on the order with fewest still-feasible placements.
            target = min(
                remaining,
                key=lambda local: (
                    sum(1 for idx in candidates_of_local[local] if idx not in blocked),
                    orders[schedulable[local]]["work_order_id"],
                ),
            )
            available = [idx for idx in candidates_of_local[target] if idx not in blocked]
            # Explore earliest placements first to lock in a strong incumbent;
            # skipping the order (capacity pre-emption) is explored last.
            ordered = sorted(
                available,
                key=lambda idx: (flat[idx][1].start_day, flat[idx][1].method_id, flat[idx][1].technician_id),
            )
            for flat_idx in ordered:
                dfs(
                    chosen_value + values[target],
                    blocked | flat_conflicts[flat_idx],
                    remaining - {target},
                    {**choice, target: flat_idx},
                )
            dfs(chosen_value, blocked, remaining - {target}, choice)

        dfs(0, set(), set(range(len(schedulable))), {})

        decisions: List[Dict[str, Any]] = []
        for local, order_index in enumerate(schedulable):
            order = orders[order_index]
            if local not in best_choice:
                decisions.append(
                    {
                        "work_order_id": order["work_order_id"],
                        "accepted": False,
                        "reason_code": REJECT_PREEMPTED,
                        "reason_detail": "feasible alone but displaced to maximise total repair value under capacity",
                        "warranty_version": order["warranty_version"],
                    }
                )
                continue
            candidate = flat[best_choice[local]][1]
            decisions.append(
                {
                    "work_order_id": order["work_order_id"],
                    "accepted": True,
                    "start_day": candidate.start_day,
                    "end_day": candidate.end_day,
                    "days": list(candidate.days),
                    "method_id": candidate.method_id,
                    "technician_id": candidate.technician_id,
                    "repair_value": candidate.value,
                    "warranty_version": order["warranty_version"],
                }
            )
        decisions.extend(hard_rejects)
        decisions.sort(key=lambda d: d["work_order_id"])
        return {
            "ok": True,
            "horizon_end_day": horizon_end_day,
            "total_value": best_value if best_value >= 0 else 0,
            "accepted": sum(1 for d in decisions if d.get("accepted")),
            "rejected": sum(1 for d in decisions if not d.get("accepted")),
            "decisions": decisions,
        }

    def _persist_batch(self, conn, batch_id: str, horizon_end_day: int, result: Mapping[str, Any]) -> None:
        conn.execute(
            "INSERT INTO booking_batches (batch_id, horizon_end_day, total_value, result_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (batch_id, horizon_end_day, result["total_value"], canonical_json(result), utc_now()),
        )
        for decision in result["decisions"]:
            conn.execute(
                """
                INSERT INTO booking_decisions
                    (batch_id, work_order_id, accepted, start_day, end_day, method_id, technician_id,
                     reason_code, reason_detail, warranty_version, result_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    decision["work_order_id"],
                    1 if decision["accepted"] else 0,
                    decision.get("start_day"),
                    decision.get("end_day"),
                    decision.get("method_id"),
                    decision.get("technician_id"),
                    None if decision["accepted"] else decision["reason_code"],
                    None if decision["accepted"] else decision["reason_detail"],
                    decision["warranty_version"],
                    canonical_json(decision),
                ),
            )
            if decision["accepted"]:
                conn.execute(
                    "UPDATE work_orders SET status = 'BOOKED' WHERE work_order_id = ?",
                    (decision["work_order_id"],),
                )
                for day in decision["days"]:
                    conn.execute(
                        "INSERT INTO technician_bookings (booking_id, batch_id, work_order_id, technician_id, day, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            f"{batch_id}:{decision['work_order_id']}:{day}",
                            batch_id,
                            decision["work_order_id"],
                            decision["technician_id"],
                            day,
                            utc_now(),
                        ),
                    )
