"""Reservation headquarters: qualification cuts, waiting books, hall issuance.

This module layers the reservation business on top of :class:`cs.store.Store`.
The store already gives atomic committed cuts; here each *submitted* cut is
translated into a waiting projection per branch, and immutable book openings /
supervisor gate events can override a queue without rewriting history.

Two independent databases cooperate during handover:

* this reservation database (``new`` system: qualification + every new hall),
* the old hall's database (:mod:`cs.legacy`), reached only through its request
  contract and a server-side lookup.

They are never put in one transaction.  A hall that needs to know whether a
person already holds a ticket today asks the old hall service-read side; the
answer is imported row by row, idempotently, so a crash leaves a retryable,
consistent state.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import reservation
from .store import (
    Store,
    StoreError,
    _Transaction,
    _json_dump,
    _utc_now,
)


class ReservationError(StoreError):
    """Rejected reservation operation."""


RESERVATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS rs_branches (
    branch_id TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    open      INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS rs_branch_classes (
    branch_id TEXT NOT NULL,
    class_id  TEXT NOT NULL,
    PRIMARY KEY (branch_id, class_id)
);
CREATE TABLE IF NOT EXISTS rs_seed_order (
    ticket_id TEXT PRIMARY KEY,
    ord       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rs_cuts (
    area_seq          INTEGER PRIMARY KEY,
    cut_id            TEXT NOT NULL UNIQUE,
    request_id        TEXT UNIQUE,
    at                TEXT NOT NULL,
    catalog_revision  TEXT NOT NULL,
    fact_revision     TEXT NOT NULL,
    fact_watermark    INTEGER NOT NULL,
    kind              TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rs_gates (
    area_seq      INTEGER PRIMARY KEY,
    branch_id     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    at            TEXT NOT NULL,
    ticket_id     TEXT,
    basis         TEXT,
    detail        TEXT NOT NULL,
    justification TEXT,
    request_id    TEXT UNIQUE,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rs_gates_branch ON rs_gates (branch_id, area_seq);
CREATE TABLE IF NOT EXISTS rs_snapshots (
    branch_id   TEXT NOT NULL,
    area_seq    INTEGER NOT NULL,
    cut_id      TEXT NOT NULL,
    result      TEXT NOT NULL,
    PRIMARY KEY (branch_id, area_seq)
);
CREATE TABLE IF NOT EXISTS rs_live (
    branch_id TEXT PRIMARY KEY,
    area_seq  INTEGER NOT NULL,
    result    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rs_books (
    book_id    TEXT PRIMARY KEY,
    branch_id  TEXT NOT NULL,
    cut_id     TEXT NOT NULL,
    area_seq   INTEGER NOT NULL,
    opened_by  TEXT NOT NULL,
    at         TEXT NOT NULL,
    frozen     TEXT NOT NULL,
    status     TEXT NOT NULL,
    request_id TEXT UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rs_bookings (
    book_id    TEXT NOT NULL,
    ticket_id  TEXT NOT NULL,
    sequence   INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (book_id, ticket_id)
);
CREATE TABLE IF NOT EXISTS rs_windows (
    window_id TEXT PRIMARY KEY,
    hall_id   TEXT NOT NULL,
    branch_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rs_day_quota (
    branch_id   TEXT NOT NULL,
    service_day TEXT NOT NULL,
    capacity    INTEGER NOT NULL,
    issued      INTEGER NOT NULL DEFAULT 0,
    closed      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (branch_id, service_day)
);
CREATE TABLE IF NOT EXISTS rs_tickets (
    ticket_no    TEXT NOT NULL,
    branch_id    TEXT NOT NULL,
    service_day  TEXT NOT NULL,
    ticket_id    TEXT NOT NULL,
    window_id    TEXT NOT NULL,
    source       TEXT NOT NULL,
    external_id  TEXT,
    request_id   TEXT,
    state        TEXT NOT NULL DEFAULT 'issued',
    created_at   TEXT NOT NULL,
    PRIMARY KEY (branch_id, service_day, ticket_no)
);
CREATE UNIQUE INDEX IF NOT EXISTS rs_ticket_person_day
    ON rs_tickets (ticket_id, service_day);
CREATE UNIQUE INDEX IF NOT EXISTS rs_ticket_external
    ON rs_tickets (source, external_id) WHERE external_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS rs_requests (
    request_id   TEXT PRIMARY KEY,
    window_id    TEXT NOT NULL,
    branch_id    TEXT NOT NULL,
    service_day  TEXT NOT NULL,
    ticket_id    TEXT,
    status       TEXT NOT NULL,
    result       TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL
);
"""


def _shift(seq: int) -> str:
    return f"S{seq:06d}"


