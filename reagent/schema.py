"""SQLite schema and connection helpers.

Design notes
------------
Allocation facts live in exactly one place: the ``reservations`` table, keyed
by a generic ``holder_id``. A run uses its ``run_id`` as holder, so the run
module consumes the very same rows the inventory entry points write; neither
side keeps a parallel quantity.

A reservation is either ``active`` (counts as reserved), ``consumed`` (counts
as consumed) or ``released`` (counts as neither, portions flow back to
unallocated).

``unallocated`` is derived on every read as
``total - active_reserved - consumed``. Expired but unallocated portions stay
in that number; expiration only changes whether they are *leadable today*, it
never removes them from the conservation equation.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional, Union

SCHEMA = """
CREATE TABLE IF NOT EXISTS kinds (
    kind                 TEXT PRIMARY KEY,
    open_lifetime_days   INTEGER NOT NULL CHECK (open_lifetime_days > 0)
);

CREATE TABLE IF NOT EXISTS bottles (
    bottle_id           TEXT PRIMARY KEY,
    kind                TEXT NOT NULL REFERENCES kinds(kind),
    total_portions      INTEGER NOT NULL CHECK (total_portions > 0),
    intake_day          INTEGER NOT NULL CHECK (intake_day >= 0),
    printed_expiry_day  INTEGER NOT NULL,
    opened_day          INTEGER,
    CHECK (printed_expiry_day > intake_day),
    CHECK (opened_day IS NULL OR (opened_day >= intake_day
                                  AND opened_day < printed_expiry_day))
);

CREATE TABLE IF NOT EXISTS methods (
    method_id   TEXT NOT NULL,
    revision    INTEGER NOT NULL CHECK (revision > 0),
    PRIMARY KEY (method_id, revision)
);

CREATE TABLE IF NOT EXISTS method_requirements (
    method_id   TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    kind        TEXT NOT NULL REFERENCES kinds(kind),
    portions    INTEGER NOT NULL CHECK (portions > 0),
    PRIMARY KEY (method_id, revision, kind),
    FOREIGN KEY (method_id, revision) REFERENCES methods(method_id, revision)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id               TEXT PRIMARY KEY,
    method_id            TEXT NOT NULL,
    method_revision      INTEGER NOT NULL,
    creation_day         INTEGER NOT NULL,
    -- demand snapshot at creation; later revisions never touch it
    demand_json          TEXT NOT NULL,
    state                TEXT NOT NULL CHECK (state IN (
                             'reserved', 'started', 'released')),
    failure_reason       TEXT,
    started_day          INTEGER,
    terminal_day         INTEGER,
    -- original terminal receipt; replayed verbatim, never recomputed
    terminal_json        TEXT,
    FOREIGN KEY (method_id, method_revision)
        REFERENCES methods(method_id, revision)
);

CREATE TABLE IF NOT EXISTS run_requirements (
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    kind        TEXT NOT NULL REFERENCES kinds(kind),
    portions    INTEGER NOT NULL CHECK (portions > 0),
    PRIMARY KEY (run_id, kind)
);

