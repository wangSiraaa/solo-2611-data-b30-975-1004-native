"""Container deposits, two-counter physical clearance and shipment planning.

A physical bottle carries one immutable supply credential: supplier, package
model, integer deposit and the latest day the empty may be returned.  A refund
voucher is *not* a portion count: it is the container itself.  Portions that
expired without being led are still physically inside the bottle, so they must
be written off as ``cleared`` by two different counters before the container
can be reclaimed.  ``cleared`` is its own conservation destination and is never
disguised as ``consumed``.

Eligibility for a voucher ("用尽且无活跃预留"):

* the bottle has no ``active`` reservations, and
* every portion is physically accounted for: ``unallocated == 0`` after
  consumption and two-counter clearance, and
* the day is on/before the credential's ``latest_return_day``.

Shipping is a small exact optimiser.  A trip has departure/arrival days and a
maximum number of boxes; each trip sells box models with slots, a price and a
set of allowed bottle kinds.  A box ships to exactly one supplier and belongs
to exactly one trip.  A deposit is only paid if the trip **arrives** on or
before the voucher's latest return day.  The objective is
``paid deposits - box cost``; ties prefer shipping more bottles.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional, Sequence

from . import bottles
from .errors import Conflict, NotFoundError, ValidationError
from .rules import advance_day, require_day, require_int, require_text
from .schema import transaction

MAX_BOTTLES_PER_SHIPMENT = 18
MAX_TRIPS_PER_SHIPMENT = 3


# ---------------------------------------------------------------------------
# small views
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Credential:
    bottle_id: str
    supplier: str
    package_model: str
    deposit: int
    latest_return_day: int


def fetch_credential(conn: sqlite3.Connection, bottle_id: str) -> Credential:
    row = conn.execute(
        "SELECT * FROM bottle_credentials WHERE bottle_id = ?", (bottle_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"bottle {bottle_id!r} has no supply credential")
    return Credential(
        bottle_id=row["bottle_id"],
        supplier=row["supplier"],
        package_model=row["package_model"],
        deposit=int(row["deposit"]),
        latest_return_day=int(row["latest_return_day"]),
    )


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------
class DepositService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- credentials / configuration ---------------------------------------
    def credential(self, bottle_id: str) -> dict:
        c = fetch_credential(self.conn, bottle_id)
        return {
            "bottle_id": c.bottle_id,
            "supplier": c.supplier,
            "package_model": c.package_model,
            "deposit": c.deposit,
            "latest_return_day": c.latest_return_day,
        }

    def add_trip(
        self, trip_id: str, departure_day: int, arrival_day: int, max_boxes: int
    ) -> dict:
        require_text(trip_id, "trip_id")
        departure_day = require_day(departure_day, "departure_day")
        arrival_day = require_day(arrival_day, "arrival_day")
        max_boxes = require_int(max_boxes, "max_boxes", minimum=0)
        if arrival_day < departure_day:
            raise ValidationError(
                f"trip {trip_id}: arrival_day {arrival_day} precedes "
                f"departure_day {departure_day}"
            )
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO trips(trip_id, departure_day, arrival_day, max_boxes) "
                "VALUES(?, ?, ?, ?)",
                (trip_id, departure_day, arrival_day, max_boxes),
            )
        return {
            "trip_id": trip_id,
            "departure_day": departure_day,
            "arrival_day": arrival_day,
            "max_boxes": max_boxes,
        }

    def add_box_model(
        self, model_id: str, slots: int, kinds: Sequence[str]
    ) -> dict:
        require_text(model_id, "model_id")
        slots = require_int(slots, "slots", minimum=1)
        kinds = [require_text(k, "kind") for k in kinds]
        if not kinds:
            raise ValidationError("a box model must allow at least one kind")
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO box_models(model_id, slots) VALUES(?, ?)",
                (model_id, slots),
            )
            for kind in kinds:
                if self.conn.execute(
                    "SELECT 1 FROM kinds WHERE kind = ?", (kind,)
                ).fetchone() is None:
                    raise NotFoundError(f"unknown kind {kind!r}")
                self.conn.execute(
                    "INSERT INTO box_model_kinds(model_id, kind) VALUES(?, ?)",
                    (model_id, kind),
                )
        return {"model_id": model_id, "slots": slots, "kinds": sorted(kinds)}

    def add_trip_offer(self, trip_id: str, model_id: str, price: int) -> dict:
        require_text(trip_id, "trip_id")
        require_text(model_id, "model_id")
        price = require_int(price, "price", minimum=0)
        with transaction(self.conn):
            if self.conn.execute(
                "SELECT 1 FROM trips WHERE trip_id = ?", (trip_id,)
            ).fetchone() is None:
                raise NotFoundError(f"unknown trip {trip_id!r}")
            if self.conn.execute(
                "SELECT 1 FROM box_models WHERE model_id = ?", (model_id,)
            ).fetchone() is None:
                raise NotFoundError(f"unknown box model {model_id!r}")
            self.conn.execute(
                "INSERT INTO trip_box_offers(trip_id, model_id, price) "
                "VALUES(?, ?, ?)",
                (trip_id, model_id, price),
            )
        return {"trip_id": trip_id, "model_id": model_id, "price": price}

    def add_supplier_kind(self, supplier: str, kind: str) -> dict:
        require_text(supplier, "supplier")
        require_text(kind, "kind")
        with transaction(self.conn):
            if self.conn.execute(
                "SELECT 1 FROM kinds WHERE kind = ?", (kind,)
            ).fetchone() is None:
                raise NotFoundError(f"unknown kind {kind!r}")
            self.conn.execute(
                "INSERT INTO supplier_kinds(supplier, kind) VALUES(?, ?)",
                (supplier, kind),
            )
        return {"supplier": supplier, "kind": kind}

    # -- stocktake version -------------------------------------------------
    def recount(self, bottle_id: str, day: int) -> dict:
        """Bump the bottle's stocktake version (a fresh physical count)."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            bottles.fetch_bottle(self.conn, bottle_id)
            self.conn.execute(
                "UPDATE bottles SET inventory_version = inventory_version + 1 "
                "WHERE bottle_id = ?",
                (bottle_id,),
            )
            row = self.conn.execute(
                "SELECT inventory_version FROM bottles WHERE bottle_id = ?",
                (bottle_id,),
            ).fetchone()
        return {
            "bottle_id": bottle_id,
            "inventory_version": int(row["inventory_version"]),
        }

    def inventory_version(self, bottle_id: str) -> int:
        row = self.conn.execute(
            "SELECT inventory_version FROM bottles WHERE bottle_id = ?",
            (bottle_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown bottle {bottle_id!r}")
        return int(row["inventory_version"])

    # -- clearance ---------------------------------------------------------
    def open_clearance(self, bottle_id: str, portions: int, day: int) -> dict:
        """Open a physical write-off of expired, still-on-hand portions."""
        portions = require_int(portions, "portions", minimum=1)
        with transaction(self.conn):
            advance_day(self.conn, day)
            bottle = bottles.fetch_bottle(self.conn, bottle_id)
            if bottle.validity(day) != bottles.EXPIRED:
                raise Conflict(
                    f"bottle {bottle_id} is not expired on day {day}; only "
                    f"remaining portions that can no longer be led may be cleared"
                )
            alloc = bottles.allocation(self.conn, bottle, day)
            if alloc.reserved > 0:
                raise Conflict(
                    f"bottle {bottle_id} has {alloc.reserved} portion(s) actively "
                    f"reserved; clearance is not allowed while reserved"
                )
            if alloc.unallocated == 0:
                raise Conflict(
                    f"bottle {bottle_id} has no unallocated portions to clear"
                )
            if portions != alloc.unallocated:
                raise Conflict(
                    f"bottle {bottle_id} has exactly {alloc.unallocated} "
                    f"unallocated portion(s); clearance must match that number, "
                    f"got {portions}"
                )
            pending = self.conn.execute(
                "SELECT request_id FROM clearance_requests "
                "WHERE bottle_id = ? AND status = 'pending'",
                (bottle_id,),
            ).fetchone()
            if pending is not None:
                raise Conflict(
                    f"bottle {bottle_id} already has pending clearance "
                    f"{pending['request_id']!r}"
                )
            request_id = self._next_clearance_id(bottle_id)
            version = self.inventory_version(bottle_id)
            self.conn.execute(
                "INSERT INTO clearance_requests(request_id, bottle_id, "
                "inventory_version, portions, status, created_day) "
                "VALUES(?, ?, ?, ?, 'pending', ?)",
                (request_id, bottle_id, version, portions, day),
            )
        return self.clearance_view(request_id)

    def sign_clearance(
        self,
        request_id: str,
        counter_id: str,
        day: int,
        inventory_version: Optional[int] = None,
        portions: Optional[int] = None,
    ) -> dict:
        """Record one counter's sign-off; clear once two agree exactly.

        Both counters must sign the *same* stocktake version and the exact
        remaining unallocated quantity.  Signing a different version leaves the
        request pending (and therefore unusable for a voucher).
        """
        require_text(counter_id, "counter_id")
        with transaction(self.conn):
            advance_day(self.conn, day)
            request = self.conn.execute(
                "SELECT * FROM clearance_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if request is None:
                raise NotFoundError(f"unknown clearance request {request_id!r}")
            if request["status"] != "pending":
                raise Conflict(f"clearance request {request_id} is already cleared")

            bottle_id = request["bottle_id"]
            bottle = bottles.fetch_bottle(self.conn, bottle_id)
            version = (
                request["inventory_version"]
                if inventory_version is None
                else require_int(inventory_version, "inventory_version", minimum=0)
            )
            signed_portions = (
                request["portions"]
                if portions is None
                else require_int(portions, "portions", minimum=0)
            )
            if self.conn.execute(
                "SELECT 1 FROM clearance_signoffs WHERE request_id = ? "
                "AND counter_id = ?",
                (request_id, counter_id),
            ).fetchone() is not None:
                raise Conflict(
                    f"counter {counter_id!r} already signed request {request_id}"
                )
            self.conn.execute(
                "INSERT INTO clearance_signoffs(request_id, counter_id, day, "
                "inventory_version, portions) VALUES(?, ?, ?, ?, ?)",
                (request_id, counter_id, day, version, signed_portions),
            )

            if self._clearance_is_complete(request, bottle, day):
                self.conn.execute(
                    "UPDATE clearance_requests SET status = 'cleared', "
                    "cleared_day = ? WHERE request_id = ?",
                    (day, request_id),
                )
        return self.clearance_view(request_id)

    def _clearance_is_complete(
        self, request: sqlite3.Row, bottle: bottles.BottleRow, day: int
    ) -> bool:
        if bottle.validity(day) != bottles.EXPIRED:
            return False
        alloc = bottles.allocation(self.conn, bottle, day)
        if alloc.reserved > 0 or alloc.unallocated != request["portions"]:
            return False
        if self.inventory_version(bottle.bottle_id) != request["inventory_version"]:
            return False
        signs = self.conn.execute(
            "SELECT counter_id, inventory_version, portions FROM clearance_signoffs "
            "WHERE request_id = ?",
            (request["request_id"],),
        ).fetchall()
        if len({s["counter_id"] for s in signs}) < 2:
            return False
        versions = {int(s["inventory_version"]) for s in signs}
        portions = {int(s["portions"]) for s in signs}
        return (
            versions == {int(request["inventory_version"])}
            and portions == {int(request["portions"])}
        )

    def clearance_view(self, request_id: str) -> dict:
        request = self.conn.execute(
            "SELECT * FROM clearance_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if request is None:
            raise NotFoundError(f"unknown clearance request {request_id!r}")
        signs = self.conn.execute(
            "SELECT counter_id, day, inventory_version, portions "
            "FROM clearance_signoffs WHERE request_id = ? "
            "ORDER BY counter_id",
            (request_id,),
        ).fetchall()
        return {
            "request_id": request["request_id"],
            "bottle_id": request["bottle_id"],
            "inventory_version": int(request["inventory_version"]),
            "portions": int(request["portions"]),
            "status": request["status"],
            "created_day": int(request["created_day"]),
            "cleared_day": None
            if request["cleared_day"] is None
            else int(request["cleared_day"]),
            "signoffs": [
                {
                    "counter_id": s["counter_id"],
                    "day": int(s["day"]),
                    "inventory_version": int(s["inventory_version"]),
                    "portions": int(s["portions"]),
                }
                for s in signs
            ],
        }

    def clearances(self, bottle_id: Optional[str] = None) -> list[dict]:
        if bottle_id is None:
            rows = self.conn.execute(
                "SELECT request_id FROM clearance_requests ORDER BY request_id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT request_id FROM clearance_requests WHERE bottle_id = ? "
                "ORDER BY request_id",
                (bottle_id,),
            ).fetchall()
        return [self.clearance_view(r["request_id"]) for r in rows]

    # -- vouchers ----------------------------------------------------------
    def voucher_blockers(self, bottle_id: str, day: int) -> list[str]:
        """Explain why ``bottle_id`` may not receive a refund voucher."""
        require_day(day)
        bottle = bottles.fetch_bottle(self.conn, bottle_id)
        blockers: list[str] = []
        cred_row = self.conn.execute(
            "SELECT * FROM bottle_credentials WHERE bottle_id = ?", (bottle_id,)
        ).fetchone()
        if cred_row is None:
            blockers.append("missing supply credential")
            return blockers
        latest = int(cred_row["latest_return_day"])
        if day > latest:
            blockers.append(
                f"day {day} is past the latest return day {latest}"
            )
        alloc = bottles.allocation(self.conn, bottle, day)
        if alloc.reserved > 0:
            blockers.append(
                f"{alloc.reserved} portion(s) are still actively reserved"
            )
        if alloc.unallocated > 0:
            if bottle.validity(day) == bottles.EXPIRED:
                blockers.append(
                    f"{alloc.unallocated} expired portion(s) remain physically in "
                    f"the bottle; two counters must clear them on the same "
                    f"stocktake version before a voucher may be requested"
                )
            else:
                blockers.append(
                    f"{alloc.unallocated} portion(s) are still leadable and have "
                    f"not been used"
                )
        return blockers

    def explain_voucher(self, bottle_id: str, day: int) -> dict:
        """Independent warehouse check: why (not) eligible, and what exists."""
        bottle = bottles.fetch_bottle(self.conn, bottle_id)
        alloc = bottles.allocation(self.conn, bottle, day).as_dict()
        blockers = self.voucher_blockers(bottle_id, day)
        voucher = self.conn.execute(
            "SELECT voucher_id, status FROM vouchers WHERE bottle_id = ? "
            "AND status IN ('open', 'shipped')",
            (bottle_id,),
        ).fetchone()
        return {
            "bottle_id": bottle_id,
            "day": day,
            "allocation": alloc,
            "eligible": not blockers,
            "blockers": blockers,
            "voucher_id": None if voucher is None else voucher["voucher_id"],
            "voucher_status": None if voucher is None else voucher["status"],
        }

    def apply_voucher(self, bottle_id: str, day: int) -> dict:
        """Issue the one surviving refund voucher for a physical bottle."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            blockers = self.voucher_blockers(bottle_id, day)
            if blockers:
                raise Conflict(
                    f"bottle {bottle_id} is not refund-voucher eligible: "
                    + "; ".join(blockers)
                )
            existing = self.conn.execute(
                "SELECT voucher_id FROM vouchers WHERE bottle_id = ? "
                "AND status IN ('open', 'shipped')",
                (bottle_id,),
            ).fetchone()
            if existing is not None:
                raise Conflict(
                    f"bottle {bottle_id} already has surviving voucher "
                    f"{existing['voucher_id']!r}"
                )
            cred = fetch_credential(self.conn, bottle_id)
            voucher_id = f"VOUCHER:{bottle_id}"
            self.conn.execute(
                "INSERT INTO vouchers(voucher_id, bottle_id, supplier, "
                "package_model, deposit, latest_return_day, status, created_day) "
                "VALUES(?, ?, ?, ?, ?, ?, 'open', ?)",
                (
                    voucher_id,
                    bottle_id,
                    cred.supplier,
                    cred.package_model,
                    cred.deposit,
                    cred.latest_return_day,
                    day,
                ),
            )
        return self.voucher_view(voucher_id)

    def voucher_view(self, voucher_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM vouchers WHERE voucher_id = ?", (voucher_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown voucher {voucher_id!r}")
        kind = bottles.fetch_bottle(self.conn, row["bottle_id"]).kind
        return {
            "voucher_id": row["voucher_id"],
            "bottle_id": row["bottle_id"],
            "kind": kind,
            "supplier": row["supplier"],
            "package_model": row["package_model"],
            "deposit": int(row["deposit"]),
            "latest_return_day": int(row["latest_return_day"]),
            "status": row["status"],
            "created_day": int(row["created_day"]),
            "shipped_day": None
            if row["shipped_day"] is None
            else int(row["shipped_day"]),
            "shipment_id": row["shipment_id"],
        }

    def vouchers(self, status: Optional[str] = None) -> list[dict]:
        if status is None:
            rows = self.conn.execute(
                "SELECT voucher_id FROM vouchers ORDER BY voucher_id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT voucher_id FROM vouchers WHERE status = ? "
                "ORDER BY voucher_id",
                (status,),
            ).fetchall()
        return [self.voucher_view(r["voucher_id"]) for r in rows]

    # -- shipping ----------------------------------------------------------
    def _offers(self, day: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT o.trip_id, o.model_id, o.price, t.arrival_day,
                   t.departure_day, t.max_boxes, m.slots
              FROM trip_box_offers o
              JOIN trips t ON t.trip_id = o.trip_id
              JOIN box_models m ON m.model_id = o.model_id
             WHERE t.departure_day >= ?
             ORDER BY o.trip_id, o.model_id
            """,
            (day,),
        ).fetchall()
        offers: list[dict] = []
        for row in rows:
            allowed = {
                r["kind"]
                for r in self.conn.execute(
                    "SELECT kind FROM box_model_kinds WHERE model_id = ?",
                    (row["model_id"],),
                )
            }
            offers.append(
                {
                    "trip_id": row["trip_id"],
                    "model_id": row["model_id"],
                    "price": int(row["price"]),
                    "arrival_day": int(row["arrival_day"]),
                    "departure_day": int(row["departure_day"]),
                    "max_boxes": int(row["max_boxes"]),
                    "slots": int(row["slots"]),
                    "allowed": allowed,
                }
            )
        return offers

    def _supplier_kinds(self) -> set[tuple[str, str]]:
        return {
            (r["supplier"], r["kind"])
            for r in self.conn.execute("SELECT supplier, kind FROM supplier_kinds")
        }

    def plan_shipment(
        self,
        day: int,
        max_bottles: int = MAX_BOTTLES_PER_SHIPMENT,
        max_trips: int = MAX_TRIPS_PER_SHIPMENT,
    ) -> dict:
        """Compute the optimal loading (never mutates); may ship nothing."""
        require_day(day)
        max_bottles = require_int(max_bottles, "max_bottles", minimum=0)
        max_trips = require_int(max_trips, "max_trips", minimum=0)
        open_vouchers = [
            v for v in self.vouchers("open") if v["created_day"] <= day
        ]
        offers = self._offers(day)
        plan = _optimize(
            open_vouchers,
            offers,
            self._supplier_kinds(),
            max_bottles,
            max_trips,
        )
        plan["day"] = day
        return plan

    def ship(
        self,
        day: int,
        shipment_id: Optional[str] = None,
        max_bottles: int = MAX_BOTTLES_PER_SHIPMENT,
        max_trips: int = MAX_TRIPS_PER_SHIPMENT,
    ) -> dict:
        """Plan and atomically persist an optimal shipment."""
        with transaction(self.conn):
            advance_day(self.conn, day)
            plan = self.plan_shipment(day, max_bottles, max_trips)
            if plan["bottle_count"] == 0:
                raise Conflict("no profitable shipment exists; nothing to ship")
            shipment_id = self._next_shipment_id(shipment_id)
            self.conn.execute(
                "INSERT INTO shipments(shipment_id, day, deposit_total, "
                "box_cost, profit, bottle_count) VALUES(?, ?, ?, ?, ?, ?)",
                (
                    shipment_id,
                    day,
                    plan["deposit_total"],
                    plan["box_cost"],
                    plan["profit"],
                    plan["bottle_count"],
                ),
            )
            for index, box in enumerate(plan["boxes"]):
                box_id = f"{shipment_id}#B{index}"
                self.conn.execute(
                    "INSERT INTO shipped_boxes(box_id, shipment_id, trip_id, "
                    "model_id, supplier, price) VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        box_id,
                        shipment_id,
                        box["trip_id"],
                        box["model_id"],
                        box["supplier"],
                        box["price"],
                    ),
                )
                for voucher_id in box["voucher_ids"]:
                    self._load_voucher(box_id, voucher_id, shipment_id, day)
        return self.shipment_view(shipment_id)

    def _load_voucher(
        self, box_id: str, voucher_id: str, shipment_id: str, day: int
    ) -> None:
        voucher = self.conn.execute(
            "SELECT * FROM vouchers WHERE voucher_id = ?", (voucher_id,)
        ).fetchone()
        if voucher is None:
            raise NotFoundError(f"unknown voucher {voucher_id!r}")
        if voucher["status"] != "open":
            raise Conflict(
                f"voucher {voucher_id} is locked (status {voucher['status']}); "
                f"it cannot be loaded again"
            )
        self.conn.execute(
            "INSERT INTO shipped_items(box_id, voucher_id, bottle_id, deposit_paid) "
            "VALUES(?, ?, ?, ?)",
            (box_id, voucher_id, voucher["bottle_id"], int(voucher["deposit"])),
        )
        self.conn.execute(
            "UPDATE vouchers SET status = 'shipped', shipped_day = ?, "
            "shipment_id = ? WHERE voucher_id = ? AND status = 'open'",
            (day, shipment_id, voucher_id),
        )

    def shipment_view(self, shipment_id: str) -> dict:
        shipment = self.conn.execute(
            "SELECT * FROM shipments WHERE shipment_id = ?", (shipment_id,)
        ).fetchone()
        if shipment is None:
            raise NotFoundError(f"unknown shipment {shipment_id!r}")
        boxes = []
        for box in self.conn.execute(
            "SELECT * FROM shipped_boxes WHERE shipment_id = ? ORDER BY box_id",
            (shipment_id,),
        ).fetchall():
            items = self.conn.execute(
                "SELECT voucher_id, bottle_id, deposit_paid FROM shipped_items "
                "WHERE box_id = ? ORDER BY item_id",
                (box["box_id"],),
            ).fetchall()
            boxes.append(
                {
                    "box_id": box["box_id"],
                    "trip_id": box["trip_id"],
                    "model_id": box["model_id"],
                    "supplier": box["supplier"],
                    "price": int(box["price"]),
                    "voucher_ids": [i["voucher_id"] for i in items],
                    "bottles": [i["bottle_id"] for i in items],
                    "deposit_total": sum(int(i["deposit_paid"]) for i in items),
                }
            )
        return {
            "shipment_id": shipment["shipment_id"],
            "day": int(shipment["day"]),
            "deposit_total": int(shipment["deposit_total"]),
            "box_cost": int(shipment["box_cost"]),
            "profit": int(shipment["profit"]),
            "bottle_count": int(shipment["bottle_count"]),
            "trips_used": sorted({b["trip_id"] for b in boxes}),
            "boxes": boxes,
        }

    # -- helpers -----------------------------------------------------------
    def _next_clearance_id(self, bottle_id: str) -> str:
        base = f"CLR:{bottle_id}"
        candidate = base
        suffix = 1
        while self.conn.execute(
            "SELECT 1 FROM clearance_requests WHERE request_id = ?", (candidate,)
        ).fetchone() is not None:
            suffix += 1
            candidate = f"{base}#{suffix}"
        return candidate

    def _next_shipment_id(self, requested: Optional[str]) -> str:
        if requested is not None:
            require_text(requested, "shipment_id")
            if self.conn.execute(
                "SELECT 1 FROM shipments WHERE shipment_id = ?", (requested,)
            ).fetchone() is not None:
                raise Conflict(f"shipment {requested!r} already exists")
            return requested
        index = 1
        while self.conn.execute(
            "SELECT 1 FROM shipments WHERE shipment_id = ?", (f"SHP{index}",)
        ).fetchone() is not None:
            index += 1
        return f"SHP{index}"


# ---------------------------------------------------------------------------
# exact shipment optimiser
# ---------------------------------------------------------------------------
def _optimize(
    vouchers: list[dict],
    offers: list[dict],
    supplier_kinds: set[tuple[str, str]],
    max_bottles: int,
    max_trips: int,
) -> dict:
    """Exact search: maximise deposit - box cost, then bottle count.

    ``vouchers`` are open voucher views; ``offers`` are per-trip box offers.
    Returns a plan dict (possibly empty).  Vouchers are processed in
    descending deposit order, which lets us greedily fill free slots of already
    bought boxes (a later voucher can never out-pay a slot we fill now) and
    prune with a suffix-sum upper bound.  A same-state memo collapses the many
    equivalent box orders.
    """
    empty = {
        "boxes": [],
        "trips_used": [],
        "deposit_total": 0,
        "box_cost": 0,
        "profit": 0,
        "bottle_count": 0,
    }
    if not vouchers or not offers or max_bottles <= 0 or max_trips <= 0:
        return empty

    def payable(v: dict, o: dict) -> bool:
        return (
            o["arrival_day"] <= v["latest_return_day"]
            and v["kind"] in o["allowed"]
            and (v["supplier"], v["kind"]) in supplier_kinds
        )

    items = [v for v in vouchers if any(payable(v, o) for o in offers)]
    if not items:
        return empty
    # Payable a bottle without paying a full extra box is strictly better, so
    # sort high deposits first; when two are equal, prefer a smaller box later.
    items.sort(key=lambda v: (-v["deposit"], v["supplier"], v["voucher_id"]))
    n = len(items)
    suffix = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix[i] = suffix[i + 1] + items[i]["deposit"]

    boxes: list[dict] = []
    trip_counts: dict[str, int] = {}
    best = {"profit": 0, "count": 0, "boxes": []}
    # State -> list of (profit, count) already expanded; dominated states are
    # those whose profit and count are both <= an existing entry.
    memo: dict[tuple, list[tuple[int, int]]] = {}

    def canonical_state() -> tuple:
        return tuple(
            sorted(
                (b["trip_id"], b["model_id"], b["supplier"], b["rem"])
                for b in boxes
            )
        )

    def snapshot() -> list[dict]:
        return [
            {
                "trip_id": box["trip_id"],
                "model_id": box["model_id"],
                "price": box["price"],
                "supplier": box["supplier"],
                "voucher_ids": [items[i]["voucher_id"] for i in box["items"]],
                "bottles": [items[i]["bottle_id"] for i in box["items"]],
            }
            for box in boxes
        ]

    def dfs(index: int, profit: int, count: int) -> None:
        if profit > best["profit"] or (
            profit == best["profit"] and count > best["count"]
        ):
            best["profit"] = profit
            best["count"] = count
            best["boxes"] = snapshot()
        if index == n:
            return
        # Upper bound: every remaining voucher's full deposit, no box cost.
        if profit + suffix[index] < best["profit"]:
            return
        if (
            profit + suffix[index] == best["profit"]
            and count + (n - index) <= best["count"]
        ):
            return

        state = (index, canonical_state())
        history = memo.setdefault(state, [])
        for seen_profit, seen_count in history:
            if profit <= seen_profit and count <= seen_count:
                return
        history[:] = [
            (p, c) for (p, c) in history
            if not (p <= profit and c <= count)
        ]
        history.append((profit, count))

        item = items[index]

        # 1) fill a free slot of an already bought box (dedupe equal boxes)
        if count + 1 <= max_bottles:
            seen_boxes: set[tuple] = set()
            for box in boxes:
                if box["supplier"] != item["supplier"]:
                    continue
                if item["kind"] not in box["allowed"]:
                    continue
                if box["rem"] <= 0:
                    continue
                if box["arrival_day"] > item["latest_return_day"]:
                    continue
                signature = (
                    box["trip_id"],
                    box["model_id"],
                    box["supplier"],
                    box["rem"],
                )
                if signature in seen_boxes:
                    continue
                seen_boxes.add(signature)
                box["rem"] -= 1
                box["items"].append(index)
                dfs(index + 1, profit + item["deposit"], count + 1)
                box["items"].pop()
                box["rem"] += 1

        # 2) buy a brand-new box on some trip / model
        if count + 1 <= max_bottles:
            tried: set[tuple] = set()
            for offer in offers:
                if not payable(item, offer):
                    continue
                trip = offer["trip_id"]
                key = (trip, offer["model_id"])
                if key in tried:
                    continue
                tried.add(key)
                new_trip = trip not in trip_counts
                if new_trip and len(trip_counts) >= max_trips:
                    continue
                if trip_counts.get(trip, 0) + 1 > offer["max_boxes"]:
                    continue
                boxes.append(
                    {
                        "trip_id": trip,
                        "model_id": offer["model_id"],
                        "price": offer["price"],
                        "supplier": item["supplier"],
                        "allowed": offer["allowed"],
                        "arrival_day": offer["arrival_day"],
                        "rem": offer["slots"] - 1,
                        "items": [index],
                    }
                )
                trip_counts[trip] = trip_counts.get(trip, 0) + 1
                dfs(
                    index + 1,
                    profit + item["deposit"] - offer["price"],
                    count + 1,
                )
                trip_counts[trip] -= 1
                if trip_counts[trip] == 0:
                    del trip_counts[trip]
                boxes.pop()

        # 3) leave this voucher unshipped
        dfs(index + 1, profit, count)

    dfs(0, 0, 0)

    boxes_out = best["boxes"]
    deposit_lookup = {v["voucher_id"]: v["deposit"] for v in items}
    deposit_total = sum(
        deposit_lookup[vid] for box in boxes_out for vid in box["voucher_ids"]
    )
    box_cost = sum(box["price"] for box in boxes_out)
    return {
        "boxes": boxes_out,
        "trips_used": sorted({b["trip_id"] for b in boxes_out}),
        "deposit_total": deposit_total,
        "box_cost": box_cost,
        "profit": deposit_total - box_cost,
        "bottle_count": sum(len(b["voucher_ids"]) for b in boxes_out),
    }
