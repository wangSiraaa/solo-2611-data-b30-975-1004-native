"""SQLite plumbing shared by the quote service and the after-sales ledger."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def json_loads(value: str) -> Any:
    return json.loads(value)


def deep_copy(value: Any) -> Any:
    return json_loads(json.dumps(value, ensure_ascii=False))


def ensure_parent(path: str) -> None:
    if path and path != ":memory:":
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)


class Database:
    """A tiny per-operation connection factory.

    Every call opens its own connection so two instances (or two threads) can
    exercise real SQLite locking.  ``BEGIN IMMEDIATE`` serialises writers; the
    busy timeout makes a competing writer wait instead of failing spuriously.
    """

    def __init__(self, path: str, busy_timeout_ms: int = 30000):
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        self._memory_lock = threading.RLock()
        self._memory_conn: Optional[sqlite3.Connection] = None
        self._memory_ready = False
        ensure_parent(path)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            self._init_schema(conn)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if self.path == ":memory:":
            with self._memory_lock:
                if self._memory_conn is None:
                    self._memory_conn = self._new_connection()
                    if not self._memory_ready:
                        self._init_schema(self._memory_conn)
                        self._memory_ready = True
                yield self._memory_conn
            return
        conn = self._new_connection()
        try:
            yield conn
        finally:
            conn.close()

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=self.busy_timeout_ms / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id TEXT PRIMARY KEY,
                seq INTEGER NOT NULL UNIQUE,
                content_json TEXT NOT NULL,
                coupons_json TEXT NOT NULL,
                products_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS quotes (
                request_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                basket_sig TEXT NOT NULL,
                basket_json TEXT NOT NULL,
                quote_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sales (
                sale_id TEXT PRIMARY KEY,
                quote_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                quote_sig TEXT NOT NULL,
                quote_json TEXT NOT NULL,
                total INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sale_units (
                sale_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                price INTEGER NOT NULL,
                coupon_id TEXT,
                discount INTEGER NOT NULL,
                paid INTEGER NOT NULL,
                returned INTEGER NOT NULL DEFAULT 0,
                return_request_id TEXT,
                grade TEXT,
                PRIMARY KEY (sale_id, unit_id),
                FOREIGN KEY (sale_id) REFERENCES sales(sale_id)
            );

            CREATE TABLE IF NOT EXISTS return_requests (
                request_id TEXT PRIMARY KEY,
                sale_id TEXT NOT NULL,
                canonical_request TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (sale_id) REFERENCES sales(sale_id)
            );

            CREATE TABLE IF NOT EXISTS return_items (
                request_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                grade TEXT NOT NULL,
                original_paid INTEGER NOT NULL,
                refund INTEGER NOT NULL,
                PRIMARY KEY (request_id, unit_id),
                FOREIGN KEY (request_id) REFERENCES return_requests(request_id)
            );

            CREATE TABLE IF NOT EXISTS adjustments (
                event_id TEXT PRIMARY KEY,
                content_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inventory_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                bucket TEXT NOT NULL CHECK (bucket IN ('RESTOCK', 'SCRAP')),
                delta INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inventory_totals (
                sku TEXT NOT NULL,
                bucket TEXT NOT NULL CHECK (bucket IN ('RESTOCK', 'SCRAP')),
                qty INTEGER NOT NULL,
                PRIMARY KEY (sku, bucket)
            );

            -- Factory-return warranty terms published per SKU, including any
            -- promotional addendum attached to a specific coupon.
            CREATE TABLE IF NOT EXISTS warranty_terms (
                term_id TEXT PRIMARY KEY,
                sku TEXT NOT NULL,
                base_days INTEGER NOT NULL,
                coupon_id TEXT,
                bonus_days INTEGER NOT NULL DEFAULT 0,
                content_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (sku, coupon_id)
            );

            -- One warranty per physical unit of a bound sale, frozen at the
            -- unit's own coupon allocation at deal time.
            CREATE TABLE IF NOT EXISTS warranties (
                sale_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                sold_day INTEGER NOT NULL,
                term_id TEXT NOT NULL,
                base_days INTEGER NOT NULL,
                coupon_id TEXT,
                bonus_days INTEGER NOT NULL,
                warranty_days INTEGER NOT NULL,
                current_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                PRIMARY KEY (sale_id, unit_id),
                FOREIGN KEY (sale_id) REFERENCES sales(sale_id)
            );

            -- Append-only factory outage records.  Business days are integer
            -- calendar days; [start_day, end_day) is half-open.  A late,
            -- back-dated record simply gets a later record_id/seq; the
            -- warranty version is recomputed from business dates.
            CREATE TABLE IF NOT EXISTS outage_records (
                record_id TEXT PRIMARY KEY,
                sale_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                start_day INTEGER NOT NULL,
                end_day INTEGER NOT NULL,
                registered_day INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                seq INTEGER NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                UNIQUE (sale_id, unit_id, start_day, end_day),
                FOREIGN KEY (sale_id, unit_id) REFERENCES warranties(sale_id, unit_id)
            );

            -- Normal returns terminate warranty on the business day the return
            -- becomes effective (the physical unit leaves after-sales cover).
            CREATE TABLE IF NOT EXISTS warranty_exits (
                sale_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                exit_day INTEGER NOT NULL,
                return_request_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (sale_id, unit_id),
                FOREIGN KEY (sale_id, unit_id) REFERENCES warranties(sale_id, unit_id)
            );

            -- Every recomputation is preserved as an immutable version so an
            -- answer previously given to a customer remains auditable forever.
            CREATE TABLE IF NOT EXISTS warranty_versions (
                sale_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                warranty_days INTEGER NOT NULL,
                paused_days INTEGER NOT NULL,
                expiry_day INTEGER NOT NULL,
                latest_start_day INTEGER,
                cover_until_day INTEGER,
                end_reason TEXT NOT NULL,
                based_on_records TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (sale_id, unit_id, version)
            );

            -- Customer-facing warranty queries are stored against the version
            -- that answered them; later recomputations never overwrite history.
            CREATE TABLE IF NOT EXISTS warranty_queries (
                query_id TEXT PRIMARY KEY,
                sale_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                query_day INTEGER NOT NULL,
                warranty_version INTEGER NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            -- Approved repair methods: factory-approved duration and the
            -- technician specialty required.
            CREATE TABLE IF NOT EXISTS repair_methods (
                method_id TEXT PRIMARY KEY,
                sku TEXT NOT NULL,
                duration_days INTEGER NOT NULL,
                specialty TEXT NOT NULL,
                content_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (sku, method_id)
            );

            CREATE TABLE IF NOT EXISTS technicians (
                technician_id TEXT PRIMARY KEY,
                specialty TEXT NOT NULL,
                content_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            -- Days a technician cannot work, stored half-open as [day, day+1).
            CREATE TABLE IF NOT EXISTS technician_unavailable (
                technician_id TEXT NOT NULL,
                start_day INTEGER NOT NULL,
                end_day INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (technician_id) REFERENCES technicians(technician_id)
            );

            CREATE TABLE IF NOT EXISTS work_orders (
                work_order_id TEXT PRIMARY KEY,
                sale_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                delivery_day INTEGER NOT NULL,
                repair_value INTEGER NOT NULL,
                method_ids_json TEXT NOT NULL,
                warranty_version INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                created_at TEXT NOT NULL,
                UNIQUE (sale_id, unit_id)
            );

            -- Persisted batch optimisation runs and the per-order decision.
            CREATE TABLE IF NOT EXISTS booking_batches (
                batch_id TEXT PRIMARY KEY,
                horizon_end_day INTEGER NOT NULL,
                total_value INTEGER NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS booking_decisions (
                batch_id TEXT NOT NULL,
                work_order_id TEXT NOT NULL,
                accepted INTEGER NOT NULL,
                start_day INTEGER,
                end_day INTEGER,
                method_id TEXT,
                technician_id TEXT,
                reason_code TEXT,
                reason_detail TEXT,
                warranty_version INTEGER NOT NULL,
                result_json TEXT NOT NULL,
                PRIMARY KEY (batch_id, work_order_id),
                FOREIGN KEY (batch_id) REFERENCES booking_batches(batch_id)
            );

            -- The actual technician calendar reservations created by accepted
            -- decisions; one technician can hold at most one unit per day.
            CREATE TABLE IF NOT EXISTS technician_bookings (
                booking_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                work_order_id TEXT NOT NULL,
                technician_id TEXT NOT NULL,
                day INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (technician_id, day)
            );
            """
        )