class ReservationStore(Store):
    """Qualification books and hall tickets over one reservation database."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        super().__init__(connection)
        self.conn.executescript(RESERVATION_SCHEMA)

    @classmethod
    def open_reservation(cls, path: str) -> "ReservationStore":
        conn = sqlite3.connect(
            path, timeout=30.0, isolation_level=None, check_same_thread=False
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return cls(conn)

    # --------------------------------------------------------------- branches
    def add_branch(
        self, branch_id: str, classes: Sequence[str], *, name: str | None = None
    ) -> dict[str, Any]:
        if not branch_id:
            raise ReservationError("branch_id must be non-empty")
        classes = sorted(set(classes))
        if not classes:
            raise ReservationError("a branch must manage at least one class")
        if not self.is_initialized():
            raise ReservationError("create the catalog before branches")
        state = self.load_catalog_state()
        for cls in classes:
            if cls not in state.nodes:
                raise ReservationError(f"branch {branch_id}: unknown class {cls!r}")
        with self._write_lock, _Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO rs_branches(branch_id, name, open) VALUES(?,?,1) "
                "ON CONFLICT(branch_id) DO UPDATE SET name=excluded.name",
                (branch_id, name or branch_id),
            )
            self.conn.execute(
                "DELETE FROM rs_branch_classes WHERE branch_id=?", (branch_id,)
            )
            for cls in classes:
                self.conn.execute(
                    "INSERT INTO rs_branch_classes(branch_id, class_id) VALUES(?,?)",
                    (branch_id, cls),
                )
        return self.branch(branch_id)

    def set_branch_open(self, branch_id: str, is_open: bool) -> dict[str, Any]:
        self._require_branch(branch_id)
        with self._write_lock, _Transaction(self.conn):
            self.conn.execute(
                "UPDATE rs_branches SET open=? WHERE branch_id=?",
                (1 if is_open else 0, branch_id),
            )
        return self.branch(branch_id)

    def _require_branch(self, branch_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM rs_branches WHERE branch_id=?", (branch_id,)
        ).fetchone()
        if row is None:
            raise ReservationError(f"unknown branch {branch_id!r}")
        return row

    def _managed_classes(self, branch_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT class_id FROM rs_branch_classes WHERE branch_id=? ORDER BY class_id",
            (branch_id,),
        ).fetchall()
        return [r["class_id"] for r in rows]

    def branch(self, branch_id: str) -> dict[str, Any]:
        row = self._require_branch(branch_id)
        return {
            "branch_id": row["branch_id"],
            "name": row["name"],
            "open": bool(row["open"]),
            "classes": self._managed_classes(branch_id),
        }

    def list_branches(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT branch_id FROM rs_branches ORDER BY branch_id"
        ).fetchall()
        return [self.branch(r["branch_id"]) for r in rows]

    def add_window(self, window_id: str, branch_id: str, hall_id: str) -> dict[str, Any]:
        self._require_branch(branch_id)
        with self._write_lock, _Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO rs_windows(window_id, hall_id, branch_id) VALUES(?,?,?) "
                "ON CONFLICT(window_id) DO UPDATE SET hall_id=excluded.hall_id, "
                "branch_id=excluded.branch_id",
                (window_id, hall_id, branch_id),
            )
        return {"window_id": window_id, "hall_id": hall_id, "branch_id": branch_id}

    def _window(self, window_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM rs_windows WHERE window_id=?", (window_id,)
        ).fetchone()
        if row is None:
            raise ReservationError(f"unknown window {window_id!r}")
        return row

    # ------------------------------------------------------------- area seq
    def _next_area_seq(self) -> int:
        return int(self._meta_int("rs_area_seq", 0)) + 1

    def _set_area_seq(self, value: int) -> None:
        self._set_meta("rs_area_seq", str(value))

    # -------------------------------------------------------- qualification
    def submit_qualification(
        self,
        bundle: Mapping[str, Any],
        *,
        request_id: str,
        at: str | None = None,
    ) -> dict[str, Any]:
        """Apply one committed cut and sign out a fresh waiting book per branch.

        The underlying catalog/fact bundle is committed atomically by
        :meth:`Store.apply_bundle`.  The projection, edges and direct classes
        are then fixed for that cut, so replay is deterministic.  Re-sending the
        same ``request_id`` replays the stored cut instead of advancing a shift.
        """

        recorded = self.conn.execute(
            "SELECT * FROM rs_cuts WHERE request_id=?", (request_id,)
        ).fetchone()
        if recorded is not None:
            return self._cut_report(recorded)

        committed = self.apply_bundle(dict(bundle), request_id=request_id)
        if committed.noop:
            latest = self.conn.execute(
                "SELECT * FROM rs_cuts ORDER BY area_seq DESC LIMIT 1"
            ).fetchone()
            if latest is None:
                raise ReservationError("no committed qualification cut yet")
            return {**self._cut_report(latest), "noop": True}

        at = at or _utc_now()
        with self._write_lock, _Transaction(self.conn):
            seq = self._next_area_seq()
            self._set_area_seq(seq)
            cut_id = f"Q{seq}"
            self.conn.execute(
                "INSERT INTO rs_cuts(area_seq, cut_id, request_id, at, "
                "catalog_revision, fact_revision, fact_watermark, kind, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    seq,
                    cut_id,
                    request_id,
                    at,
                    committed.cut.catalog_revision,
                    committed.cut.fact_revision,
                    committed.cut.fact_watermark,
                    "bundle",
                    _utc_now(),
                ),
            )
            self._rebuild_all()
        row = self.conn.execute(
            "SELECT * FROM rs_cuts WHERE area_seq=?", (seq,)
        ).fetchone()
        return self._cut_report(row)

    def seed(
        self,
        *,
        facts: Sequence[Mapping[str, Any]],
        order: Sequence[str],
        at: str = "1970-01-01T00:00:00+00:00",
        request_id: str = "seed",
    ) -> dict[str, Any]:
        """Apply the initial facts and fix the historical arrival order.

        ``order`` is the order the old hall first received the tickets; it
        becomes the initial shift ranking, recovered from real work orders
        rather than invented.
        """

        recorded = self.conn.execute(
            "SELECT * FROM rs_cuts WHERE request_id=?", (request_id,)
        ).fetchone()
        if recorded is not None:
            return self._cut_report(recorded)
        if not self.is_initialized():
            raise ReservationError("catalog must be initialized before seeding")
        self.apply_fact_revisions([dict(f) for f in facts], request_id=request_id)
        with self._write_lock, _Transaction(self.conn):
            for offset, ticket_id in enumerate(order, start=1):
                self.conn.execute(
                    "INSERT INTO rs_seed_order(ticket_id, ord) VALUES(?,?) "
                    "ON CONFLICT(ticket_id) DO UPDATE SET ord=excluded.ord",
                    (ticket_id, offset),
                )
            seq = max(self._next_area_seq(), len(order) + 1)
            self._set_area_seq(seq)
            cut = self.current_cut()
            cut_id = f"Q{seq}"
            self.conn.execute(
                "INSERT INTO rs_cuts(area_seq, cut_id, request_id, at, "
                "catalog_revision, fact_revision, fact_watermark, kind, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    seq,
                    cut_id,
                    request_id,
                    at,
                    cut.catalog_revision,
                    cut.fact_revision,
                    cut.fact_watermark,
                    "seed",
                    _utc_now(),
                ),
            )
            self._rebuild_all()
        row = self.conn.execute(
            "SELECT * FROM rs_cuts WHERE area_seq=?", (seq,)
        ).fetchone()
        return self._cut_report(row)

    def _cut_report(self, row: sqlite3.Row) -> dict[str, Any]:
        branches = {}
        for r in self.conn.execute(
            "SELECT branch_id FROM rs_branches ORDER BY branch_id"
        ).fetchall():
            try:
                branches[r["branch_id"]] = self.snapshot(
                    r["branch_id"], int(row["area_seq"])
                )
            except ReservationError:
                branches[r["branch_id"]] = None
        return {
            "cut_id": row["cut_id"],
            "area_seq": int(row["area_seq"]),
            "request_id": row["request_id"],
            "at": row["at"],
            "cut": {
                "catalog_revision": row["catalog_revision"],
                "fact_revision": row["fact_revision"],
                "fact_watermark": int(row["fact_watermark"]),
            },
            "books": branches,
        }

    def list_cuts(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM rs_cuts ORDER BY area_seq").fetchall()
        return [self._cut_report(r) for r in rows]

    # ----------------------------------------------------------- projections
    def _tickets_at(self, fact_watermark: int) -> dict[str, list[str]]:
        if fact_watermark == 0:
            return {}
        resolved = self._watermark_seq(fact_watermark)
        if resolved is None:
            raise ReservationError(f"unknown fact watermark {fact_watermark}")
        seq, _revision = resolved
        facts = self._load_facts_at_seq(seq)
        return {f.ticket_id: list(f.classes) for f in facts}

    def _seed_stays(self, cut_seq: int) -> dict[str, reservation.Stay]:
        rows = self.conn.execute("SELECT ticket_id, ord FROM rs_seed_order").fetchall()
        stays = {r["ticket_id"]: (_shift(int(r["ord"])), int(r["ord"])) for r in rows}
        return stays

    def _project(
        self,
        branch_id: str,
        classes: list[str],
        edges: Iterable[tuple[str, str]],
        tickets: Mapping[str, Sequence[str]],
        previous: Mapping[str, reservation.Stay],
        cut_seq: int,
        forced: Mapping[str, dict],
        suppressed: Iterable[str],
        seed_stays: Mapping[str, reservation.Stay],
    ) -> reservation.Projection:
        projection = reservation.compute_projection(
            cut_id="",
            branch_id=branch_id,
            managed_classes=classes,
            edges=edges,
            tickets=tickets,
            previous=previous,
            entered_shift=_shift(cut_seq),
            entered_seq=cut_seq,
            initial_stays=seed_stays,
        )
        by_id = {c.ticket_id: c for c in projection.candidates}
        for ticket_id, basis in forced.items():
            if ticket_id in by_id:
                continue
            stay = previous.get(ticket_id) or seed_stays.get(ticket_id) or (
                _shift(cut_seq),
                cut_seq,
            )
            match = reservation.Match(
                ticket_id=ticket_id,
                class_id=basis.get("class_id", ""),
                path=list(basis.get("path", [])),
            )
            by_id[ticket_id] = reservation.Candidate(
                ticket_id=ticket_id,
                entered_shift=stay[0],
                entered_seq=stay[1],
                match=match,
            )
        drop = set(suppressed)
        candidates = sorted(
            (c for c in by_id.values() if c.ticket_id not in drop),
            key=lambda c: c.sort_key,
        )
        present = {c.ticket_id for c in candidates}
        projection.candidates = candidates
        projection.leavers = sorted(set(previous) - present)
        return projection

    def _events(self, branch_id: str) -> list[tuple[tuple, str, sqlite3.Row]]:
        items: list[tuple[tuple, str, sqlite3.Row]] = []
        for row in self.conn.execute("SELECT * FROM rs_cuts").fetchall():
            items.append(((row["at"], int(row["area_seq"])), "cut", row))
        for row in self.conn.execute(
            "SELECT * FROM rs_gates WHERE branch_id=? OR branch_id='*'", (branch_id,)
        ).fetchall():
            items.append(((row["at"], int(row["area_seq"])), "gate", row))
        items.sort(key=lambda item: item[0])
        return items

    def _rebuild_branch(self, branch_id: str) -> None:
        classes = self._managed_classes(branch_id)
        if not classes:
            return
        previous: dict[str, reservation.Stay] = {}
        forced: dict[str, dict] = {}
        suppressed: set[str] = set()
        last_cut_seq = 0
        last_cut: sqlite3.Row | None = None
        last_tickets: Mapping[str, Sequence[str]] = {}
        last_edges: Iterable[tuple[str, str]] = ()
        seed_stays: dict[str, reservation.Stay] = {}
        first_cut_seq: int | None = None

        for _key, kind, row in self._events(branch_id):
            if kind == "gate":
                self._apply_gate_to_state(row, previous, forced, suppressed)
                continue
            last_cut_seq = int(row["area_seq"])
            last_cut = row
            last_edges = self.load_catalog_state(row["catalog_revision"]).edges
            last_tickets = self._tickets_at(int(row["fact_watermark"]))
            if not seed_stays:
                seed_stays = self._seed_stays(last_cut_seq)
            if first_cut_seq is None:
                first_cut_seq = last_cut_seq
            # The historical arrival order only establishes the first cut's
            # ranking; after that, leaving and returning starts a fresh stay.
            initial = seed_stays if last_cut_seq == first_cut_seq else {}
            projection = self._project(
                branch_id,
                classes,
                last_edges,
                last_tickets,
                previous,
                last_cut_seq,
                forced,
                suppressed,
                initial,
            )
            projection.cut_id = row["cut_id"]
            self._store_snapshot(branch_id, last_cut_seq, row["cut_id"], projection)
            previous = projection.stay_map()

        if last_cut is None:
            live = {
                "cut_id": None,
                "branch_id": branch_id,
                "managed_classes": sorted(classes),
                "waiting": [],
                "eligible": [],
                "leavers": [],
            }
        else:
            # Live view: the newest cut plus every gate recorded after it, so
            # pre-open appeals are visible immediately.
            live_projection = self._project(
                branch_id,
                classes,
                last_edges,
                last_tickets,
                previous,
                last_cut_seq,
                forced,
                suppressed,
                seed_stays if last_cut_seq == first_cut_seq else {},
            )
            live_projection.cut_id = last_cut["cut_id"]
            live = live_projection.as_dict()
        self.conn.execute(
            "INSERT INTO rs_live(branch_id, area_seq, result) VALUES(?,?,?) "
            "ON CONFLICT(branch_id) DO UPDATE SET area_seq=excluded.area_seq, "
            "result=excluded.result",
            (branch_id, last_cut_seq, _json_dump(live)),
        )

    def _store_snapshot(
        self,
        branch_id: str,
        area_seq: int,
        cut_id: str,
        projection: reservation.Projection,
    ) -> None:
        self.conn.execute(
            "INSERT INTO rs_snapshots(branch_id, area_seq, cut_id, result) "
            "VALUES(?,?,?,?) ON CONFLICT(branch_id, area_seq) DO UPDATE "
            "SET result=excluded.result, cut_id=excluded.cut_id",
            (branch_id, area_seq, cut_id, _json_dump(projection.as_dict())),
        )

    def _apply_gate_to_state(
        self,
        row: sqlite3.Row,
        previous: dict[str, reservation.Stay],
        forced: dict[str, dict],
        suppressed: set[str],
    ) -> None:
        kind = row["kind"]
        ticket_id = row["ticket_id"]
        seq = int(row["area_seq"])
        detail = json.loads(row["detail"]) if row["detail"] else {}
        if kind in {"insert", "restore"}:
            basis = json.loads(row["basis"]) if row["basis"] else {}
            forced[ticket_id] = basis
            suppressed.discard(ticket_id)
            position = detail.get("position", "back")
            if kind == "restore" or position == "back":
                stay = previous.get(ticket_id) or (_shift(seq), seq)
            elif position == "front":
                stay = ("S000000", 0)
            else:
                raise ReservationError(f"unknown insert position {position!r}")
            if detail.get("entered_shift"):
                stay = (detail["entered_shift"], detail.get("entered_seq", seq))
            previous[ticket_id] = stay
        elif kind == "drop":
            previous.pop(ticket_id, None)
            forced.pop(ticket_id, None)
            suppressed.add(ticket_id)
        elif kind == "correct":
            if ticket_id in previous:
                previous[ticket_id] = (
                    detail.get("entered_shift", previous[ticket_id][0]),
                    detail.get("entered_seq", previous[ticket_id][1]),
                )
        elif kind == "admit":
            suppressed.discard(ticket_id)
        else:
            raise ReservationError(f"unknown gate kind {kind!r}")

    def _rebuild_all(self) -> None:
        for row in self.conn.execute(
            "SELECT branch_id FROM rs_branches ORDER BY branch_id"
        ).fetchall():
            self._rebuild_branch(row["branch_id"])

    def ensure_current(self) -> None:
        """Self-heal projections if a previous process stopped mid-record."""

        with self._write_lock:
            expected = int(
                self.conn.execute("SELECT COUNT(*) AS n FROM rs_cuts").fetchone()["n"]
            )
            max_seq = int(
                self.conn.execute(
                    "SELECT COALESCE(MAX(area_seq), 0) AS n FROM rs_cuts"
                ).fetchone()["n"]
            )
            for row in self.conn.execute(
                "SELECT branch_id FROM rs_branches ORDER BY branch_id"
            ).fetchall():
                live = self.conn.execute(
                    "SELECT area_seq FROM rs_live WHERE branch_id=?",
                    (row["branch_id"],),
                ).fetchone()
                snaps = int(
                    self.conn.execute(
                        "SELECT COUNT(*) AS n FROM rs_snapshots WHERE branch_id=?",
                        (row["branch_id"],),
                    ).fetchone()["n"]
                )
                if live is None or int(live["area_seq"]) != max_seq or snaps != expected:
                    with _Transaction(self.conn):
                        self._rebuild_branch(row["branch_id"])

    def snapshot(self, branch_id: str, area_seq: int) -> dict[str, Any]:
        self._require_branch(branch_id)
        row = self.conn.execute(
            "SELECT result FROM rs_snapshots WHERE branch_id=? AND area_seq=?",
            (branch_id, area_seq),
        ).fetchone()
        if row is None:
            raise ReservationError(
                f"branch {branch_id!r} has no snapshot for area_seq {area_seq}"
            )
        return json.loads(row["result"])

    def live(self, branch_id: str) -> dict[str, Any]:
        self._require_branch(branch_id)
        self.ensure_current()
        row = self.conn.execute(
            "SELECT result FROM rs_live WHERE branch_id=?", (branch_id,)
        ).fetchone()
        if row is None:
            raise ReservationError(f"branch {branch_id!r} has no live projection")
        return json.loads(row["result"])

    def eligible(self, branch_id: str) -> list[str]:
        return list(self.live(branch_id)["eligible"])

    # ----------------------------------------------------------------- gates
    def _basis_for(self, branch_id: str, ticket_id: str) -> dict[str, Any] | None:
        for entry in self.live(branch_id)["waiting"]:
            if entry["ticket_id"] == ticket_id:
                return entry["match"]
        return None

    def apply_gate(
        self,
        *,
        branch_id: str,
        kind: str,
        ticket_id: str,
        justification: str,
        request_id: str,
        at: str | None = None,
        basis: Mapping[str, Any] | None = None,
        position: str = "back",
        entered_shift: str | None = None,
        entered_seq: int | None = None,
    ) -> dict[str, Any]:
        """Record a supervisor gate before any window opens.

        Kinds: ``insert`` (missed registration / jump the queue), ``drop``,
        ``restore`` (re-open a dropped entry with its original basis) and
        ``correct`` (re-age an entry).  A gate never rewrites a committed cut;
        it is a new timestamped event projected on top.
        """

        self._require_branch(branch_id)
        if kind not in {"insert", "restore", "drop", "correct", "admit"}:
            raise ReservationError(f"unknown gate kind {kind!r}")
        if not ticket_id:
            raise ReservationError("gate requires a ticket_id")
        if not justification:
            raise ReservationError("gate requires a justification")
        existing = self.conn.execute(
            "SELECT * FROM rs_gates WHERE request_id=?", (request_id,)
        ).fetchone()
        if existing is not None:
            return self._gate_report(existing)
        if kind == "restore":
            drop = self.conn.execute(
                "SELECT * FROM rs_gates WHERE branch_id=? AND ticket_id=? "
                "AND kind='drop' ORDER BY area_seq DESC LIMIT 1",
                (branch_id, ticket_id),
            ).fetchone()
            if drop is None:
                raise ReservationError(
                    f"ticket {ticket_id!r} has no dropped entry to restore"
                )
            if basis is None:
                basis = json.loads(drop["basis"]) if drop["basis"] else None
            if basis is None:
                basis = self._basis_for(branch_id, ticket_id)
            if basis is None:
                raise ReservationError(
                    f"ticket {ticket_id!r}: no stored classification basis to restore"
                )
        elif kind in {"insert"} and basis is None:
            basis = self._basis_for(branch_id, ticket_id)
            if basis is None:
                raise ReservationError(
                    f"ticket {ticket_id!r} is not reachable; a classification "
                    f"basis is required to force it in"
                )
        if kind == "drop" and basis is None:
            # Keep the entry's current basis so a restore can re-open it.
            basis = self._basis_for(branch_id, ticket_id)
        at = at or _utc_now()
        detail: dict[str, Any] = {"position": position}
        if entered_shift is not None:
            detail["entered_shift"] = entered_shift
        if entered_seq is not None:
            detail["entered_seq"] = entered_seq
        with self._write_lock, _Transaction(self.conn):
            seq = self._next_area_seq()
            self._set_area_seq(seq)
            self.conn.execute(
                "INSERT INTO rs_gates(area_seq, branch_id, kind, at, ticket_id, "
                "basis, detail, justification, request_id, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    seq,
                    branch_id,
                    kind,
                    at,
                    ticket_id,
                    _json_dump(basis) if basis is not None else None,
                    _json_dump(detail),
                    justification,
                    request_id,
                    _utc_now(),
                ),
            )
            self._rebuild_all()
        row = self.conn.execute(
            "SELECT * FROM rs_gates WHERE area_seq=?", (seq,)
        ).fetchone()
        return self._gate_report(row)

    def _gate_report(self, row: sqlite3.Row) -> dict[str, Any]:
        detail = json.loads(row["detail"]) if row["detail"] else {}
        return {
            "area_seq": int(row["area_seq"]),
            "branch_id": row["branch_id"],
            "kind": row["kind"],
            "at": row["at"],
            "ticket_id": row["ticket_id"],
            "basis": json.loads(row["basis"]) if row["basis"] else None,
            "justification": row["justification"],
            "request_id": row["request_id"],
            "detail": detail,
        }

    # ----------------------------------------------------------------- books
    def open_book(
        self,
        *,
        branch_id: str,
        opened_by: str,
        request_id: str,
        ticket_ids: Sequence[str] | None = None,
        limit: int | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        """Sign out a waiting book from exactly one committed cut.

        The frozen snapshot is what a later re-open reads, so the order and the
        classification basis stay auditable even after the catalog changes.
        """

        self._require_branch(branch_id)
        existing = self.conn.execute(
            "SELECT * FROM rs_books WHERE request_id=?", (request_id,)
        ).fetchone()
        if existing is not None:
            return self._book_report(existing)
        self.ensure_current()
        live = self.live(branch_id)
        cut_id = live["cut_id"]
        if cut_id is None:
            raise ReservationError("no committed cut to sign out a book from")
        cut = self.conn.execute(
            "SELECT * FROM rs_cuts WHERE cut_id=?", (cut_id,)
        ).fetchone()
        order = [entry["ticket_id"] for entry in live["waiting"]]
        if ticket_ids is not None:
            unknown = sorted(set(ticket_ids) - set(order))
            if unknown:
                raise ReservationError(
                    "tickets not waiting at this cut: " + ", ".join(unknown)
                )
            selected = [t for t in order if t in set(ticket_ids)]
        else:
            selected = order
        if limit is not None:
            selected = selected[:limit]
        frozen = {
            "cut_id": cut_id,
            "area_seq": int(cut["area_seq"]),
            "at": cut["at"],
            "cut": {
                "catalog_revision": cut["catalog_revision"],
                "fact_revision": cut["fact_revision"],
                "fact_watermark": int(cut["fact_watermark"]),
            },
            "snapshot": self.snapshot(branch_id, int(cut["area_seq"])),
            "waiting": live["waiting"],
            "selected": selected,
        }
        book_id = "BK-" + hashlib.sha256(
            _json_dump([branch_id, request_id]).encode("utf-8")
        ).hexdigest()[:12]
        at = at or _utc_now()
        with self._write_lock, _Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO rs_books(book_id, branch_id, cut_id, area_seq, "
                "opened_by, at, frozen, status, request_id, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    book_id,
                    branch_id,
                    cut_id,
                    int(cut["area_seq"]),
                    opened_by,
                    at,
                    _json_dump(frozen),
                    "open",
                    request_id,
                    _utc_now(),
                ),
            )
            for index, ticket_id in enumerate(selected, start=1):
                self.conn.execute(
                    "INSERT INTO rs_bookings(book_id, ticket_id, sequence, created_at) "
                    "VALUES(?,?,?,?)",
                    (book_id, ticket_id, index, _utc_now()),
                )
        return self.book(book_id)

    def _book_report(self, row: sqlite3.Row) -> dict[str, Any]:
        return self.book(row["book_id"])

    def book(self, book_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM rs_books WHERE book_id=?", (book_id,)
        ).fetchone()
        if row is None:
            raise ReservationError(f"unknown book {book_id!r}")
        bookings = self.conn.execute(
            "SELECT ticket_id, sequence FROM rs_bookings WHERE book_id=? "
            "ORDER BY sequence",
            (book_id,),
        ).fetchall()
        return {
            "book_id": row["book_id"],
            "branch_id": row["branch_id"],
            "cut_id": row["cut_id"],
            "area_seq": int(row["area_seq"]),
            "opened_by": row["opened_by"],
            "at": row["at"],
            "status": row["status"],
            "frozen": json.loads(row["frozen"]),
            "entries": [b["ticket_id"] for b in bookings],
        }

    def books(self, branch_id: str | None = None) -> list[dict[str, Any]]:
        if branch_id is None:
            rows = self.conn.execute(
                "SELECT book_id FROM rs_books ORDER BY created_at, book_id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT book_id FROM rs_books WHERE branch_id=? "
                "ORDER BY created_at, book_id",
                (branch_id,),
            ).fetchall()
        return [self.book(r["book_id"]) for r in rows]

    def close_book(self, book_id: str) -> dict[str, Any]:
        book = self.book(book_id)
        with self._write_lock, _Transaction(self.conn):
            self.conn.execute(
                "UPDATE rs_books SET status='closed' WHERE book_id=?", (book_id,)
            )
        return {**book, "status": "closed"}

    def reopen_book(self, book_id: str, *, by: str) -> dict[str, Any]:
        """Re-open a signed-out book; the frozen snapshot is read back as-is."""

        book = self.book(book_id)
        with self._write_lock, _Transaction(self.conn):
            self.conn.execute(
                "UPDATE rs_books SET status='open' WHERE book_id=?", (book_id,)
            )
        frozen = book["frozen"]
        selected = set(frozen["selected"])
        return {
            "reopened_by": by,
            "book_id": book_id,
            "cut_id": frozen["cut_id"],
            "cut": frozen["cut"],
            "entry_count": len(frozen["selected"]),
            "entries": frozen["selected"],
            "classification": [
                {
                    "ticket_id": entry["ticket_id"],
                    "class_id": entry["match"]["class_id"],
                    "path": entry["match"]["path"],
                }
                for entry in frozen["waiting"]
                if entry["ticket_id"] in selected
            ],
        }

    # ---------------------------------------------------------------- day quota
    def set_day_quota(
        self, branch_id: str, service_day: str, capacity: int
    ) -> dict[str, Any]:
        self._require_branch(branch_id)
        if capacity < 0:
            raise ReservationError("capacity must be non-negative")
        with self._write_lock, _Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO rs_day_quota(branch_id, service_day, capacity, issued, closed) "
                "VALUES(?,?,?,0,0) ON CONFLICT(branch_id, service_day) DO UPDATE "
                "SET capacity=excluded.capacity",
                (branch_id, service_day, capacity),
            )
        return self.day_status(branch_id, service_day)

    def open_day(
        self, branch_id: str, service_day: str, capacity: int | None = None
    ) -> dict[str, Any]:
        self._require_branch(branch_id)
        with self._write_lock, _Transaction(self.conn):
            row = self.conn.execute(
                "SELECT * FROM rs_day_quota WHERE branch_id=? AND service_day=?",
                (branch_id, service_day),
            ).fetchone()
            if row is None:
                if capacity is None:
                    raise ReservationError("a new day needs a capacity")
                self.conn.execute(
                    "INSERT INTO rs_day_quota(branch_id, service_day, capacity, issued, closed) "
                    "VALUES(?,?,?,0,0)",
                    (branch_id, service_day, capacity),
                )
            else:
                self.conn.execute(
                    "UPDATE rs_day_quota SET closed=0, capacity=? "
                    "WHERE branch_id=? AND service_day=?",
                    (
                        capacity if capacity is not None else row["capacity"],
                        branch_id,
                        service_day,
                    ),
                )
        return self.day_status(branch_id, service_day)

    def close_day(self, branch_id: str, service_day: str) -> dict[str, Any]:
        with self._write_lock, _Transaction(self.conn):
            self.conn.execute(
                "UPDATE rs_day_quota SET closed=1 WHERE branch_id=? AND service_day=?",
                (branch_id, service_day),
            )
        return self.day_status(branch_id, service_day)

    def day_status(self, branch_id: str, service_day: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM rs_day_quota WHERE branch_id=? AND service_day=?",
            (branch_id, service_day),
        ).fetchone()
        if row is None:
            return {
                "branch_id": branch_id,
                "service_day": service_day,
                "capacity": 0,
                "issued": 0,
                "remaining": 0,
                "closed": True,
                "opened": False,
            }
        capacity = int(row["capacity"])
        issued = int(row["issued"])
        return {
            "branch_id": branch_id,
            "service_day": service_day,
            "capacity": capacity,
            "issued": issued,
            "remaining": max(capacity - issued, 0),
            "closed": bool(row["closed"]),
            "opened": True,
        }

    # ------------------------------------------------------------- issuance
    def has_ticket(self, ticket_id: str, service_day: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM rs_tickets WHERE ticket_id=? AND service_day=?",
            (ticket_id, service_day),
        ).fetchone()
        return dict(row) if row else None

    def lookup_ticket(
        self, branch_id: str, service_day: str, ticket_no: str
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM rs_tickets WHERE branch_id=? AND service_day=? AND ticket_no=?",
            (branch_id, service_day, ticket_no),
        ).fetchone()
        return dict(row) if row else None

    def tickets_for(self, branch_id: str, service_day: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM rs_tickets WHERE branch_id=? AND service_day=? "
            "ORDER BY CAST(ticket_no AS INTEGER)",
            (branch_id, service_day),
        ).fetchall()
        return [dict(r) for r in rows]

    def import_external_ticket(
        self,
        *,
        ticket_id: str,
        service_day: str,
        ticket_no: str,
        window_id: str,
        external_id: str,
        branch_id: str,
        source: str = "legacy",
    ) -> dict[str, Any]:
        """Import a ticket already issued elsewhere (old hall) into the ledger.

        Idempotent on the one-person-per-day uniqueness.  It does not consume
        this branch's quota: the old hall already spent its own.
        """

        existing = self.conn.execute(
            "SELECT * FROM rs_tickets WHERE ticket_id=? AND service_day=?",
            (ticket_id, service_day),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        with self._write_lock, _Transaction(self.conn):
            try:
                self.conn.execute(
                    "INSERT INTO rs_tickets(ticket_no, branch_id, service_day, ticket_id, "
                    "window_id, source, external_id, state, created_at) "
                    "VALUES(?,?,?,?,?,?,?, 'issued', ?)",
                    (
                        ticket_no,
                        branch_id,
                        service_day,
                        ticket_id,
                        window_id,
                        source,
                        external_id,
                        _utc_now(),
                    ),
                )
            except sqlite3.IntegrityError:
                pass
        row = self.conn.execute(
            "SELECT * FROM rs_tickets WHERE ticket_id=? AND service_day=?",
            (ticket_id, service_day),
        ).fetchone()
        return dict(row)

    def issue(
        self,
        *,
        request_id: str,
        window_id: str,
        service_day: str,
        ticket_id: str | None = None,
        reconcile: Callable[[str, str], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """Issue the next实名 ticket of a branch for a service day.

        Ordering is the signed-out waiting order.  A person already holding a
        ticket that day (in this ledger, or found via ``reconcile`` on the old
        hall) is skipped; the counter seat is allocated exactly once, and two
        windows racing for the same person produce one ticket plus a retry from
        the queue head.
        """

        existing = self.conn.execute(
            "SELECT * FROM rs_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if existing is not None and existing["status"] == "issued":
            # A committed issuance is immutable: a retry returns the same ticket
            # and never draws a second seat.  A non-issued outcome (exhausted /
            # closed) is re-evaluated, since the queue can change before a retry.
            return self._request_report(existing)
        window = self._window(window_id)
        branch_id = window["branch_id"]
        branch_row = self._require_branch(branch_id)
        if not int(branch_row["open"]):
            return self._record_request(
                request_id,
                window_id,
                branch_id,
                service_day,
                ticket_id,
                "closed",
                error=f"branch {branch_id!r} is closed for handover",
            )
        day = self.conn.execute(
            "SELECT * FROM rs_day_quota WHERE branch_id=? AND service_day=?",
            (branch_id, service_day),
        ).fetchone()
        if day is None:
            raise ReservationError(
                f"branch {branch_id!r} has no quota for {service_day!r}"
            )
        if int(day["closed"]):
            raise ReservationError(f"branch {branch_id!r} is closed for {service_day!r}")
        if int(day["issued"]) >= int(day["capacity"]):
            return self._record_request(
                request_id,
                window_id,
                branch_id,
                service_day,
                ticket_id,
                "exhausted",
                error="no ticket remains for this branch today",
            )

        self.ensure_current()
        queue = self.eligible(branch_id)
        if ticket_id is not None:
            queue = [ticket_id] if ticket_id in queue else []
        if not queue:
            return self._record_request(
                request_id,
                window_id,
                branch_id,
                service_day,
                ticket_id,
                "exhausted",
                error="nobody is waiting for this branch",
            )

        for candidate in queue:
            held = self.has_ticket(candidate, service_day)
            if held is None and reconcile is not None:
                found = reconcile(candidate, service_day)
                status = (found or {}).get("status")
                if found is not None and status in {"issued", "committed"}:
                    held = self.import_external_ticket(
                        ticket_id=candidate,
                        service_day=service_day,
                        ticket_no=str(found.get("ticket_no")),
                        window_id=str(found.get("window_id", "legacy")),
                        external_id=str(found.get("request_id")),
                        branch_id=str(found.get("hall_id", "legacy")),
                        source="legacy",
                    )
                elif found is not None and status in {
                    "in_progress",
                    "pending",
                    "processing",
                }:
                    # The old hall is still working on this person; the seat
                    # cannot be given to anyone else yet.  Skip them.
                    continue
            if held is not None:
                continue
            try:
                return self._allocate(request_id, window, service_day, candidate)
            except sqlite3.IntegrityError:
                # Lost a race for this person; continue from the queue head.
                continue
        return self._record_request(
            request_id,
            window_id,
            branch_id,
            service_day,
            ticket_id,
            "exhausted",
            error="every waiting person already holds a ticket today",
        )

    def redeem_seat(
        self,
        *,
        branch_id: str,
        service_day: str,
        ticket_id: str,
        window_id: str,
        source: str = "prebooked",
    ) -> dict[str, Any]:
        """Consume one of a branch's seats for an already-served person.

        Models a number that has already been redeemed earlier in the day (or
        before handover).  It counts against the day's quota and cannot be
        transferred to another person.
        """

        self._require_branch(branch_id)
        with self._write_lock, _Transaction(self.conn):
            day = self.conn.execute(
                "SELECT * FROM rs_day_quota WHERE branch_id=? AND service_day=?",
                (branch_id, service_day),
            ).fetchone()
            if day is None:
                raise ReservationError(
                    f"branch {branch_id!r} has no quota for {service_day!r}"
                )
            if int(day["issued"]) >= int(day["capacity"]):
                raise ReservationError(
                    f"branch {branch_id!r} has no seat left on {service_day!r}"
                )
            held = self.conn.execute(
                "SELECT 1 FROM rs_tickets WHERE ticket_id=? AND service_day=?",
                (ticket_id, service_day),
            ).fetchone()
            if held is not None:
                raise ReservationError(
                    f"{ticket_id!r} already holds a ticket on {service_day!r}"
                )
            ticket_no = str(int(day["issued"]) + 1)
            self.conn.execute(
                "INSERT INTO rs_tickets(ticket_no, branch_id, service_day, ticket_id, "
                "window_id, source, state, created_at) "
                "VALUES(?,?,?,?,?,?, 'redeemed', ?)",
                (
                    ticket_no,
                    branch_id,
                    service_day,
                    ticket_id,
                    window_id,
                    source,
                    _utc_now(),
                ),
            )
            self.conn.execute(
                "UPDATE rs_day_quota SET issued=issued+1 "
                "WHERE branch_id=? AND service_day=?",
                (branch_id, service_day),
            )
        return {
            "branch_id": branch_id,
            "service_day": service_day,
            "ticket_id": ticket_id,
            "ticket_no": ticket_no,
        }

    def _allocate(
        self,
        request_id: str,
        window: sqlite3.Row,
        service_day: str,
        ticket_id: str,
    ) -> dict[str, Any]:
        branch_id = window["branch_id"]
        window_id = window["window_id"]
        with self._write_lock, _Transaction(self.conn):
            day = self.conn.execute(
                "SELECT * FROM rs_day_quota WHERE branch_id=? AND service_day=?",
                (branch_id, service_day),
            ).fetchone()
            if day is None or int(day["closed"]):
                raise ReservationError("branch day closed")
            issued = int(day["issued"])
            if issued >= int(day["capacity"]):
                result = {"status": "exhausted",
                          "error": "no ticket remains for this branch today"}
                self._write_request(
                    request_id, window_id, branch_id, service_day, ticket_id,
                    "exhausted", result,
                )
                return {"request_id": request_id, **result}
            ticket_no = str(issued + 1)
            self.conn.execute(
                "INSERT INTO rs_tickets(ticket_no, branch_id, service_day, ticket_id, "
                "window_id, source, request_id, state, created_at) "
                "VALUES(?,?,?,?,?, 'new', ?, 'issued', ?)",
                (
                    ticket_no,
                    branch_id,
                    service_day,
                    ticket_id,
                    window_id,
                    request_id,
                    _utc_now(),
                ),
            )
            self.conn.execute(
                "UPDATE rs_day_quota SET issued=issued+1 "
                "WHERE branch_id=? AND service_day=?",
                (branch_id, service_day),
            )
            result = {
                "status": "issued",
                "ticket_no": ticket_no,
                "ticket_id": ticket_id,
                "branch_id": branch_id,
                "window_id": window_id,
                "service_day": service_day,
            }
            self._write_request(
                request_id, window_id, branch_id, service_day, ticket_id,
                "issued", result,
            )
        return {"request_id": request_id, **result}

    def _write_request(
        self,
        request_id: str,
        window_id: str,
        branch_id: str,
        service_day: str,
        ticket_id: str | None,
        status: str,
        result: dict[str, Any],
    ) -> None:
        self.conn.execute(
            "INSERT INTO rs_requests(request_id, window_id, branch_id, service_day, "
            "ticket_id, status, result, error, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE "
            "SET status=excluded.status, result=excluded.result, error=excluded.error",
            (
                request_id,
                window_id,
                branch_id,
                service_day,
                ticket_id,
                status,
                _json_dump(result),
                result.get("error"),
                _utc_now(),
            ),
        )

    def _record_request(
        self,
        request_id: str,
        window_id: str,
        branch_id: str,
        service_day: str,
        ticket_id: str | None,
        status: str,
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        result = {"status": status}
        if error:
            result["error"] = error
        with self._write_lock, _Transaction(self.conn):
            self._write_request(
                request_id, window_id, branch_id, service_day, ticket_id, status, result
            )
        return {"request_id": request_id, **result}

    def _request_report(self, row: sqlite3.Row) -> dict[str, Any]:
        result = json.loads(row["result"]) if row["result"] else {"status": row["status"]}
        return {"request_id": row["request_id"], **result}

    def request(self, request_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM rs_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        return self._request_report(row) if row else None

    def window_issued(self, window_id: str, service_day: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM rs_tickets WHERE window_id=? AND service_day=? "
            "ORDER BY CAST(ticket_no AS INTEGER)",
            (window_id, service_day),
        ).fetchall()
        return [dict(r) for r in rows]

    def window_summary(self, window_id: str, service_day: str) -> dict[str, Any]:
        window = self._window(window_id)
        tickets = self.window_issued(window_id, service_day)
        return {
            "window_id": window_id,
            "branch_id": window["branch_id"],
            "service_day": service_day,
            "issued": len(tickets),
            "tickets": tickets,
            "remaining": self.day_status(window["branch_id"], service_day)["remaining"],
        }
