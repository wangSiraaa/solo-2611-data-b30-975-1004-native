"""The old hall: a local, restartable legacy ticket service.

The old client is frozen for this season: it may send only a window number, a
hall/branch, a service day and a **stable request id**.  It never sends a
ticket id.  The server therefore decides the person on the request id, and the
same request id always yields the same ticket (or an explicit non-issued
state).  The server may be extended freely -- that is what the lookup and
intake operations here are for.

Durability contract
-------------------
``reserve`` records a ``pending`` request row and commits it **before** any
counter seat is taken.  The counter seat and the ``issued`` result are then
written in one separate transaction.  If the process dies after taking the
seat but before the caller gets a reply, a retry with the same request id finds
the committed ``issued`` row and returns the original ticket number; nothing is
issued twice.  A request that never got a seat stays ``pending`` and is
reported as ``in_progress`` -- never a fake ticket.

Lookups never draw a new counter seat and never consume a new day's quota.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .store import StoreError, _Transaction, _utc_now


class LegacyError(StoreError):
    """Rejected legacy-hall operation."""


LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS lh_requests (
    request_id TEXT PRIMARY KEY,
    window_id  TEXT NOT NULL,
    hall_id    TEXT NOT NULL,
    service_day TEXT NOT NULL,
    ticket_id  TEXT NOT NULL,
    status     TEXT NOT NULL,        -- pending | issued | awaiting
    ticket_no  TEXT,
    error      TEXT,
    created_at TEXT NOT NULL,
    issued_at  TEXT
);
CREATE TABLE IF NOT EXISTS lh_day (
    hall_id     TEXT NOT NULL,
    service_day TEXT NOT NULL,
    capacity    INTEGER NOT NULL,
    issued      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hall_id, service_day)
);
CREATE TABLE IF NOT EXISTS lh_tickets (
    hall_id     TEXT NOT NULL,
    service_day TEXT NOT NULL,
    ticket_no   TEXT NOT NULL,
    ticket_id   TEXT NOT NULL,
    window_id   TEXT NOT NULL,
    request_id  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (hall_id, service_day, ticket_no)
);
CREATE UNIQUE INDEX IF NOT EXISTS lh_person_day
    ON lh_tickets (ticket_id, service_day);
CREATE TABLE IF NOT EXISTS lh_imports (
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    hall_id     TEXT NOT NULL,
    service_day TEXT NOT NULL,
    ticket_no   TEXT NOT NULL,
    ticket_id   TEXT NOT NULL,
    window_id   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);
"""


