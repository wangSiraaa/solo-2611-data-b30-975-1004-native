"""Recycling: turn SCRAP-graded returned units into cakes per sealed process sheets.

Only *named physical items* the after-sales ledger actually received with grade
``SCRAP`` can become furnace material.  A manual scrap count booked with
``adjust_inventory`` is just a quantity per SKU — it names no items, binds no
sale and no coupon group, so it can never justify a cake.  That is why the
batch scrap total alone is never enough.

Process sheets are sealed (idempotent by content, conflict on change):

* ``UNIT`` sheet, keyed by SKU: a plain chip yields its recipe's red/white
  integer portions per piece, one cake per physical item.
* ``GROUP`` sheet, keyed by coupon id: chips actually assigned to that coupon
  in a sale's stored quote were filmed together and are de-filmed as one
  group.  The sheet lists each member's recipe and the fixed whole-group loss
  per colour.  A group cake appears only when *every* member was received as
  SCRAP and the summed material covers the loss in each colour; until then the
  received members stay tagged (pending).

Group membership is always read from the sale's stored quote (the actual
snapshot and the actual coupon group), never from caller claims.  RESTOCK
units are never eligible.  Every qualified item enters at most one recycling
batch — enforced by the ``cake_items`` primary key inside the batch
transaction.  Recycling never mutates the after-sales inventory buckets, so
the old stock queries keep working unchanged.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .db import Database, canonical_json, json_loads, utc_now
from .errors import (
    INVALID_INPUT,
    INVALID_SHEET,
    SHEET_CONFLICT,
    UNKNOWN_BATCH,
    UNKNOWN_SHEET,
    BasketError,
)
from .validation import is_int, require_id

COLORS = ("red", "white")


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _color_pair(value: Any, field: str, *, allow_zero_sum: bool) -> Dict[str, int]:
    if not isinstance(value, Mapping):
        raise BasketError(INVALID_SHEET, f"{field} must be an object with red/white integer portions")
    pair: Dict[str, int] = {}
    for color in COLORS:
        portion = value.get(color)
        if not is_int(portion) or portion < 0:
            raise BasketError(
                INVALID_SHEET,
                f"{field}.{color} must be a non-negative integer",
                {"field": f"{field}.{color}", "value": portion},
            )
        pair[color] = portion
    extra = set(value) - set(COLORS)
    if extra:
        raise BasketError(INVALID_SHEET, f"{field} has unknown colours", {"field": field, "extra": sorted(extra)})
    if not allow_zero_sum and pair["red"] + pair["white"] < 1:
        raise BasketError(INVALID_SHEET, f"{field} must contain at least one portion of material")
    return pair


def validate_sheet(sheet: Any) -> Dict[str, Any]:
    """Validate and canonicalise a process sheet."""

    if not isinstance(sheet, Mapping):
        raise BasketError(INVALID_SHEET, "a process sheet must be an object")
    kind = sheet.get("kind")
    if kind == "UNIT":
        sku = require_id(sheet.get("sku"), INVALID_SHEET, "sheet.sku")
        recipe = _color_pair(sheet.get("recipe"), "sheet.recipe", allow_zero_sum=False)
        return {"kind": "UNIT", "sku": sku, "recipe": recipe}
    if kind == "GROUP":
        coupon_id = require_id(sheet.get("coupon_id"), INVALID_SHEET, "sheet.coupon_id")
        members_raw = sheet.get("members")
        if not isinstance(members_raw, Mapping) or len(members_raw) == 0:
            raise BasketError(INVALID_SHEET, "sheet.members must be a non-empty sku -> recipe mapping")
        members: Dict[str, Dict[str, int]] = {}
        for sku, recipe in members_raw.items():
            if not isinstance(sku, str) or sku == "":
                raise BasketError(INVALID_SHEET, "sheet.members keys must be non-empty sku strings")
            members[sku] = _color_pair(recipe, f"sheet.members[{sku!r}]", allow_zero_sum=False)
        loss = _color_pair(sheet.get("loss"), "sheet.loss", allow_zero_sum=True)
        return {
            "kind": "GROUP",
            "coupon_id": coupon_id,
            "members": {sku: members[sku] for sku in sorted(members)},
            "loss": loss,
        }
    raise BasketError(INVALID_SHEET, "sheet.kind must be UNIT or GROUP", {"kind": kind})


def sheet_id_for(sheet: Mapping[str, Any]) -> str:
    if sheet["kind"] == "UNIT":
        return f"UNIT/{sheet['sku']}"
    return f"GROUP/{sheet['coupon_id']}"


def _unit_batch_no(sale_id: str, unit_id: str) -> str:
    return "cake-u-" + _hash({"kind": "UNIT", "sale_id": sale_id, "unit_id": unit_id})[:16]


def _group_batch_no(sale_id: str, coupon_id: str, member_ids: List[str]) -> str:
    return "cake-g-" + _hash(
        {"kind": "GROUP", "sale_id": sale_id, "coupon_id": coupon_id, "members": sorted(member_ids)}
    )[:16]


class RecyclingService:
    """Sealed process sheets, recycling batches, cakes and their audit."""

    def __init__(self, db: Database):
        self.db = db

    # -------------------------------------------------------------- sheets
    def register_sheet(self, sheet: Any) -> str:
        canonical = validate_sheet(sheet)
        sheet_id = sheet_id_for(canonical)
        content = canonical_json(canonical)
        with self.db.transaction() as conn:
            row = conn.execute("SELECT content_json FROM process_sheets WHERE sheet_id = ?", (sheet_id,)).fetchone()
            if row is not None:
                if row["content_json"] != content:
                    raise BasketError(
                        SHEET_CONFLICT,
                        "process sheet already sealed with different content",
                        {"sheet_id": sheet_id},
                    )
                return sheet_id
            conn.execute(
                "INSERT INTO process_sheets (sheet_id, kind, key, content_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (sheet_id, canonical["kind"], canonical.get("sku") or canonical.get("coupon_id"), content, utc_now()),
            )
        return sheet_id

    def get_sheet(self, sheet_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM process_sheets WHERE sheet_id = ?", (sheet_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_SHEET, "unknown process sheet", {"sheet_id": sheet_id})
        return {"sheet_id": row["sheet_id"], "created_at": row["created_at"], **json_loads(row["content_json"])}

    def sheets(self) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute("SELECT * FROM process_sheets ORDER BY sheet_id").fetchall()
        return [{"sheet_id": row["sheet_id"], **json_loads(row["content_json"])} for row in rows]

    def _sheet_maps(self, conn) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        rows = conn.execute("SELECT kind, key, content_json FROM process_sheets").fetchall()
        unit_sheets: Dict[str, Dict[str, Any]] = {}
        group_sheets: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            content = json_loads(row["content_json"])
            if row["kind"] == "UNIT":
                unit_sheets[row["key"]] = content
            else:
                group_sheets[row["key"]] = content
        return unit_sheets, group_sheets

    # -------------------------------------------------------------- batches
    def run_batch(self, batch_id: str) -> Dict[str, Any]:
        """Sweep every unconsumed SCRAP-received unit into cakes, atomically.

        Replaying an existing ``batch_id`` returns the stored result; new
        arrivals are picked up by running a *new* batch id.
        """

        batch_id = require_id(batch_id, INVALID_INPUT, "batch_id")
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT result_json FROM recycle_batches WHERE batch_id = ?", (batch_id,)).fetchone()
            if existing is not None:
                return json_loads(existing["result_json"])

            unit_sheets, group_sheets = self._sheet_maps(conn)
            scrap_rows = conn.execute(
                """
                SELECT su.sale_id, su.unit_id, su.sku, su.return_request_id, s.quote_json
                FROM sale_units su
                JOIN sales s ON s.sale_id = su.sale_id
                WHERE su.returned = 1 AND su.grade = 'SCRAP'
                ORDER BY su.sale_id, su.unit_id
                """
            ).fetchall()
            consumed = {
                (row["sale_id"], row["unit_id"])
                for row in conn.execute("SELECT sale_id, unit_id FROM cake_items").fetchall()
            }

            # Invert each sale's stored quote assignments: unit -> coupon id.
            # This is the actual snapshot's actual coupon group, never a claim.
            coupon_of: Dict[Tuple[str, str], str] = {}
            members_of: Dict[Tuple[str, str], List[str]] = {}
            sales_seen: set[str] = set()
            for row in scrap_rows:
                sale_id = row["sale_id"]
                if sale_id in sales_seen:
                    continue
                sales_seen.add(sale_id)
                assignments = json_loads(row["quote_json"])["assignments"]
                for coupon_id, unit_ids in assignments.items():
                    members_of[(sale_id, coupon_id)] = sorted(unit_ids)
                    for unit_id in unit_ids:
                        coupon_of[(sale_id, unit_id)] = coupon_id

            cakes: List[Dict[str, Any]] = []
            pending: List[Dict[str, Any]] = []
            skipped: List[Dict[str, Any]] = []
            problems: List[Dict[str, Any]] = []
            groups_seen: set[Tuple[str, str]] = set()

            for row in scrap_rows:
                sale_id, unit_id, sku = row["sale_id"], row["unit_id"], row["sku"]
                if (sale_id, unit_id) in consumed:
                    continue
                coupon_id = coupon_of.get((sale_id, unit_id))
                group_sheet = group_sheets.get(coupon_id) if coupon_id is not None else None

                if group_sheet is None:
                    # Plain physical item: one cake per piece from its SKU sheet.
                    sheet = unit_sheets.get(sku)
                    if sheet is None:
                        skipped.append({"sale_id": sale_id, "unit_id": unit_id, "sku": sku, "reason": "NO_SHEET"})
                        continue
                    recipe = sheet["recipe"]
                    cake = {
                        "batch_no": _unit_batch_no(sale_id, unit_id),
                        "kind": "UNIT",
                        "red": recipe["red"],
                        "white": recipe["white"],
                        "sale_id": sale_id,
                        "coupon_id": None,
                        "unit_id": unit_id,
                        "sku": sku,
                        "request_id": row["return_request_id"],
                        "recipe": dict(recipe),
                    }
                    cakes.append(cake)
                    continue

                # Filmed group member: the whole actual coupon group de-films at once.
                group_key = (sale_id, coupon_id)
                if group_key in groups_seen:
                    continue
                groups_seen.add(group_key)
                members = members_of[group_key]
                status_by_unit = self._member_status(conn, sale_id, members, consumed)
                received = sorted(u for u in members if status_by_unit[u] == "READY")
                if len(received) < len(members):
                    awaiting = sorted(u for u in members if status_by_unit[u] != "READY")
                    entry = {
                        "sale_id": sale_id,
                        "coupon_id": coupon_id,
                        "received": received,
                        "awaiting": awaiting,
                    }
                    if any(status_by_unit[u] == "CONSUMED" for u in members):
                        entry["reason"] = "BLOCKED"
                        problems.append({**entry, "detail": "a group member already entered another batch"})
                    else:
                        pending.append(entry)
                    continue

                # All members SCRAP-received and unconsumed: de-film the group.
                material = {"red": 0, "white": 0}
                member_records: List[Dict[str, Any]] = []
                uncovered = False
                request_by_unit = {r["unit_id"]: r["return_request_id"] for r in scrap_rows if r["sale_id"] == sale_id}
                sku_by_unit = {r["unit_id"]: r["sku"] for r in scrap_rows if r["sale_id"] == sale_id}
                for member in members:
                    member_sku = sku_by_unit[member]
                    recipe = group_sheet["members"].get(member_sku)
                    if recipe is None:
                        problems.append(
                            {
                                "sale_id": sale_id,
                                "coupon_id": coupon_id,
                                "reason": "UNCOVERED",
                                "detail": f"group sheet has no recipe for member sku {member_sku!r}",
                            }
                        )
                        uncovered = True
                        break
                    material["red"] += recipe["red"]
                    material["white"] += recipe["white"]
                    member_records.append(
                        {
                            "unit_id": member,
                            "sku": member_sku,
                            "request_id": request_by_unit[member],
                            "recipe": dict(recipe),
                        }
                    )
                if uncovered:
                    continue
                loss = group_sheet["loss"]
                if material["red"] < loss["red"] or material["white"] < loss["white"]:
                    problems.append(
                        {
                            "sale_id": sale_id,
                            "coupon_id": coupon_id,
                            "reason": "SHORTAGE",
                            "detail": "group material does not cover the fixed loss",
                            "material": material,
                            "loss": dict(loss),
                        }
                    )
                    continue
                cakes.append(
                    {
                        "batch_no": _group_batch_no(sale_id, coupon_id, members),
                        "kind": "GROUP",
                        "red": material["red"] - loss["red"],
                        "white": material["white"] - loss["white"],
                        "sale_id": sale_id,
                        "coupon_id": coupon_id,
                        "members": member_records,
                        "material": material,
                        "loss": dict(loss),
                    }
                )

            for cake in cakes:
                self._insert_cake(conn, batch_id, cake)
            result = {
                "ok": True,
                "batch_id": batch_id,
                "cakes": cakes,
                "pending": pending,
                "skipped": skipped,
                "problems": problems,
                "repeated": False,
            }
            conn.execute(
                "INSERT INTO recycle_batches (batch_id, result_json, created_at) VALUES (?, ?, ?)",
                (batch_id, canonical_json(result), utc_now()),
            )
            return result

    @staticmethod
    def _member_status(conn, sale_id: str, members: List[str], consumed: set) -> Dict[str, str]:
        rows = conn.execute(
            f"SELECT unit_id, returned, grade FROM sale_units WHERE sale_id = ? AND unit_id IN ({','.join('?' * len(members))})",
            (sale_id, *members),
        ).fetchall()
        by_unit = {row["unit_id"]: row for row in rows}
        status: Dict[str, str] = {}
        for unit_id in members:
            if (sale_id, unit_id) in consumed:
                status[unit_id] = "CONSUMED"
            else:
                row = by_unit.get(unit_id)
                status[unit_id] = "READY" if row is not None and row["returned"] and row["grade"] == "SCRAP" else "AWAITING"
        return status

    def _insert_cake(self, conn, batch_id: str, cake: Dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO cakes (batch_no, kind, red, white, sale_id, coupon_id, batch_id, content_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'IN_STOCK', ?)
            """,
            (
                cake["batch_no"],
                cake["kind"],
                cake["red"],
                cake["white"],
                cake["sale_id"],
                cake["coupon_id"],
                batch_id,
                canonical_json(cake),
                utc_now(),
            ),
        )
        items = (
            [(cake["unit_id"], cake["request_id"])]
            if cake["kind"] == "UNIT"
            else [(m["unit_id"], m["request_id"]) for m in cake["members"]]
        )
        for unit_id, request_id in items:
            # The primary key on (sale_id, unit_id) is the guard behind
            # "one qualified item enters exactly one recycling batch".
            conn.execute(
                "INSERT INTO cake_items (sale_id, unit_id, batch_no, batch_id, request_id) VALUES (?, ?, ?, ?, ?)",
                (cake["sale_id"], unit_id, cake["batch_no"], batch_id, request_id),
            )

    def get_batch(self, batch_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT result_json FROM recycle_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_BATCH, "unknown recycling batch", {"batch_id": batch_id})
        result = json_loads(row["result_json"])
        result["repeated"] = True
        return result

    # -------------------------------------------------------------- cakes
    def cakes(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status is not None and status not in ("IN_STOCK", "REQUISITIONED"):
            raise BasketError(INVALID_INPUT, "status must be IN_STOCK or REQUISITIONED", {"status": status})
        with self.db.connection() as conn:
            if status is None:
                rows = conn.execute("SELECT * FROM cakes ORDER BY batch_no").fetchall()
            else:
                rows = conn.execute("SELECT * FROM cakes WHERE status = ? ORDER BY batch_no", (status,)).fetchall()
        return [self._cake_row(row) for row in rows]

    @staticmethod
    def _cake_row(row) -> Dict[str, Any]:
        cake = json_loads(row["content_json"])
        cake["status"] = row["status"]
        cake["requisition_id"] = row["requisition_id"]
        cake["batch_id"] = row["batch_id"]
        return cake

    # -------------------------------------------------------------- audit
    def audit(self) -> Dict[str, Any]:
        """Independently recompute every cake from raw ledger rows and sheets.

        The auditor reads only ``sale_units``/``sales``/``process_sheets`` and
        re-derives each cake's components, material, loss and batch number —
        the recycler's own claims are never trusted.  It also verifies that no
        physical item entered two cakes and that requisitioned cakes point at
        recorded furnace runs.
        """

        checks: List[Dict[str, Any]] = []

        def note(name: str, ok: bool, detail: Any = None) -> None:
            checks.append({"check": name, "ok": ok, "detail": detail})

        with self.db.connection() as conn:
            unit_sheets, group_sheets = self._sheet_maps(conn)
            scrap = {}
            assignments_by_sale: Dict[str, Dict[str, List[str]]] = {}
            for row in conn.execute(
                """
                SELECT su.sale_id, su.unit_id, su.sku, su.returned, su.grade, su.return_request_id, s.quote_json
                FROM sale_units su JOIN sales s ON s.sale_id = su.sale_id
                """
            ).fetchall():
                if row["sale_id"] not in assignments_by_sale:
                    assignments_by_sale[row["sale_id"]] = json_loads(row["quote_json"])["assignments"]
                if row["returned"] and row["grade"] == "SCRAP":
                    scrap[(row["sale_id"], row["unit_id"])] = dict(row)
            cake_rows = conn.execute("SELECT * FROM cakes ORDER BY batch_no").fetchall()
            item_rows = conn.execute("SELECT * FROM cake_items ORDER BY sale_id, unit_id").fetchall()
            run_ids = {row["run_id"] for row in conn.execute("SELECT run_id FROM furnace_runs").fetchall()}

        # 1. Every cake recomputes from raw facts and sealed sheets.
        for row in cake_rows:
            cake = json_loads(row["content_json"])
            name = f"cake.{cake['batch_no']}.recomputes"
            try:
                expected = self._expected_cake(cake, scrap, assignments_by_sale, unit_sheets, group_sheets)
                ok = (
                    expected is not None
                    and expected["red"] == row["red"]
                    and expected["white"] == row["white"]
                    and expected["batch_no"] == row["batch_no"]
                )
                note(name, ok, None if ok else {"stored": {"red": row["red"], "white": row["white"]}, "expected": expected})
            except Exception as exc:  # audit must report, not crash
                note(name, False, str(exc))

        # 2. Every cake's item rows are exactly its declared components.
        items_by_cake: Dict[str, set] = {}
        for row in item_rows:
            items_by_cake.setdefault(row["batch_no"], set()).add((row["sale_id"], row["unit_id"]))
        mismatched = []
        for row in cake_rows:
            cake = json_loads(row["content_json"])
            declared = (
                {(cake["sale_id"], cake["unit_id"])}
                if cake["kind"] == "UNIT"
                else {(cake["sale_id"], member["unit_id"]) for member in cake["members"]}
            )
            if items_by_cake.get(row["batch_no"], set()) != declared:
                mismatched.append(row["batch_no"])
        note("cakes.item_rows_match_components", not mismatched, mismatched or None)

        # 3. No physical item entered two cakes (the one-batch contract).
        seen: Dict[Tuple[str, str], str] = {}
        double: List[Any] = []
        for row in item_rows:
            key = (row["sale_id"], row["unit_id"])
            if key in seen:
                double.append({"sale_id": key[0], "unit_id": key[1], "batches": [seen[key], row["batch_no"]]})
            seen[key] = row["batch_no"]
        note("items.enter_at_most_one_batch", not double, double or None)

        # 4. Every caked item is a real SCRAP-received unit of the claimed sale.
        not_scrap = [
            {"sale_id": row["sale_id"], "unit_id": row["unit_id"]}
            for row in item_rows
            if (row["sale_id"], row["unit_id"]) not in scrap
        ]
        note("items.are_scrap_received_named_units", not not_scrap, not_scrap or None)

        # 5. Requisitioned cakes point at recorded furnace runs.
        dangling = [
            row["batch_no"] for row in cake_rows if row["status"] == "REQUISITIONED" and row["requisition_id"] not in run_ids
        ]
        note("requisitions_reference_recorded_runs", not dangling, dangling or None)

        return {"ok": all(c["ok"] for c in checks), "checks": checks}

    @staticmethod
    def _expected_cake(
        cake: Dict[str, Any],
        scrap: Dict[Tuple[str, str], Dict[str, Any]],
        assignments_by_sale: Dict[str, Dict[str, List[str]]],
        unit_sheets: Dict[str, Dict[str, Any]],
        group_sheets: Dict[str, Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        sale_id = cake["sale_id"]
        if cake["kind"] == "UNIT":
            unit_id = cake["unit_id"]
            received = scrap.get((sale_id, unit_id))
            sheet = unit_sheets.get(received["sku"]) if received else None
            if sheet is None:
                return None
            return {
                "batch_no": _unit_batch_no(sale_id, unit_id),
                "red": sheet["recipe"]["red"],
                "white": sheet["recipe"]["white"],
            }
        coupon_id = cake["coupon_id"]
        members = sorted(assignments_by_sale.get(sale_id, {}).get(coupon_id, []))
        sheet = group_sheets.get(coupon_id)
        if not members or sheet is None:
            return None
        if any((sale_id, member) not in scrap for member in members):
            return None
        material = {"red": 0, "white": 0}
        for member in members:
            recipe = sheet["members"].get(scrap[(sale_id, member)]["sku"])
            if recipe is None:
                return None
            material["red"] += recipe["red"]
            material["white"] += recipe["white"]
        return {
            "batch_no": _group_batch_no(sale_id, coupon_id, members),
            "red": material["red"] - sheet["loss"]["red"],
            "white": material["white"] - sheet["loss"]["white"],
        }