-- A reservation is an inventory fact identified by a generic holder. A run
-- uses its run_id as the holder; the inventory API can also be driven with an
-- independent holder. Runs reference facts through the holder join and never
-- store a private portion counter.
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id   TEXT PRIMARY KEY,
    holder_id        TEXT NOT NULL,
    bottle_id        TEXT NOT NULL REFERENCES bottles(bottle_id),
    portions         INTEGER NOT NULL CHECK (portions > 0),
    status           TEXT NOT NULL CHECK (status IN (
                          'active', 'consumed', 'released')),
    created_day      INTEGER NOT NULL,
    settled_day      INTEGER,
    release_reason   TEXT,
    CHECK ((status = 'active' AND settled_day IS NULL)
        OR (status IN ('consumed', 'released') AND settled_day IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS reservations_holder_idx
    ON reservations(holder_id, status);
CREATE INDEX IF NOT EXISTS reservations_bottle_idx
    ON reservations(bottle_id);
CREATE INDEX IF NOT EXISTS reservations_bottle_status_idx
    ON reservations(bottle_id, status);

CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- Allocation facts per bottle. ``unallocated`` is derived from the
-- reservation rows, so an expired-but-unallocated portion can never be lost
-- from the conservation equation.
CREATE VIEW IF NOT EXISTS bottle_allocation AS
SELECT b.bottle_id,
       b.kind,
       b.total_portions,
       COALESCE((SELECT SUM(r.portions) FROM reservations r
                  WHERE r.bottle_id = b.bottle_id AND r.status = 'active'), 0)
           AS reserved,
       COALESCE((SELECT SUM(r.portions) FROM reservations r
                  WHERE r.bottle_id = b.bottle_id AND r.status = 'consumed'), 0)
           AS consumed,
       b.total_portions
         - COALESCE((SELECT SUM(r.portions) FROM reservations r
                      WHERE r.bottle_id = b.bottle_id AND r.status = 'active'), 0)
         - COALESCE((SELECT SUM(r.portions) FROM reservations r
                      WHERE r.bottle_id = b.bottle_id AND r.status = 'consumed'), 0)
           AS unallocated
  FROM bottles b;

-- Structural conservation: reserved + consumed can never exceed total.
CREATE TRIGGER IF NOT EXISTS reservations_conservation_insert
BEFORE INSERT ON reservations
WHEN NEW.status <> 'released'
BEGIN
    SELECT RAISE(ABORT, 'reservation exceeds bottle total')
     WHERE NEW.portions
           + COALESCE((SELECT SUM(portions) FROM reservations
                        WHERE bottle_id = NEW.bottle_id
                          AND status IN ('active', 'consumed')), 0)
           > (SELECT total_portions FROM bottles
               WHERE bottle_id = NEW.bottle_id);
END;

CREATE TRIGGER IF NOT EXISTS reservations_conservation_update
BEFORE UPDATE ON reservations
WHEN NEW.status <> 'released'
BEGIN
    SELECT RAISE(ABORT, 'reservation exceeds bottle total')
     WHERE NEW.portions
           + COALESCE((SELECT SUM(portions) FROM reservations
                        WHERE bottle_id = NEW.bottle_id
                          AND reservation_id <> OLD.reservation_id
                          AND status IN ('active', 'consumed')), 0)
           > (SELECT total_portions FROM bottles
               WHERE bottle_id = NEW.bottle_id);
END;

-- A settled reservation (consumed/released) never changes again; its
-- quantities, bottle and status are all frozen.
CREATE TRIGGER IF NOT EXISTS reservations_terminal_immutable
BEFORE UPDATE ON reservations
WHEN OLD.status <> 'active'
BEGIN
    SELECT RAISE(ABORT, 'settled reservation is immutable');
END;

CREATE TRIGGER IF NOT EXISTS reservations_terminal_no_delete
BEFORE DELETE ON reservations
WHEN OLD.status <> 'active'
BEGIN
    SELECT RAISE(ABORT, 'settled reservation cannot be deleted');
END;

-- A run's terminal state and its demand snapshot are frozen once terminal.
CREATE TRIGGER IF NOT EXISTS runs_terminal_immutable
BEFORE UPDATE ON runs
WHEN OLD.state IN ('started', 'released')
BEGIN
    SELECT RAISE(ABORT, 'terminal run is immutable');
END;

CREATE TRIGGER IF NOT EXISTS run_requirements_immutable
BEFORE UPDATE ON run_requirements
BEGIN
    SELECT RAISE(ABORT, 'run requirements are immutable after creation');
END;

CREATE TRIGGER IF NOT EXISTS run_requirements_no_delete
BEFORE DELETE ON run_requirements
BEGIN
    SELECT RAISE(ABORT, 'run requirements are immutable after creation');
END;

-- Opening day is monotonic: once opened it can never move or be cleared.
CREATE TRIGGER IF NOT EXISTS bottles_open_immutable
BEFORE UPDATE OF opened_day ON bottles
WHEN OLD.opened_day IS NOT NULL
 AND (NEW.opened_day IS NULL OR NEW.opened_day <> OLD.opened_day)
BEGIN
    SELECT RAISE(ABORT, 'opened_day is immutable');
END;

-- ---------------------------------------------------------------------------
-- Whole-bottle seals + offline co-signing (封单 / 会签)
-- ---------------------------------------------------------------------------
-- A seat is a named signer position. ``ordinary`` seats are single-owner and
-- therefore guarantee at most one settlement per seal; ``deputy`` seats carry
-- an independent paper book and two windows may each hold a copy.
CREATE TABLE IF NOT EXISTS seats (
    seat_id     TEXT PRIMARY KEY,
    seat_type   TEXT NOT NULL CHECK (seat_type IN ('ordinary', 'deputy'))
);

-- A seal freezes a whole-bottle handover manifest: the transitive run/bottle
-- closure, each run's witness seats, and the exact reservation rows. Sealing
-- never deducts inventory. Storing the reservation ids lets a later release
-- plus re-reservation of the same amount be detected as stale.
CREATE TABLE IF NOT EXISTS seals (
    seal_id                TEXT PRIMARY KEY,
    created_day            INTEGER NOT NULL,
    designated_runs_json   TEXT NOT NULL,
    runs_json              TEXT NOT NULL,
    bottles_json           TEXT NOT NULL,
    witnesses_json         TEXT NOT NULL,
    reservations_json      TEXT NOT NULL,
    report_json            TEXT NOT NULL
);

-- Historical groups that may still be effective. Once ``recalled`` is set,
-- the group stops constraining a new scheme; recall never reverses.
CREATE TABLE IF NOT EXISTS historical_groups (
    group_id    TEXT PRIMARY KEY,
    seats_json  TEXT NOT NULL,
    created_day INTEGER NOT NULL,
    recalled    INTEGER NOT NULL DEFAULT 0 CHECK (recalled IN (0, 1))
);

-- A co-signing scheme: the candidate groups plus the computed activation.
CREATE TABLE IF NOT EXISTS schemes (
    scheme_id    TEXT PRIMARY KEY,
    seal_id      TEXT NOT NULL REFERENCES seals(seal_id),
    created_day  INTEGER NOT NULL,
    groups_json  TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    feasible     INTEGER NOT NULL CHECK (feasible IN (0, 1)),
    total_fee    INTEGER
);

-- Formal settlement postings. At most one settlement per seal (unique index),
-- and a repeated (seal, settlement_no) is deduplicated on read.
CREATE TABLE IF NOT EXISTS postings (
    posting_id      TEXT PRIMARY KEY,
    seal_id         TEXT NOT NULL REFERENCES seals(seal_id),
    scheme_id       TEXT NOT NULL REFERENCES schemes(scheme_id),
    settlement_no   TEXT NOT NULL,
    group_name      TEXT NOT NULL,
    signatures_json TEXT NOT NULL,
    posted_day      INTEGER NOT NULL,
    UNIQUE (seal_id, settlement_no)
);

CREATE UNIQUE INDEX IF NOT EXISTS postings_one_per_seal
    ON postings(seal_id);

CREATE TRIGGER IF NOT EXISTS seals_immutable
BEFORE UPDATE ON seals
BEGIN
    SELECT RAISE(ABORT, 'seal is immutable');
END;

CREATE TRIGGER IF NOT EXISTS seals_no_delete
BEFORE DELETE ON seals
BEGIN
    SELECT RAISE(ABORT, 'seal is immutable');
END;

CREATE TRIGGER IF NOT EXISTS schemes_immutable
BEFORE UPDATE ON schemes
BEGIN
    SELECT RAISE(ABORT, 'scheme is immutable');
END;

CREATE TRIGGER IF NOT EXISTS schemes_no_delete
BEFORE DELETE ON schemes
BEGIN
    SELECT RAISE(ABORT, 'scheme is immutable');
END;

CREATE TRIGGER IF NOT EXISTS postings_immutable
BEFORE UPDATE ON postings
BEGIN
    SELECT RAISE(ABORT, 'posting is immutable');
END;

CREATE TRIGGER IF NOT EXISTS postings_no_delete
BEFORE DELETE ON postings
BEGIN
    SELECT RAISE(ABORT, 'posting is immutable');
END;

-- Recall is monotonic and the seat set of a historical group is frozen.
CREATE TRIGGER IF NOT EXISTS historical_groups_seats_immutable
BEFORE UPDATE OF seats_json, created_day ON historical_groups
BEGIN
    SELECT RAISE(ABORT, 'historical group seats are immutable');
END;

CREATE TRIGGER IF NOT EXISTS historical_groups_recall_monotonic
BEFORE UPDATE OF recalled ON historical_groups
WHEN OLD.recalled = 1 AND NEW.recalled <> 1
BEGIN
    SELECT RAISE(ABORT, 'historical group recall cannot be undone');
END;
"""


def connect(path: Union[str, Path]) -> sqlite3.Connection:
    """Open a connection with the isolation / pragma settings we rely on."""
    path = str(path)
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection, None, None]:
    """``BEGIN IMMEDIATE`` transaction, or a savepoint-less nested join.

    ``BEGIN IMMEDIATE`` takes the write lock up front. Two competing
    connections therefore serialize on the same last portion: the loser waits
    (``busy_timeout``) until the winner commits, then re-reads availability and
    fails cleanly instead of over-allocating.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
