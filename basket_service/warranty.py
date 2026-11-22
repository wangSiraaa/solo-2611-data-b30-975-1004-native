"""Factory-return warranty service on top of the sales/returns ledger.

A warranty is bound per *physical unit* of an already registered sale:

* warranty terms are published per stable SKU as a base number of days, plus an
  optional promotional addendum (``coupon_id`` + bonus days); the addendum
  belongs only to a unit that actually received that coupon at deal time,
  exactly the per-unit allocation already frozen in ``sale_units``;
* every unit's clock starts on its integer business sale day (day 0 of that
  unit); confirmed factory outages are half-open business-day intervals and
  overlapping work orders stop the clock only once (see ``warranty_engine``);
* late back-dated entry/exit records are allowed and simply recompute a new
  immutable warranty *version* from the business dates; results previously
  given to the service desk are stored against the version that answered them
  and are never overwritten;
* a normal return already recorded in the ledger ends cover on its effective
  business day.

Import order is never a date: every back-filled event carries its own business
day and references a stable sale/return id.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from .db import Database, canonical_json, deep_copy, json_loads, utc_now
from .errors import (
    INVALID_INPUT,
    INVALID_INTERVAL,
    RECORD_CONFLICT,
    RETURN_CONFLICT,
    TERM_CONFLICT,
    UNKNOWN_COUPON_TERM,
    UNKNOWN_RECORD,
    UNKNOWN_SALE,
    UNKNOWN_TERM,
    UNKNOWN_WARRANTY,
    WARRANTY_CONFLICT,
    BasketError,
)
from .validation import is_int, require_id
from .warranty_engine import (
    EXPIRED_BUDGET,
    EXPIRED_RETURN,
    Outage,
    compute_warranty,
    eligibility,
    explain_outages,
)

EXPIRY_REASONS = (EXPIRED_BUDGET, EXPIRED_RETURN)


def _require_day(value: Any, field: str) -> int:
    if not is_int(value) or value < 0:
        raise BasketError(INVALID_INPUT, f"{field} must be a non-negative integer business day", {"field": field, "value": value})
    return value


class WarrantyService:
    def __init__(self, db: Database):
        self.db = db

    # ----------------------------------------------------------- terms ----
    def publish_term(
        self,
        term_id: str,
        sku: str,
        base_days: Any,
        *,
        coupon_id: Optional[str] = None,
        bonus_days: Any = 0,
    ) -> Dict[str, Any]:
        """Publish (or replay) a warranty term for a SKU, optionally a coupon addendum."""

        term_id = require_id(term_id, INVALID_INPUT, "term_id")
        sku = require_id(sku, INVALID_INPUT, "sku")
        if not is_int(base_days) or base_days < 0:
            raise BasketError(INVALID_INPUT, "base_days must be a non-negative integer", {"base_days": base_days})
        if not is_int(bonus_days) or bonus_days < 0:
            raise BasketError(INVALID_INPUT, "bonus_days must be a non-negative integer", {"bonus_days": bonus_days})
        if coupon_id is not None:
            coupon_id = require_id(coupon_id, INVALID_INPUT, "coupon_id")
        if coupon_id is None and bonus_days != 0:
            raise BasketError(INVALID_INPUT, "bonus_days require a coupon_id addendum")
        content = {
            "term_id": term_id,
            "sku": sku,
            "base_days": base_days,
            "coupon_id": coupon_id,
            "bonus_days": bonus_days,
        }
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT * FROM warranty_terms WHERE term_id = ?", (term_id,)).fetchone()
            if existing is not None:
                if existing["content_json"] != canonical_json(content):
                    raise BasketError(TERM_CONFLICT, "term_id already published with different content", {"term_id": term_id})
                return deep_copy(content)
            clash = conn.execute(
                "SELECT term_id FROM warranty_terms WHERE sku = ? AND coupon_id IS ?",
                (sku, coupon_id),
            ).fetchone() if coupon_id is None else conn.execute(
                "SELECT term_id FROM warranty_terms WHERE sku = ? AND coupon_id = ?",
                (sku, coupon_id),
            ).fetchone()
            if clash is not None:
                raise BasketError(
                    TERM_CONFLICT,
                    "another term already covers this sku/coupon pair",
                    {"sku": sku, "coupon_id": coupon_id, "existing_term": clash["term_id"]},
                )
            conn.execute(
                "INSERT INTO warranty_terms (term_id, sku, base_days, coupon_id, bonus_days, content_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (term_id, sku, base_days, coupon_id, bonus_days, canonical_json(content), utc_now()),
            )
        return deep_copy(content)

    def get_term(self, term_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT content_json FROM warranty_terms WHERE term_id = ?", (term_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_TERM, "unknown warranty term", {"term_id": term_id})
        return json_loads(row["content_json"])

    def list_terms(self, sku: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            if sku is None:
                rows = conn.execute("SELECT content_json FROM warranty_terms ORDER BY sku, coupon_id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT content_json FROM warranty_terms WHERE sku = ? ORDER BY coupon_id", (sku,)
                ).fetchall()
        return [json_loads(r["content_json"]) for r in rows]

    # -------------------------------------------------------- enrollment ---
    def enroll_sale(self, sale_id: str, sold_day: Any, *, term_map: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
        """Create one warranty per physical unit of a registered sale.

        ``term_map`` optionally pins ``sku -> base term_id``; otherwise the
        unique base term (``coupon_id IS NULL``) for each unit's SKU is used.
        A unit carrying a ``coupon_id`` in the bound sale only earns that
        coupon's addendum when a matching term exists; an unknown coupon term
        is an explicit error rather than a silently dropped benefit.
        """

        sale_id = require_id(sale_id, INVALID_INPUT, "sale_id")
        sold_day = _require_day(sold_day, "sold_day")
        term_map = dict(term_map or {})
        with self.db.transaction() as conn:
            sale = conn.execute("SELECT sale_id FROM sales WHERE sale_id = ?", (sale_id,)).fetchone()
            if sale is None:
                raise BasketError(UNKNOWN_SALE, "cannot enroll warranty for an unknown sale", {"sale_id": sale_id})
            units = conn.execute(
                "SELECT unit_id, sku, coupon_id FROM sale_units WHERE sale_id = ? ORDER BY unit_id", (sale_id,)
            ).fetchall()
            existing = conn.execute(
                "SELECT unit_id FROM warranties WHERE sale_id = ? ORDER BY unit_id", (sale_id,)
            ).fetchall()
            if existing:
                enrolled = [r["unit_id"] for r in existing]
                # Idempotent replay only when every unit is already enrolled
                # under the same sale day; a partial/tampered replay conflicts.
                if [u["unit_id"] for u in units] == enrolled:
                    return self._warranty_rows(conn, sale_id)
                raise BasketError(
                    WARRANTY_CONFLICT,
                    "sale already has a different warranty enrollment",
                    {"sale_id": sale_id, "enrolled": enrolled},
                )
            created: List[Dict[str, Any]] = []
            for unit in units:
                sku = unit["sku"]
                base_term_id = term_map.get(sku)
                base_term = self._load_base_term(conn, sku, base_term_id)
                bonus_days = 0
                coupon_id = unit["coupon_id"]
                if coupon_id is not None:
                    addendum = conn.execute(
                        "SELECT content_json FROM warranty_terms WHERE sku = ? AND coupon_id = ?",
                        (sku, coupon_id),
                    ).fetchone()
                    if addendum is None:
                        raise BasketError(
                            UNKNOWN_COUPON_TERM,
                            "unit received a coupon at deal time but no warranty addendum is published for it",
                            {"sale_id": sale_id, "unit_id": unit["unit_id"], "coupon_id": coupon_id, "sku": sku},
                        )
                    bonus_days = json_loads(addendum["content_json"])["bonus_days"]
                warranty_days = base_term["base_days"] + bonus_days
                conn.execute(
                    """
                    INSERT INTO warranties
                        (sale_id, unit_id, sku, sold_day, term_id, base_days, coupon_id, bonus_days,
                         warranty_days, current_version, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        sale_id,
                        unit["unit_id"],
                        sku,
                        sold_day,
                        base_term["term_id"],
                        base_term["base_days"],
                        coupon_id,
                        bonus_days,
                        warranty_days,
                        utc_now(),
                    ),
                )
                record_ids = []
                self._insert_version(conn, sale_id, unit["unit_id"], version=1, based_on=record_ids)
                created.append(self._load_warranty(conn, sale_id, unit["unit_id"]))
            return created

    def _load_base_term(self, conn, sku: str, term_id: Optional[str]) -> Dict[str, Any]:
        if term_id is not None:
            row = conn.execute("SELECT content_json FROM warranty_terms WHERE term_id = ?", (term_id,)).fetchone()
            if row is None:
                raise BasketError(UNKNOWN_TERM, "unknown warranty term", {"term_id": term_id})
            term = json_loads(row["content_json"])
            if term["sku"] != sku or term["coupon_id"] is not None:
                raise BasketError(
                    TERM_CONFLICT,
                    "pinned term must be the base (coupon-less) term for the unit's sku",
                    {"term_id": term_id, "sku": sku},
                )
            return term
        row = conn.execute(
            "SELECT content_json FROM warranty_terms WHERE sku = ? AND coupon_id IS NULL", (sku,)
        ).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_TERM, "no base warranty term published for sku", {"sku": sku})
        return json_loads(row["content_json"])

    def get_warranty(self, sale_id: str, unit_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            self._require_warranty(conn, sale_id, unit_id)
            return self._load_warranty(conn, sale_id, unit_id)

    def list_warranties(self, sale_id: str) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            if conn.execute("SELECT 1 FROM sales WHERE sale_id = ?", (sale_id,)).fetchone() is None:
                raise BasketError(UNKNOWN_SALE, "unknown sale", {"sale_id": sale_id})
            return self._warranty_rows(conn, sale_id)

    # ------------------------------------------------------------- outages --
    def register_outage(
        self,
        record_id: str,
        sale_id: str,
        unit_id: str,
        start_day: Any,
        end_day: Any,
        *,
        registered_day: Optional[int] = None,
        note: str = "",
    ) -> Dict[str, Any]:
        """Register a confirmed factory outage; back-dated records are allowed.

        The record's business interval drives the clock.  ``registered_day``
        defaults to ``end_day - 1`` (reported on release) but may be any later
        day to model a late report; it never changes which business days pause.
        """

        record_id = require_id(record_id, INVALID_INPUT, "record_id")
        sale_id = require_id(sale_id, INVALID_INPUT, "sale_id")
        unit_id = require_id(unit_id, INVALID_INPUT, "unit_id")
        start_day = _require_day(start_day, "start_day")
        end_day = _require_day(end_day, "end_day")
        if start_day >= end_day:
            raise BasketError(
                INVALID_INTERVAL,
                "outage must be a half-open [start_day, end_day) interval with start < end",
                {"start_day": start_day, "end_day": end_day},
            )
        if not isinstance(note, str):
            raise BasketError(INVALID_INPUT, "note must be a string")
        with self.db.transaction() as conn:
            self._require_warranty(conn, sale_id, unit_id)
            if registered_day is None:
                registered_day = end_day - 1
            registered_day = _require_day(registered_day, "registered_day")
            existing = conn.execute(
                "SELECT * FROM outage_records WHERE record_id = ?", (record_id,)
            ).fetchone()
            if existing is not None:
                same = (
                    existing["sale_id"] == sale_id
                    and existing["unit_id"] == unit_id
                    and existing["start_day"] == start_day
                    and existing["end_day"] == end_day
                )
                if not same:
                    raise BasketError(
                        RECORD_CONFLICT,
                        "record_id reused for a different outage",
                        {"record_id": record_id},
                    )
                return self._load_warranty(conn, sale_id, unit_id)
            duplicate = conn.execute(
                "SELECT record_id FROM outage_records WHERE sale_id = ? AND unit_id = ? AND start_day = ? AND end_day = ?",
                (sale_id, unit_id, start_day, end_day),
            ).fetchone()
            if duplicate is not None:
                raise BasketError(
                    RECORD_CONFLICT,
                    "identical outage interval already recorded",
                    {"record_id": duplicate["record_id"]},
                )
            seq_row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM outage_records").fetchone()
            conn.execute(
                """
                INSERT INTO outage_records
                    (record_id, sale_id, unit_id, start_day, end_day, registered_day, note, seq, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (record_id, sale_id, unit_id, start_day, end_day, registered_day, note, seq_row["next"], utc_now()),
            )
            self._recompute(conn, sale_id, unit_id, trigger_record=record_id)
            return self._load_warranty(conn, sale_id, unit_id)

    def get_outage(self, record_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM outage_records WHERE record_id = ?", (record_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_RECORD, "unknown outage record", {"record_id": record_id})
        return {
            "record_id": row["record_id"],
            "sale_id": row["sale_id"],
            "unit_id": row["unit_id"],
            "start_day": row["start_day"],
            "end_day": row["end_day"],
            "registered_day": row["registered_day"],
            "note": row["note"],
            "seq": row["seq"],
        }

    def list_outages(self, sale_id: str, unit_id: str) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            self._require_warranty(conn, sale_id, unit_id)
            rows = conn.execute(
                "SELECT * FROM outage_records WHERE sale_id = ? AND unit_id = ? ORDER BY start_day, end_day, seq",
                (sale_id, unit_id),
            ).fetchall()
        return [
            {
                "record_id": r["record_id"],
                "start_day": r["start_day"],
                "end_day": r["end_day"],
                "registered_day": r["registered_day"],
                "note": r["note"],
                "seq": r["seq"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------- returns --
    def mark_return_exit(self, sale_id: str, unit_id: str, return_day: Any, return_request_id: str) -> Dict[str, Any]:
        """Record that a normal return removes the unit from cover on a business day."""

        sale_id = require_id(sale_id, INVALID_INPUT, "sale_id")
        unit_id = require_id(unit_id, INVALID_INPUT, "unit_id")
        return_day = _require_day(return_day, "return_day")
        return_request_id = require_id(return_request_id, INVALID_INPUT, "return_request_id")
        with self.db.transaction() as conn:
            self._require_warranty(conn, sale_id, unit_id)
            returned = conn.execute(
                "SELECT return_request_id FROM sale_units WHERE sale_id = ? AND unit_id = ? AND returned = 1",
                (sale_id, unit_id),
            ).fetchone()
            if returned is None:
                raise BasketError(
                    RETURN_CONFLICT,
                    "warranty exit requires the unit to be returned in the ledger first",
                    {"sale_id": sale_id, "unit_id": unit_id},
                )
            if returned["return_request_id"] != return_request_id:
                raise BasketError(
                    RETURN_CONFLICT,
                    "return_request_id does not match the ledger's return record",
                    {"expected": returned["return_request_id"], "got": return_request_id},
                )
            existing = conn.execute(
                "SELECT * FROM warranty_exits WHERE sale_id = ? AND unit_id = ?", (sale_id, unit_id)
            ).fetchone()
            if existing is not None:
                if existing["exit_day"] != return_day or existing["return_request_id"] != return_request_id:
                    raise BasketError(RETURN_CONFLICT, "warranty exit already recorded differently", {"record_id": existing["return_request_id"]})
                return self._load_warranty(conn, sale_id, unit_id)
            conn.execute(
                "INSERT INTO warranty_exits (sale_id, unit_id, exit_day, return_request_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (sale_id, unit_id, return_day, return_request_id, utc_now()),
            )
            self._recompute(conn, sale_id, unit_id, trigger_record=f"return:{return_request_id}")
            return self._load_warranty(conn, sale_id, unit_id)

    # --------------------------------------------------------- desk query ---
    def query_warranty(self, query_id: str, sale_id: str, unit_id: str, query_day: Any) -> Dict[str, Any]:
        """Service-desk query: is this unit covered on this business day?

        The answer is persisted against the warranty *version* that produced
        it.  Later back-dated events create newer versions but this record
        keeps showing the customer exactly what they were told and why.
        """

        query_id = require_id(query_id, INVALID_INPUT, "query_id")
        query_day = _require_day(query_day, "query_day")
        with self.db.transaction() as conn:
            self._require_warranty(conn, sale_id, unit_id)
            existing = conn.execute("SELECT result_json FROM warranty_queries WHERE query_id = ?", (query_id,)).fetchone()
            if existing is not None:
                return json_loads(existing["result_json"])
            warranty = self._load_warranty(conn, sale_id, unit_id)
            summary = self._summary(conn, sale_id, unit_id)
            answer = eligibility(summary, query_day)
            outages = [
                Outage(r["start_day"], r["end_day"], r["record_id"])
                for r in conn.execute(
                    "SELECT * FROM outage_records WHERE sale_id = ? AND unit_id = ? ORDER BY start_day, end_day",
                    (sale_id, unit_id),
                ).fetchall()
            ]
            result = {
                "ok": True,
                "query_id": query_id,
                "sale_id": sale_id,
                "unit_id": unit_id,
                "sku": warranty["sku"],
                "query_day": query_day,
                "covered": answer["covered"],
                "reason": answer["reason"],
                "warranty_version": warranty["current_version"],
                "sold_day": warranty["sold_day"],
                "term": {
                    "term_id": warranty["term_id"],
                    "base_days": warranty["base_days"],
                    "coupon_id": warranty["coupon_id"],
                    "bonus_days": warranty["bonus_days"],
                    "warranty_days": warranty["warranty_days"],
                },
                "paused_days": summary["paused_days"],
                "ineffectual_outage_days": summary["ineffectual_outage_days"],
                "expiry_day": summary["expiry_day"],
                "cover_until_day": summary["cover_until_day"],
                "latest_start_day": summary["latest_start_day"],
                "end_reason": summary["end_reason"],
                "outages": explain_outages(outages, warranty["sold_day"], summary["expiry_day"]),
            }
            conn.execute(
                "INSERT INTO warranty_queries (query_id, sale_id, unit_id, query_day, warranty_version, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (query_id, sale_id, unit_id, query_day, warranty["current_version"], canonical_json(result), utc_now()),
            )
            return result

    def get_query(self, query_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT result_json FROM warranty_queries WHERE query_id = ?", (query_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_RECORD, "unknown warranty query", {"query_id": query_id})
        return json_loads(row["result_json"])

    def versions(self, sale_id: str, unit_id: str) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            self._require_warranty(conn, sale_id, unit_id)
            rows = conn.execute(
                "SELECT * FROM warranty_versions WHERE sale_id = ? AND unit_id = ? ORDER BY version",
                (sale_id, unit_id),
            ).fetchall()
        return [
            {
                "version": r["version"],
                "warranty_days": r["warranty_days"],
                "paused_days": r["paused_days"],
                "expiry_day": r["expiry_day"],
                "latest_start_day": r["latest_start_day"],
                "cover_until_day": r["cover_until_day"],
                "end_reason": r["end_reason"],
                "based_on_records": json_loads(r["based_on_records"]),
            }
            for r in rows
        ]

    def get_version_summary(self, sale_id: str, unit_id: str, version: int) -> Dict[str, Any]:
        """Reconstruct the clock summary exactly as a pinned historical version saw it."""

        if not is_int(version) or version < 1:
            raise BasketError(INVALID_INPUT, "version must be a positive integer", {"version": version})
        with self.db.connection() as conn:
            self._require_warranty(conn, sale_id, unit_id)
            row = conn.execute(
                "SELECT * FROM warranty_versions WHERE sale_id = ? AND unit_id = ? AND version = ?",
                (sale_id, unit_id, version),
            ).fetchone()
            if row is None:
                raise BasketError(UNKNOWN_RECORD, "unknown warranty version", {"version": version})
            record_ids = set(json_loads(row["based_on_records"]))
            outages = [
                Outage(r["start_day"], r["end_day"], r["record_id"])
                for r in conn.execute(
                    "SELECT * FROM outage_records WHERE sale_id = ? AND unit_id = ? ORDER BY start_day, end_day",
                    (sale_id, unit_id),
                ).fetchall()
                if r["record_id"] in record_ids
            ]
            exit_row = conn.execute(
                "SELECT exit_day, return_request_id FROM warranty_exits WHERE sale_id = ? AND unit_id = ?",
                (sale_id, unit_id),
            ).fetchone()
            return_day = None
            if exit_row is not None and f"return:{exit_row['return_request_id']}" in record_ids:
                return_day = exit_row["exit_day"]
            sold_day = conn.execute(
                "SELECT sold_day FROM warranties WHERE sale_id = ? AND unit_id = ?", (sale_id, unit_id)
            ).fetchone()["sold_day"]
            summary = compute_warranty(
                sold_day=sold_day,
                warranty_days=row["warranty_days"],
                outages=outages,
                return_day=return_day,
            )
            stored = {
                "version": version,
                "paused_days": row["paused_days"],
                "expiry_day": row["expiry_day"],
                "latest_start_day": row["latest_start_day"],
                "cover_until_day": row["cover_until_day"],
                "end_reason": row["end_reason"],
            }
            if (
                summary["paused_days"] != stored["paused_days"]
                or summary["expiry_day"] != stored["expiry_day"]
                or summary["end_reason"] != stored["end_reason"]
            ):  # pragma: no cover - defensive history integrity guard
                raise BasketError(WARRANTY_CONFLICT, "stored version cannot be reconstructed identically", stored)
            summary["version"] = version
            summary["based_on_records"] = sorted(record_ids)
            return summary

    # ------------------------------------------------------------- helpers --
    def _require_warranty(self, conn, sale_id: str, unit_id: str) -> None:
        row = conn.execute(
            "SELECT 1 FROM warranties WHERE sale_id = ? AND unit_id = ?", (sale_id, unit_id)
        ).fetchone()
        if row is None:
            raise BasketError(
                UNKNOWN_WARRANTY,
                "no warranty enrolled for this sale/unit",
                {"sale_id": sale_id, "unit_id": unit_id},
            )

    def _load_warranty(self, conn, sale_id: str, unit_id: str) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM warranties WHERE sale_id = ? AND unit_id = ?", (sale_id, unit_id)).fetchone()
        summary_row = conn.execute(
            "SELECT * FROM warranty_versions WHERE sale_id = ? AND unit_id = ? AND version = ?",
            (sale_id, unit_id, row["current_version"]),
        ).fetchone()
        return {
            "sale_id": sale_id,
            "unit_id": unit_id,
            "sku": row["sku"],
            "sold_day": row["sold_day"],
            "term_id": row["term_id"],
            "base_days": row["base_days"],
            "coupon_id": row["coupon_id"],
            "bonus_days": row["bonus_days"],
            "warranty_days": row["warranty_days"],
            "current_version": row["current_version"],
            "paused_days": summary_row["paused_days"],
            "expiry_day": summary_row["expiry_day"],
            "cover_until_day": summary_row["cover_until_day"],
            "latest_start_day": summary_row["latest_start_day"],
            "end_reason": summary_row["end_reason"],
        }

    def _warranty_rows(self, conn, sale_id: str) -> List[Dict[str, Any]]:
        units = conn.execute(
            "SELECT unit_id FROM warranties WHERE sale_id = ? ORDER BY unit_id", (sale_id,)
        ).fetchall()
        return [self._load_warranty(conn, sale_id, r["unit_id"]) for r in units]

    def _summary(self, conn, sale_id: str, unit_id: str) -> Dict[str, Any]:
        warranty = self._load_warranty(conn, sale_id, unit_id)
        outages = [
            Outage(r["start_day"], r["end_day"], r["record_id"])
            for r in conn.execute(
                "SELECT * FROM outage_records WHERE sale_id = ? AND unit_id = ? ORDER BY start_day, end_day",
                (sale_id, unit_id),
            ).fetchall()
        ]
        exit_row = conn.execute(
            "SELECT exit_day FROM warranty_exits WHERE sale_id = ? AND unit_id = ?", (sale_id, unit_id)
        ).fetchone()
        return compute_warranty(
            sold_day=warranty["sold_day"],
            warranty_days=warranty["warranty_days"],
            outages=outages,
            return_day=exit_row["exit_day"] if exit_row is not None else None,
        )

    def _recompute(self, conn, sale_id: str, unit_id: str, *, trigger_record: str) -> None:
        warranty = self._load_warranty(conn, sale_id, unit_id)
        record_rows = conn.execute(
            "SELECT record_id FROM outage_records WHERE sale_id = ? AND unit_id = ? ORDER BY seq",
            (sale_id, unit_id),
        ).fetchall()
        based_on = [r["record_id"] for r in record_rows]
        exit_row = conn.execute(
            "SELECT exit_day, return_request_id FROM warranty_exits WHERE sale_id = ? AND unit_id = ?",
            (sale_id, unit_id),
        ).fetchone()
        if exit_row is not None:
            based_on.append(f"return:{exit_row['return_request_id']}")
        summary = self._summary(conn, sale_id, unit_id)
        next_version = warranty["current_version"] + 1
        conn.execute(
            "UPDATE warranties SET current_version = ? WHERE sale_id = ? AND unit_id = ?",
            (next_version, sale_id, unit_id),
        )
        self._insert_version(conn, sale_id, unit_id, version=next_version, based_on=based_on, summary=summary)

    def _insert_version(
        self,
        conn,
        sale_id: str,
        unit_id: str,
        *,
        version: int,
        based_on: Sequence[str],
        summary: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if summary is None:
            warranty = conn.execute(
                "SELECT * FROM warranties WHERE sale_id = ? AND unit_id = ?", (sale_id, unit_id)
            ).fetchone()
            summary = compute_warranty(
                sold_day=warranty["sold_day"],
                warranty_days=warranty["warranty_days"],
                outages=[],
            )
        conn.execute(
            """
            INSERT INTO warranty_versions
                (sale_id, unit_id, version, warranty_days, paused_days, expiry_day, latest_start_day,
                 cover_until_day, end_reason, based_on_records, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sale_id,
                unit_id,
                version,
                summary["warranty_days"],
                summary["paused_days"],
                summary["expiry_day"],
                summary["latest_start_day"],
                summary["cover_until_day"],
                summary["end_reason"],
                canonical_json(sorted(based_on)),
                utc_now(),
            ),
        )
