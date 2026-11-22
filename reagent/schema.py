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
``total - active_reserved - consumed - cleared``. Expired but unallocated
portions stay in that number; expiration only changes whether they are
*leadable today*, it never removes them from the conservation equation.
``cleared`` is a separate physical write-off destination maintained by
two-counter sign-off; it is never folded into ``consumed``.
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
    -- monotonic stocktake version; two counters must sign the same version
    inventory_version   INTEGER NOT NULL DEFAULT 0,
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

-- Container deposit facts.  A physical bottle carries exactly one immutable
-- supply credential (supplier / package model / integer deposit / return-by
-- day).  These are fictional accounting data for the exercise.
CREATE TABLE IF NOT EXISTS bottle_credentials (
    bottle_id          TEXT PRIMARY KEY REFERENCES bottles(bottle_id),
    supplier           TEXT NOT NULL,
    package_model      TEXT NOT NULL,
    deposit            INTEGER NOT NULL CHECK (deposit >= 0),
    latest_return_day  INTEGER NOT NULL CHECK (latest_return_day >= 0)
);

-- Physical write-off of portions that are still in the bottle but can no
-- longer be led because the bottle expired.  Two *different* counters must
-- sign the same inventory version and the exact remaining unallocated
-- quantity before a request becomes ``cleared``.  Cleared portions are a
-- fourth, separate destination: total = unallocated + reserved + consumed
-- + cleared.  They are never disguised as consumed.
CREATE TABLE IF NOT EXISTS clearance_requests (
    request_id         TEXT PRIMARY KEY,
    bottle_id          TEXT NOT NULL REFERENCES bottles(bottle_id),
    inventory_version  INTEGER NOT NULL,
    portions           INTEGER NOT NULL CHECK (portions > 0),
    status             TEXT NOT NULL CHECK (status IN ('pending', 'cleared')),
    created_day        INTEGER NOT NULL,
    cleared_day        INTEGER,
    CHECK ((status = 'pending' AND cleared_day IS NULL)
        OR (status = 'cleared' AND cleared_day IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS clearance_requests_bottle_idx
    ON clearance_requests(bottle_id, status);

CREATE TABLE IF NOT EXISTS clearance_signoffs (
    request_id          TEXT NOT NULL REFERENCES clearance_requests(request_id),
    counter_id          TEXT NOT NULL,
    day                 INTEGER NOT NULL,
    inventory_version   INTEGER NOT NULL,
    portions            INTEGER NOT NULL,
    PRIMARY KEY (request_id, counter_id)
);

-- Recycling-station trips and the box models each trip may buy.
CREATE TABLE IF NOT EXISTS trips (
    trip_id        TEXT PRIMARY KEY,
    departure_day  INTEGER NOT NULL CHECK (departure_day >= 0),
    arrival_day    INTEGER NOT NULL CHECK (arrival_day >= 0),
    max_boxes      INTEGER NOT NULL CHECK (max_boxes >= 0),
    CHECK (arrival_day >= departure_day)
);

CREATE TABLE IF NOT EXISTS box_models (
    model_id  TEXT PRIMARY KEY,
    slots     INTEGER NOT NULL CHECK (slots > 0)
);

CREATE TABLE IF NOT EXISTS box_model_kinds (
    model_id  TEXT NOT NULL REFERENCES box_models(model_id),
    kind      TEXT NOT NULL REFERENCES kinds(kind),
    PRIMARY KEY (model_id, kind)
);

-- Which bottle kinds a supplier will accept at all.
CREATE TABLE IF NOT EXISTS supplier_kinds (
    supplier  TEXT NOT NULL,
    kind      TEXT NOT NULL REFERENCES kinds(kind),
    PRIMARY KEY (supplier, kind)
);

-- What a given trip charges for a box model.  A model can be offered by
-- several trips at different prices.
CREATE TABLE IF NOT EXISTS trip_box_offers (
    trip_id   TEXT NOT NULL REFERENCES trips(trip_id),
    model_id  TEXT NOT NULL REFERENCES box_models(model_id),
    price     INTEGER NOT NULL CHECK (price >= 0),
    PRIMARY KEY (trip_id, model_id)
);

-- A refund application for one physical bottle.  Only one surviving voucher
-- may exist per bottle; a shipped voucher is locked forever and can never be
-- loaded onto another shipment.
CREATE TABLE IF NOT EXISTS vouchers (
    voucher_id         TEXT PRIMARY KEY,
    bottle_id          TEXT NOT NULL REFERENCES bottles(bottle_id),
    supplier           TEXT NOT NULL,
    package_model      TEXT NOT NULL,
    deposit            INTEGER NOT NULL CHECK (deposit >= 0),
    latest_return_day  INTEGER NOT NULL CHECK (latest_return_day >= 0),
    status             TEXT NOT NULL CHECK (status IN ('open', 'shipped')),
    created_day        INTEGER NOT NULL,
    shipped_day        INTEGER,
    shipment_id        TEXT,
    CHECK ((status = 'open' AND shipped_day IS NULL AND shipment_id IS NULL)
        OR (status = 'shipped' AND shipped_day IS NOT NULL
            AND shipment_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_surviving_voucher_per_bottle
    ON vouchers(bottle_id) WHERE status IN ('open', 'shipped');

CREATE TABLE IF NOT EXISTS shipments (
    shipment_id    TEXT PRIMARY KEY,
    day            INTEGER NOT NULL,
    deposit_total  INTEGER NOT NULL,
    box_cost       INTEGER NOT NULL,
    profit         INTEGER NOT NULL,
    bottle_count   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS shipped_boxes (
    box_id       TEXT PRIMARY KEY,
    shipment_id  TEXT NOT NULL REFERENCES shipments(shipment_id),
    trip_id      TEXT NOT NULL REFERENCES trips(trip_id),
    model_id     TEXT NOT NULL REFERENCES box_models(model_id),
    supplier     TEXT NOT NULL,
    price        INTEGER NOT NULL CHECK (price >= 0)
);

CREATE TABLE IF NOT EXISTS shipped_items (
    item_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    box_id        TEXT NOT NULL REFERENCES shipped_boxes(box_id),
    voucher_id    TEXT NOT NULL UNIQUE REFERENCES vouchers(voucher_id),
    bottle_id     TEXT NOT NULL REFERENCES bottles(bottle_id),
    deposit_paid  INTEGER NOT NULL CHECK (deposit_paid >= 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_shipment_per_bottle
    ON shipped_items(bottle_id);

CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- Allocation facts per bottle. ``unallocated`` is derived from the
-- reservation and clearance rows, so an expired-but-unallocated portion can
-- never be lost from the conservation equation.  ``cleared`` is its own
-- destination, never merged into ``consumed``:
--   total == unallocated + reserved + consumed + cleared
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
       COALESCE((SELECT SUM(c.portions) FROM clearance_requests c
                  WHERE c.bottle_id = b.bottle_id AND c.status = 'cleared'), 0)
           AS cleared,
       b.total_portions
         - COALESCE((SELECT SUM(r.portions) FROM reservations r
                      WHERE r.bottle_id = b.bottle_id AND r.status = 'active'), 0)
         - COALESCE((SELECT SUM(r.portions) FROM reservations r
                      WHERE r.bottle_id = b.bottle_id AND r.status = 'consumed'), 0)
         - COALESCE((SELECT SUM(c.portions) FROM clearance_requests c
                      WHERE c.bottle_id = b.bottle_id AND c.status = 'cleared'), 0)
           AS unallocated
  FROM bottles b;

-- Structural conservation: reserved + consumed + cleared can never exceed
-- total.
CREATE TRIGGER IF NOT EXISTS reservations_conservation_insert
BEFORE INSERT ON reservations
WHEN NEW.status <> 'released'
BEGIN
    SELECT RAISE(ABORT, 'reservation exceeds bottle total')
     WHERE NEW.portions
           + COALESCE((SELECT SUM(portions) FROM reservations
                        WHERE bottle_id = NEW.bottle_id
                          AND status IN ('active', 'consumed')), 0)
           + COALESCE((SELECT SUM(portions) FROM clearance_requests
                        WHERE bottle_id = NEW.bottle_id
                          AND status = 'cleared'), 0)
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
           + COALESCE((SELECT SUM(portions) FROM clearance_requests
                        WHERE bottle_id = NEW.bottle_id
                          AND status = 'cleared'), 0)
           > (SELECT total_portions FROM bottles
               WHERE bottle_id = NEW.bottle_id);
END;

-- Clearing physical portions can never exceed what is still unoccupied.
CREATE TRIGGER IF NOT EXISTS clearance_conservation_insert
BEFORE INSERT ON clearance_requests
BEGIN
    SELECT RAISE(ABORT, 'clearance exceeds unallocated portions')
     WHERE NEW.portions
           + COALESCE((SELECT SUM(portions) FROM reservations
                        WHERE bottle_id = NEW.bottle_id
                          AND status IN ('active', 'consumed')), 0)
           + COALESCE((SELECT SUM(portions) FROM clearance_requests
                        WHERE bottle_id = NEW.bottle_id
                          AND status = 'cleared'), 0)
           > (SELECT total_portions FROM bottles
               WHERE bottle_id = NEW.bottle_id);
END;

CREATE TRIGGER IF NOT EXISTS clearance_conservation_update
BEFORE UPDATE ON clearance_requests
WHEN NEW.status = 'cleared'
BEGIN
    SELECT RAISE(ABORT, 'clearance exceeds unallocated portions')
     WHERE NEW.portions
           + COALESCE((SELECT SUM(portions) FROM reservations
                        WHERE bottle_id = NEW.bottle_id
                          AND status IN ('active', 'consumed')), 0)
           + COALESCE((SELECT SUM(portions) FROM clearance_requests
                        WHERE bottle_id = NEW.bottle_id
                          AND request_id <> OLD.request_id
                          AND status = 'cleared'), 0)
           > (SELECT total_portions FROM bottles
               WHERE bottle_id = NEW.bottle_id);
END;

-- A cleared request is frozen; a pending one can only move to cleared.
CREATE TRIGGER IF NOT EXISTS clearance_terminal_immutable
BEFORE UPDATE ON clearance_requests
WHEN OLD.status = 'cleared'
BEGIN
    SELECT RAISE(ABORT, 'cleared request is immutable');
END;

CREATE TRIGGER IF NOT EXISTS clearance_terminal_no_delete
BEFORE DELETE ON clearance_requests
WHEN OLD.status = 'cleared'
BEGIN
    SELECT RAISE(ABORT, 'cleared request cannot be deleted');
END;

-- Sign-offs are append-only once their request is cleared.
CREATE TRIGGER IF NOT EXISTS clearance_signoff_no_delete
BEFORE DELETE ON clearance_signoffs
WHEN (SELECT status FROM clearance_requests
       WHERE request_id = OLD.request_id) = 'cleared'
BEGIN
    SELECT RAISE(ABORT, 'cleared sign-off cannot be deleted');
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

-- A supply credential is fixed with the physical bottle.
CREATE TRIGGER IF NOT EXISTS bottle_credentials_immutable
BEFORE UPDATE ON bottle_credentials
BEGIN
    SELECT RAISE(ABORT, 'bottle credential is immutable');
END;

-- A shipped voucher is locked: it may never reopen, move or be deleted.
CREATE TRIGGER IF NOT EXISTS vouchers_shipped_immutable
BEFORE UPDATE ON vouchers
WHEN OLD.status = 'shipped'
BEGIN
    SELECT RAISE(ABORT, 'shipped voucher is locked');
END;

CREATE TRIGGER IF NOT EXISTS vouchers_no_delete
BEFORE DELETE ON vouchers
BEGIN
    SELECT RAISE(ABORT, 'vouchers are never deleted');
END;

-- A shipment receipt is immutable once written.
CREATE TRIGGER IF NOT EXISTS shipments_immutable
BEFORE UPDATE ON shipments
BEGIN
    SELECT RAISE(ABORT, 'shipment receipt is immutable');
END;

CREATE TRIGGER IF NOT EXISTS shipments_no_delete
BEFORE DELETE ON shipments
BEGIN
    SELECT RAISE(ABORT, 'shipment receipt cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS shipped_items_immutable
BEFORE UPDATE ON shipped_items
BEGIN
    SELECT RAISE(ABORT, 'shipped item is immutable');
END;

CREATE TRIGGER IF NOT EXISTS shipped_items_no_delete
BEFORE DELETE ON shipped_items
BEGIN
    SELECT RAISE(ABORT, 'shipped item cannot be deleted');
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