class LegacyHall:
    """A single old-hall database with its own quota and request log."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(LEGACY_SCHEMA)

    @classmethod
    def open(cls, path: str) -> "LegacyHall":
        conn = sqlite3.connect(
            path, timeout=30.0, isolation_level=None, check_same_thread=False
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        return cls(conn)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "LegacyHall":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ day
    def open_day(self, hall_id: str, service_day: str, capacity: int) -> dict[str, Any]:
        with _Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO lh_day(hall_id, service_day, capacity, issued) "
                "VALUES(?,?,?,0) ON CONFLICT(hall_id, service_day) DO UPDATE "
                "SET capacity=excluded.capacity",
                (hall_id, service_day, capacity),
            )
        return self.day_status(hall_id, service_day)

    def day_status(self, hall_id: str, service_day: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM lh_day WHERE hall_id=? AND service_day=?",
            (hall_id, service_day),
        ).fetchone()
        if row is None:
            return {"hall_id": hall_id, "service_day": service_day,
                    "capacity": 0, "issued": 0, "remaining": 0, "opened": False}
        return {
            "hall_id": hall_id,
            "service_day": service_day,
            "capacity": int(row["capacity"]),
            "issued": int(row["issued"]),
            "remaining": max(int(row["capacity"]) - int(row["issued"]), 0),
            "opened": True,
        }

    # -------------------------------------------------------------- contract
    def _row(self, request_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM lh_requests WHERE request_id=?", (request_id,)
        ).fetchone()

    def reserve(
        self,
        *,
        request_id: str,
        window_id: str,
        hall_id: str,
        service_day: str,
        ticket_id: str,
        capacity: int | None = None,
    ) -> dict[str, Any]:
        """Issue once per request id; safe to retry after a lost reply."""

        row = self._row(request_id)
        if row is not None:
            return self._report(row)
        if not ticket_id:
            raise LegacyError("ticket_id is required")
        with _Transaction(self.conn):
            existing = self._row(request_id)
            if existing is not None:
                return self._report(existing)
            day = self.conn.execute(
                "SELECT * FROM lh_day WHERE hall_id=? AND service_day=?",
                (hall_id, service_day),
            ).fetchone()
            if day is None:
                if capacity is None:
                    raise LegacyError("the hall day is not open and no capacity given")
                self.conn.execute(
                    "INSERT INTO lh_day(hall_id, service_day, capacity, issued) "
                    "VALUES(?,?,?,0)",
                    (hall_id, service_day, capacity),
                )
                day = self.conn.execute(
                    "SELECT * FROM lh_day WHERE hall_id=? AND service_day=?",
                    (hall_id, service_day),
                ).fetchone()
            # Phase 1: durable pending marker (survives a crash mid-issue).
            self.conn.execute(
                "INSERT INTO lh_requests(request_id, window_id, hall_id, service_day, "
                "ticket_id, status, created_at) VALUES(?,?,?,?,?, 'pending', ?)",
                (request_id, window_id, hall_id, service_day, ticket_id, _utc_now()),
            )
            conflict = self.conn.execute(
                "SELECT 1 FROM lh_tickets WHERE ticket_id=? AND service_day=? "
                "UNION ALL SELECT 1 FROM lh_imports WHERE ticket_id=? AND service_day=?",
                (ticket_id, service_day, ticket_id, service_day),
            ).fetchone()
            if conflict is not None:
                self.conn.execute(
                    "UPDATE lh_requests SET status='awaiting', "
                    "error='person already holds a ticket today' WHERE request_id=?",
                    (request_id,),
                )
        if conflict is not None:
            return self._report(self._row(request_id))
        if int(day["issued"]) >= int(day["capacity"]):
            with _Transaction(self.conn):
                self.conn.execute(
                    "UPDATE lh_requests SET status='awaiting', "
                    "error='no ticket remains today' WHERE request_id=?",
                    (request_id,),
                )
            return self._report(self._row(request_id))
        # Phase 2: commit the seat and the issued result together.
        with _Transaction(self.conn):
            day = self.conn.execute(
                "SELECT * FROM lh_day WHERE hall_id=? AND service_day=?",
                (hall_id, service_day),
            ).fetchone()
            if int(day["issued"]) >= int(day["capacity"]):
                self.conn.execute(
                    "UPDATE lh_requests SET status='awaiting', "
                    "error='no ticket remains today' WHERE request_id=?",
                    (request_id,),
                )
            else:
                ticket_no = str(int(day["issued"]) + 1)
                self.conn.execute(
                    "INSERT INTO lh_tickets(hall_id, service_day, ticket_no, ticket_id, "
                    "window_id, request_id, created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        hall_id,
                        service_day,
                        ticket_no,
                        ticket_id,
                        window_id,
                        request_id,
                        _utc_now(),
                    ),
                )
                self.conn.execute(
                    "UPDATE lh_day SET issued=issued+1 WHERE hall_id=? AND service_day=?",
                    (hall_id, service_day),
                )
                self.conn.execute(
                    "UPDATE lh_requests SET status='issued', ticket_no=?, issued_at=? "
                    "WHERE request_id=?",
                    (ticket_no, _utc_now(), request_id),
                )
        return self._report(self._row(request_id))

    def lookup(self, request_id: str) -> dict[str, Any]:
        """Query by the original request id.  Never allocates a new seat."""

        row = self._row(request_id)
        if row is None:
            return {"request_id": request_id, "status": "unknown",
                    "error": "no such request"}
        return self._report(row)

    def locate_person(self, ticket_id: str, service_day: str) -> dict[str, Any]:
        """Server-side lookup used by the new halls during handover.

        Searches the full old request log, so a request whose reply was lost is
        still found.  Does not touch quota and does not allocate anything.
        """

        issued = self.conn.execute(
            "SELECT * FROM lh_requests WHERE ticket_id=? AND service_day=? "
            "AND status='issued' ORDER BY created_at LIMIT 1",
            (ticket_id, service_day),
        ).fetchone()
        if issued is not None:
            return {
                "status": "issued",
                "ticket_id": ticket_id,
                "ticket_no": issued["ticket_no"],
                "window_id": issued["window_id"],
                "hall_id": issued["hall_id"],
                "service_day": issued["service_day"],
                "request_id": issued["request_id"],
            }
        pending = self.conn.execute(
            "SELECT * FROM lh_requests WHERE ticket_id=? AND service_day=? "
            "AND status='pending' ORDER BY created_at LIMIT 1",
            (ticket_id, service_day),
        ).fetchone()
        if pending is not None:
            return {
                "status": "in_progress",
                "ticket_id": ticket_id,
                "request_id": pending["request_id"],
                "service_day": service_day,
            }
        awaiting = self.conn.execute(
            "SELECT * FROM lh_requests WHERE ticket_id=? AND service_day=? "
            "AND status='awaiting' ORDER BY created_at LIMIT 1",
            (ticket_id, service_day),
        ).fetchone()
        if awaiting is not None:
            return {
                "status": "awaiting",
                "ticket_id": ticket_id,
                "request_id": awaiting["request_id"],
                "error": awaiting["error"],
                "service_day": service_day,
            }
        return {"status": "none", "ticket_id": ticket_id, "service_day": service_day}

    def import_ticket(
        self,
        *,
        source: str,
        external_id: str,
        hall_id: str,
        service_day: str,
        ticket_no: str,
        ticket_id: str,
        window_id: str,
        capacity: int | None = None,
    ) -> dict[str, Any]:
        """Record a ticket issued by another system (new hall) so this old
        hall will not hand the same person a second ticket today.

        Idempotent on ``(source, external_id)`` and never consumes quota.
        """

        existing = self.conn.execute(
            "SELECT * FROM lh_imports WHERE source=? AND external_id=?",
            (source, external_id),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        held = self.conn.execute(
            "SELECT * FROM lh_tickets WHERE ticket_id=? AND service_day=?",
            (ticket_id, service_day),
        ).fetchone()
        if held is not None:
            return {
                "source": source,
                "external_id": external_id,
                "hall_id": hall_id,
                "service_day": service_day,
                "ticket_no": held["ticket_no"],
                "ticket_id": ticket_id,
                "window_id": held["window_id"],
                "duplicate": True,
            }
        with _Transaction(self.conn):
            try:
                self.conn.execute(
                    "INSERT INTO lh_imports(source, external_id, hall_id, service_day, "
                    "ticket_no, ticket_id, window_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        source,
                        external_id,
                        hall_id,
                        service_day,
                        ticket_no,
                        ticket_id,
                        window_id,
                        _utc_now(),
                    ),
                )
            except sqlite3.IntegrityError:
                pass
        row = self.conn.execute(
            "SELECT * FROM lh_imports WHERE source=? AND external_id=?",
            (source, external_id),
        ).fetchone()
        return dict(row) if row else {
            "source": source, "external_id": external_id, "duplicate": True
        }

    def tickets_for(self, hall_id: str, service_day: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM lh_tickets WHERE hall_id=? AND service_day=? "
            "ORDER BY CAST(ticket_no AS INTEGER)",
            (hall_id, service_day),
        ).fetchall()
        return [dict(r) for r in rows]

    def requests_for(self, hall_id: str, service_day: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM lh_requests WHERE hall_id=? AND service_day=? "
            "ORDER BY created_at",
            (hall_id, service_day),
        ).fetchall()
        return [dict(r) for r in rows]

    def _report(self, row: sqlite3.Row) -> dict[str, Any]:
        status = row["status"]
        report: dict[str, Any] = {
            "request_id": row["request_id"],
            "status": status,
            "window_id": row["window_id"],
            "hall_id": row["hall_id"],
            "service_day": row["service_day"],
            "ticket_id": row["ticket_id"],
        }
        if status == "issued":
            report["ticket_no"] = row["ticket_no"]
            report["issued_at"] = row["issued_at"]
        elif status == "pending":
            report["error"] = "still being processed"
        else:
            report["error"] = row["error"]
        return report
