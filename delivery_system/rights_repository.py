"""Persistence for the rights desk and the distribution desk.

A third, deliberately separate SQLite database holds usage registrations,
original root grants, delegations (with a revocation flag) and offered tickets.
The rights relationships are stored, not recomputed into a flat "who may sign"
table: every answer rebuilds the chains through :class:`RightsEngine`, so a
revocation cuts exactly the paths that pass through the revoked delegation.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional, Sequence

from .errors import (
    DuplicateRightsRecord,
    RightsDataError,
    UnknownAuthorization,
)
from .models import SourceRef
from .rights_engine import RightsEngine
from .rights_models import (
    BlockRef,
    Delegation,
    RootGrant,
    Scope,
    Ticket,
    UsageRegistration,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_registrations (
    registration_id TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL,
    strip_id        TEXT NOT NULL,
    refs_json       TEXT NOT NULL,
    rights_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reg_doc ON usage_registrations (document_id);
CREATE TABLE IF NOT EXISTS root_grants (
    grant_id        TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL,
    registration_id TEXT NOT NULL,
    right           TEXT NOT NULL,
    rights_holder   TEXT NOT NULL,
    scope_json      TEXT NOT NULL,
    sublicensable   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_root_reg ON root_grants (registration_id);
CREATE TABLE IF NOT EXISTS delegations (
    delegation_id TEXT PRIMARY KEY,
    document_id   TEXT NOT NULL,
    parent_id     TEXT NOT NULL,
    trustee       TEXT NOT NULL,
    scope_json    TEXT NOT NULL,
    sublicensable INTEGER NOT NULL,
    revoked       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_deleg_parent ON delegations (parent_id);
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id  TEXT PRIMARY KEY,
    vendor     TEXT NOT NULL,
    rights_json TEXT NOT NULL,
    days_json  TEXT NOT NULL,
    fee        REAL NOT NULL,
    grp        TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _scope_to_json(scope: Scope) -> str:
    return json.dumps(scope.to_dict(), ensure_ascii=False, sort_keys=True)


def _scope_from_json(raw: str) -> Scope:
    data = json.loads(raw)
    return Scope(
        venue=data.get("venue"),
        language=data.get("language"),
        date_from=data.get("date_from"),
        date_to=data.get("date_to"),
        purpose=data.get("purpose"),
    )


def _refs_to_json(refs: Iterable[SourceRef]) -> str:
    return json.dumps(
        [{"segment_id": r.segment_id, "revision": r.revision} for r in refs],
        ensure_ascii=False,
        sort_keys=True,
    )


def _refs_from_json(raw: str) -> tuple[SourceRef, ...]:
    return tuple(
        SourceRef(segment_id=item["segment_id"], revision=int(item["revision"]))
        for item in json.loads(raw)
    )


class RightsRepository:
    def __init__(self, db_path: str, legacy_document_ids: Iterable[str] = ()) -> None:
        self.db_path = os.fspath(db_path)
        self.legacy_document_ids = frozenset(legacy_document_ids)
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def _transaction(self, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # registrations
    # ------------------------------------------------------------------
    def register_usage(
        self,
        registration_id: str,
        document_id: str,
        block: BlockRef,
        rights: Sequence[str],
    ) -> UsageRegistration:
        reg = UsageRegistration(
            registration_id=registration_id,
            document_id=document_id,
            block=block,
            rights=tuple(rights),
        )
        conn = self._connect()
        try:
            with self._transaction(conn):
                existing = conn.execute(
                    "SELECT document_id, strip_id, refs_json, rights_json"
                    " FROM usage_registrations WHERE registration_id = ?",
                    (registration_id,),
                ).fetchone()
                payload = (
                    document_id,
                    block.strip_id,
                    _refs_to_json(block.refs),
                    json.dumps(list(reg.rights), ensure_ascii=False, sort_keys=True),
                )
                if existing is not None:
                    current = (
                        existing["document_id"],
                        existing["strip_id"],
                        existing["refs_json"],
                        existing["rights_json"],
                    )
                    if current != payload:
                        raise DuplicateRightsRecord(
                            f"registration {registration_id!r} already exists with"
                            " different content"
                        )
                    return reg
                conn.execute(
                    "INSERT INTO usage_registrations (registration_id, document_id,"
                    " strip_id, refs_json, rights_json, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (registration_id, *payload[:3], payload[3], _now()),
                )
        finally:
            conn.close()
        return reg

    def list_registrations(self, document_id: Optional[str] = None) -> list[UsageRegistration]:
        conn = self._connect()
        try:
            if document_id is None:
                rows = conn.execute(
                    "SELECT * FROM usage_registrations ORDER BY registration_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM usage_registrations WHERE document_id = ?"
                    " ORDER BY registration_id",
                    (document_id,),
                ).fetchall()
            return [self._registration_from_row(row) for row in rows]
        finally:
            conn.close()

    @staticmethod
    def _registration_from_row(row: sqlite3.Row) -> UsageRegistration:
        block = BlockRef(
            strip_id=row["strip_id"],
            refs=_refs_from_json(row["refs_json"]),
        )
        return UsageRegistration(
            registration_id=row["registration_id"],
            document_id=row["document_id"],
            block=block,
            rights=tuple(json.loads(row["rights_json"])),
        )

    # ------------------------------------------------------------------
    # root grants
    # ------------------------------------------------------------------
    def add_root_grant(
        self,
        grant_id: str,
        document_id: str,
        registration_id: str,
        right: str,
        rights_holder: str,
        *,
        scope: Optional[Scope] = None,
        sublicensable: bool = True,
    ) -> RootGrant:
        grant = RootGrant(
            grant_id=grant_id,
            document_id=document_id,
            registration_id=registration_id,
            right=right,
            rights_holder=rights_holder,
            scope=scope or Scope(),
            sublicensable=sublicensable,
        )
        conn = self._connect()
        try:
            with self._transaction(conn):
                if not self._registration_exists(conn, registration_id):
                    raise UnknownAuthorization(
                        f"registration {registration_id!r} not found"
                    )
                existing = conn.execute(
                    "SELECT 1 FROM root_grants WHERE grant_id = ?", (grant_id,)
                ).fetchone()
                if existing is not None:
                    raise DuplicateRightsRecord(f"root grant {grant_id!r} already exists")
                conn.execute(
                    "INSERT INTO root_grants (grant_id, document_id, registration_id,"
                    " right, rights_holder, scope_json, sublicensable)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        grant_id,
                        document_id,
                        registration_id,
                        right,
                        rights_holder,
                        _scope_to_json(grant.scope),
                        1 if sublicensable else 0,
                    ),
                )
        finally:
            conn.close()
        return grant

    @staticmethod
    def _registration_exists(conn: sqlite3.Connection, registration_id: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM usage_registrations WHERE registration_id = ?",
            (registration_id,),
        ).fetchone() is not None

    def list_root_grants(self) -> list[RootGrant]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM root_grants ORDER BY grant_id"
            ).fetchall()
            return [
                RootGrant(
                    grant_id=row["grant_id"],
                    document_id=row["document_id"],
                    registration_id=row["registration_id"],
                    right=row["right"],
                    rights_holder=row["rights_holder"],
                    scope=_scope_from_json(row["scope_json"]),
                    sublicensable=bool(row["sublicensable"]),
                )
                for row in rows
            ]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # delegations + revocation
    # ------------------------------------------------------------------
    def add_delegation(
        self,
        delegation_id: str,
        document_id: str,
        parent_id: str,
        trustee: str,
        *,
        scope: Optional[Scope] = None,
        sublicensable: bool = True,
    ) -> Delegation:
        delegation = Delegation(
            delegation_id=delegation_id,
            document_id=document_id,
            parent_id=parent_id,
            trustee=trustee,
            scope=scope or Scope(),
            sublicensable=sublicensable,
            revoked=False,
        )
        conn = self._connect()
        try:
            with self._transaction(conn):
                parent = self._authorization_row(conn, parent_id)
                if parent is None:
                    raise UnknownAuthorization(
                        f"parent authorization {parent_id!r} not found"
                    )
                if parent["document_id"] != document_id:
                    raise RightsDataError(
                        "delegation document does not match its parent authorization"
                    )
                existing = conn.execute(
                    "SELECT 1 FROM delegations WHERE delegation_id = ?", (delegation_id,)
                ).fetchone()
                if existing is not None:
                    raise DuplicateRightsRecord(
                        f"delegation {delegation_id!r} already exists"
                    )
                conn.execute(
                    "INSERT INTO delegations (delegation_id, document_id, parent_id,"
                    " trustee, scope_json, sublicensable, revoked)"
                    " VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (
                        delegation_id,
                        document_id,
                        parent_id,
                        trustee,
                        _scope_to_json(delegation.scope),
                        1 if sublicensable else 0,
                    ),
                )
        finally:
            conn.close()
        return delegation

    @staticmethod
    def _authorization_row(conn: sqlite3.Connection, auth_id: str) -> Optional[sqlite3.Row]:
        row = conn.execute(
            "SELECT document_id FROM root_grants WHERE grant_id = ?", (auth_id,)
        ).fetchone()
        if row is not None:
            return row
        return conn.execute(
            "SELECT document_id FROM delegations WHERE delegation_id = ?", (auth_id,)
        ).fetchone()

    def revoke_delegation(self, delegation_id: str) -> None:
        conn = self._connect()
        try:
            with self._transaction(conn):
                cur = conn.execute(
                    "UPDATE delegations SET revoked = 1 WHERE delegation_id = ?",
                    (delegation_id,),
                )
                if cur.rowcount == 0:
                    raise UnknownAuthorization(
                        f"delegation {delegation_id!r} not found"
                    )
        finally:
            conn.close()

    def list_delegations(self) -> list[Delegation]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM delegations ORDER BY delegation_id"
            ).fetchall()
            return [
                Delegation(
                    delegation_id=row["delegation_id"],
                    document_id=row["document_id"],
                    parent_id=row["parent_id"],
                    trustee=row["trustee"],
                    scope=_scope_from_json(row["scope_json"]),
                    sublicensable=bool(row["sublicensable"]),
                    revoked=bool(row["revoked"]),
                )
                for row in rows
            ]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # tickets
    # ------------------------------------------------------------------
    def add_ticket(
        self,
        ticket_id: str,
        vendor: str,
        rights: Sequence[str],
        days: Sequence[str],
        fee: float,
        *,
        group: Optional[str] = None,
    ) -> Ticket:
        ticket = Ticket(
            ticket_id=ticket_id,
            vendor=vendor,
            rights=tuple(rights),
            days=tuple(days),
            fee=fee,
            group=group,
        )
        conn = self._connect()
        try:
            with self._transaction(conn):
                existing = conn.execute(
                    "SELECT vendor, rights_json, days_json, fee, grp FROM tickets"
                    " WHERE ticket_id = ?",
                    (ticket_id,),
                ).fetchone()
                payload = (
                    vendor,
                    json.dumps(list(ticket.rights), ensure_ascii=False, sort_keys=True),
                    json.dumps(list(ticket.days), ensure_ascii=False, sort_keys=True),
                    fee,
                    group,
                )
                if existing is not None:
                    current = (
                        existing["vendor"],
                        existing["rights_json"],
                        existing["days_json"],
                        existing["fee"],
                        existing["grp"],
                    )
                    if current != payload:
                        raise DuplicateRightsRecord(
                            f"ticket {ticket_id!r} already exists with different content"
                        )
                    return ticket
                conn.execute(
                    "INSERT INTO tickets (ticket_id, vendor, rights_json, days_json,"
                    " fee, grp) VALUES (?, ?, ?, ?, ?, ?)",
                    (ticket_id, *payload),
                )
        finally:
            conn.close()
        return ticket

    def list_tickets(self) -> list[Ticket]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM tickets ORDER BY ticket_id"
            ).fetchall()
            return [
                Ticket(
                    ticket_id=row["ticket_id"],
                    vendor=row["vendor"],
                    rights=tuple(json.loads(row["rights_json"])),
                    days=tuple(json.loads(row["days_json"])),
                    fee=float(row["fee"]),
                    group=row["grp"],
                )
                for row in rows
            ]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # engine assembly
    # ------------------------------------------------------------------
    def engine(self) -> RightsEngine:
        return RightsEngine(
            registrations=self.list_registrations(),
            root_grants=self.list_root_grants(),
            delegations=self.list_delegations(),
            legacy_document_ids=self.legacy_document_ids,
        )
