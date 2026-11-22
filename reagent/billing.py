"""Billing service: point vouchers, true-usage credentials and period bills.

The billing clerk works strictly from the inventory side's reservation rows:

* only ``consumed`` rows produce credentials, split into one credential per
  portion and identified by ``(reservation_id, seq)``;
* ``released`` rows, still-``active`` (reserved-only) rows and failed replays
  produce nothing;
* runs and ordinary holders both book under their registered holder id;
* voucher validity is judged against the real settlement day
  (``reservations.settled_day``), never the reservation day.

One bill per period is active at any time. Reopening an open period supersedes
its bill while keeping the old receipt; a closed bill's write-offs never move
and keep their vouchers locked. Unused vouchers are not refunded and an
unexpired voucher is never cancelled just because a period closes.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional, Sequence

from .billing_match import Claim, Voucher, optimize_redemptions
from .errors import Conflict, NotFoundError, ValidationError
from .rules import require_day, require_int, require_text
from .schema import transaction

MAX_CLAIMS_PER_BILL = 200
MAX_VOUCHERS_PER_BILL = 200

ACTIVE = "active"
CLOSED = "closed"
SUPERSEDED = "superseded"


class BillingService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- periods -------------------------------------------------------------
    def open_period(self, period_id: str, day: int) -> dict:
        period_id = require_text(period_id, "period_id")
        require_day(day)
        with transaction(self.conn):
            if self.conn.execute(
                "SELECT 1 FROM billing_periods WHERE period_id = ?", (period_id,)
            ).fetchone():
                raise Conflict(f"period {period_id!r} already exists")
            self.conn.execute(
                "INSERT INTO billing_periods(period_id, created_day) VALUES(?, ?)",
                (period_id, day),
            )
            return {"period_id": period_id, "created_day": day, "status": "open"}

    # -- supplier list prices ------------------------------------------------
    def add_price(
        self, period_id: str, bottle_id: str, price: int, day: Optional[int] = None
    ) -> dict:
        require_text(period_id, "period_id")
        require_text(bottle_id, "bottle_id")
        price = require_int(price, "price", minimum=0)
        if day is not None:
            require_day(day)
        self._require_period(period_id)
        self._require_bottle(bottle_id)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO bottle_prices(period_id, bottle_id, day, price) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(period_id, bottle_id, day) "
                "DO UPDATE SET price = excluded.price",
                (period_id, bottle_id, day, price),
            )
        return {"period_id": period_id, "bottle_id": bottle_id, "day": day,
                "price": price}

    # -- vouchers -------------------------------------------------------------
    def issue_voucher(self, doc: dict) -> dict:
        voucher_id = require_text(doc.get("voucher_id"), "voucher_id")
        owner = require_text(doc.get("owner_counter"), "owner_counter")
        paid = require_int(doc.get("paid_price"), "paid_price", minimum=0)
        cap = require_int(doc.get("cap_amount"), "cap_amount", minimum=1)
        valid_from = require_day(doc.get("valid_from_day"), "valid_from_day")
        valid_until = require_day(doc.get("valid_until_day"), "valid_until_day")
        if valid_from >= valid_until:
            raise ValidationError(
                f"voucher {voucher_id}: valid_from_day must be < valid_until_day"
            )
        bottles = _require_text_list(doc.get("bottles"), "bottles")
        counters = _require_text_list(doc.get("counters"), "counters")
        with transaction(self.conn):
            if self.conn.execute(
                "SELECT 1 FROM vouchers WHERE voucher_id = ?", (voucher_id,)
            ).fetchone():
                raise Conflict(f"voucher {voucher_id!r} already exists")
            for bottle_id in bottles:
                self._require_bottle(bottle_id)
            self.conn.execute(
                "INSERT INTO vouchers(voucher_id, owner_counter, paid_price, "
                "cap_amount, valid_from_day, valid_until_day) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (voucher_id, owner, paid, cap, valid_from, valid_until),
            )
            for bottle_id in bottles:
                self.conn.execute(
                    "INSERT INTO voucher_bottles(voucher_id, bottle_id) "
                    "VALUES(?, ?)",
                    (voucher_id, bottle_id),
                )
            for counter_id in counters:
                self.conn.execute(
                    "INSERT INTO voucher_counters(voucher_id, counter_id) "
                    "VALUES(?, ?)",
                    (voucher_id, counter_id),
                )
        return self.get_voucher(voucher_id)

    def get_voucher(self, voucher_id: str) -> dict:
        require_text(voucher_id, "voucher_id")
        row = self.conn.execute(
            "SELECT * FROM vouchers WHERE voucher_id = ?", (voucher_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown voucher {voucher_id!r}")
        bottles = [
            r["bottle_id"]
            for r in self.conn.execute(
                "SELECT bottle_id FROM voucher_bottles WHERE voucher_id = ? "
                "ORDER BY bottle_id",
                (voucher_id,),
            )
        ]
        counters = [
            r["counter_id"]
            for r in self.conn.execute(
                "SELECT counter_id FROM voucher_counters WHERE voucher_id = ? "
                "ORDER BY counter_id",
                (voucher_id,),
            )
        ]
        return {
            "voucher_id": row["voucher_id"],
            "owner_counter": row["owner_counter"],
            "paid_price": row["paid_price"],
            "cap_amount": row["cap_amount"],
            "valid_from_day": row["valid_from_day"],
            "valid_until_day": row["valid_until_day"],
            "bottles": bottles,
            "counters": counters,
            "locked": self._voucher_locked(row["voucher_id"]),
        }

    # -- credentials ----------------------------------------------------------
    def load_credentials(self, period_id: str) -> list[Claim]:
        """All un-locked true-usage credentials visible to this period.

        A consumed reservation of ``p`` portions becomes ``p`` credentials
        ``seq = 0 .. p-1``. Credentials already carried by an active or closed
        bill are skipped; superseded bills release theirs again.
        """
        self._require_period(period_id)
        rows = self.conn.execute(
            """
            SELECT r.reservation_id, r.holder_id, r.bottle_id, r.portions,
                   r.settled_day
              FROM reservations r
             WHERE r.status = 'consumed'
             ORDER BY r.reservation_id
            """
        ).fetchall()
        locked = self._locked_claim_keys()
        claims: list[Claim] = []
        for row in rows:
            for seq in range(int(row["portions"])):
                if (row["reservation_id"], seq) in locked:
                    continue
                settle_day = int(row["settled_day"])
                claims.append(
                    Claim(
                        reservation_id=row["reservation_id"],
                        seq=seq,
                        counter_id=row["holder_id"],
                        bottle_id=row["bottle_id"],
                        settle_day=settle_day,
                        list_price=self._price(
                            period_id, row["bottle_id"], settle_day
                        ),
                    )
                )
        return claims

    # -- bills ----------------------------------------------------------------
    def issue_bill(
        self,
        period_id: str,
        issue_day: int,
        credentials: Optional[Sequence[Claim]] = None,
    ) -> dict:
        """Issue (or replay) the single bill for an open period."""
        require_text(period_id, "period_id")
        require_day(issue_day)
        with transaction(self.conn):
            self._require_period(period_id)
            existing = self._active_bill_row(period_id)
            if existing is not None:
                receipt = json.loads(existing["receipt_json"])
                receipt["replayed"] = True
                return receipt

            claims = (
                list(credentials)
                if credentials is not None
                else self.load_credentials(period_id)
            )
            self._validate_claims(period_id, claims)
            vouchers = self._available_vouchers()
            if len(claims) > MAX_CLAIMS_PER_BILL:
                raise ValidationError(
                    f"period {period_id}: {len(claims)} credentials exceed "
                    f"{MAX_CLAIMS_PER_BILL} per bill"
                )
            if len(vouchers) > MAX_VOUCHERS_PER_BILL:
                raise ValidationError(
                    f"period {period_id}: {len(vouchers)} vouchers exceed "
                    f"{MAX_VOUCHERS_PER_BILL} per bill"
                )
            chosen = optimize_redemptions(claims, vouchers)
            by_id = {v.voucher_id: v for v in vouchers}
            lines = []
            for claim, voucher_id in zip(sorted(claims, key=lambda c: c.key), chosen):
                lines.append(
                    self._build_line(claim, by_id.get(voucher_id) if voucher_id else None)
                )

            version = self._next_version(period_id)
            bill_id = f"{period_id}#v{version}"
            receipt = self._receipt(bill_id, period_id, version, issue_day, lines)
            self.conn.execute(
                "INSERT INTO bills(period_id, version, status, issue_day, "
                "receipt_json) VALUES(?, ?, 'active', ?, ?)",
                (period_id, version, issue_day, json.dumps(receipt, sort_keys=True)),
            )
            for line in lines:
                self.conn.execute(
                    "INSERT INTO bill_lines(bill_id, period_id, reservation_id, "
                    "seq, counter_id, bottle_id, settle_day, list_price, "
                    "voucher_id, offset_amount, cash_to_supplier, repay_to, "
                    "repay_amount) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        bill_id,
                        period_id,
                        line["reservation_id"],
                        line["seq"],
                        line["counter_id"],
                        line["bottle_id"],
                        line["settle_day"],
                        line["list_price"],
                        line["voucher_id"],
                        line["offset_amount"],
                        line["cash_to_supplier"],
                        line["repay_to"],
                        line["repay_amount"],
                    ),
                )
            receipt["replayed"] = False
            return receipt

    def get_bill(self, period_id: str) -> dict:
        row = self._active_bill_row(period_id)
        if row is None:
            raise NotFoundError(f"period {period_id!r} has no active bill")
        return json.loads(row["receipt_json"])

    def close_period(self, period_id: str) -> dict:
        with transaction(self.conn):
            row = self._active_bill_row(period_id)
            if row is None:
                raise Conflict(f"period {period_id!r} has no bill to close")
            self.conn.execute(
                "UPDATE bills SET status = 'closed' WHERE period_id = ?",
                (period_id,),
            )
            return {"period_id": period_id, "status": CLOSED}

    def reopen_period(self, period_id: str) -> dict:
        """Reopen an old period: its active bill is superseded, facts kept.

        Closed periods are refused: their write-offs are frozen.
        """
        with transaction(self.conn):
            row = self._active_bill_row(period_id)
            if row is None:
                raise Conflict(f"period {period_id!r} has no open bill")
            if row["status"] == CLOSED:
                raise Conflict(f"period {period_id!r} is closed and immutable")
            self.conn.execute(
                "UPDATE bills SET status = 'superseded' WHERE period_id = ?",
                (period_id,),
            )
            return {"period_id": period_id, "status": "reopened",
                    "superseded_version": row["version"]}

    def bill_history(self, period_id: str) -> list[dict]:
        self._require_period(period_id)
        return [
            {"bill_id": f"{period_id}#v{r['version']}", "version": r["version"],
             "status": r["status"], "issue_day": r["issue_day"],
             "receipt": json.loads(r["receipt_json"])}
            for r in self.conn.execute(
                "SELECT version, status, issue_day, receipt_json FROM bills "
                "WHERE period_id = ? ORDER BY version",
                (period_id,),
            )
        ]

    # -- settlement totals ----------------------------------------------------
    def settlement(self, period_id: str) -> dict:
        """Supplier receivable plus each counter's net payable/receivable."""
        row = self._active_bill_row(period_id)
        if row is None:
            raise NotFoundError(f"period {period_id!r} has no active bill")
        lines = self.conn.execute(
            "SELECT * FROM bill_lines WHERE period_id = ?", (period_id,)
        ).fetchall()
        supplier = 0
        net: dict[str, int] = {}
        for line in lines:
            supplier += int(line["cash_to_supplier"])
            counter = line["counter_id"]
            net[counter] = net.get(counter, 0) + int(line["cash_to_supplier"])
            if line["repay_to"] is not None:
                net[counter] = net.get(counter, 0) + int(line["repay_amount"])
                owner = line["repay_to"]
                net[owner] = net.get(owner, 0) - int(line["repay_amount"])
        return {
            "period_id": period_id,
            "supplier_receivable": supplier,
            "counter_net": {k: net[k] for k in sorted(net)},
        }

    def voucher_status(self) -> list[dict]:
        """Report every voucher with lock state; expired ones are not deleted."""
        out = []
        for row in self.conn.execute("SELECT voucher_id FROM vouchers ORDER BY voucher_id"):
            doc = self.get_voucher(row["voucher_id"])
            out.append(doc)
        return out

    # -- internals ------------------------------------------------------------
    def _build_line(self, claim: Claim, voucher: Optional[Voucher]) -> dict:
        line = {
            "reservation_id": claim.reservation_id,
            "seq": claim.seq,
            "counter_id": claim.counter_id,
            "bottle_id": claim.bottle_id,
            "settle_day": claim.settle_day,
            "list_price": claim.list_price,
            "voucher_id": None,
            "offset_amount": 0,
            "cash_to_supplier": claim.list_price,
            "repay_to": None,
            "repay_amount": 0,
        }
        if voucher is None:
            return line
        offset = voucher.offset_for(claim)
        line["voucher_id"] = voucher.voucher_id
        line["offset_amount"] = offset
        line["cash_to_supplier"] = claim.list_price - offset
        if voucher.owner_counter != claim.counter_id:
            line["repay_to"] = voucher.owner_counter
            line["repay_amount"] = voucher.paid_price
        return line

    def _receipt(
        self,
        bill_id: str,
        period_id: str,
        version: int,
        issue_day: int,
        lines: list[dict],
    ) -> dict:
        supplier = sum(line["cash_to_supplier"] for line in lines)
        total_offset = sum(line["offset_amount"] for line in lines)
        return {
            "bill_id": bill_id,
            "period_id": period_id,
            "version": version,
            "issue_day": issue_day,
            "status": ACTIVE,
            "lines": lines,
            "total_list_price": sum(line["list_price"] for line in lines),
            "total_offset": total_offset,
            "supplier_receivable": supplier,
            "voucher_sequence": [line["voucher_id"] for line in lines],
        }

    def _validate_claims(self, period_id: str, claims: list[Claim]) -> None:
        if not claims:
            raise ValidationError(f"period {period_id}: no true-usage credentials")
        keys = set()
        locked = self._locked_claim_keys()
        for claim in claims:
            if claim.key in keys:
                raise ValidationError(f"duplicate credential {claim.key}")
            keys.add(claim.key)
            if claim.key in locked:
                raise Conflict(
                    f"credential {claim.key} is already written off by an "
                    f"active or closed bill"
                )
            row = self.conn.execute(
                "SELECT status, settled_day, holder_id FROM reservations "
                "WHERE reservation_id = ?",
                (claim.reservation_id,),
            ).fetchone()
            if row is None or row["status"] != "consumed":
                raise ValidationError(
                    f"credential {claim.key}: reservation must be a consumed "
                    f"true usage"
                )
            if int(row["settled_day"]) != claim.settle_day:
                raise ValidationError(
                    f"credential {claim.key}: settle day disagrees with the "
                    f"stored reservation"
                )
            if row["holder_id"] != claim.counter_id:
                raise ValidationError(
                    f"credential {claim.key}: counter must be the registered "
                    f"holder {row['holder_id']!r}"
                )
            if claim.list_price != self._price(
                period_id, claim.bottle_id, claim.settle_day
            ):
                raise ValidationError(
                    f"credential {claim.key}: list price disagrees with the "
                    f"period price table"
                )

    def _available_vouchers(self) -> list[Voucher]:
        rows = self.conn.execute(
            "SELECT * FROM vouchers ORDER BY voucher_id"
        ).fetchall()
        vouchers = []
        for row in rows:
            if self._voucher_locked(row["voucher_id"]):
                continue
            bottles = frozenset(
                r["bottle_id"]
                for r in self.conn.execute(
                    "SELECT bottle_id FROM voucher_bottles WHERE voucher_id = ?",
                    (row["voucher_id"],),
                )
            )
            counters = frozenset(
                r["counter_id"]
                for r in self.conn.execute(
                    "SELECT counter_id FROM voucher_counters WHERE voucher_id = ?",
                    (row["voucher_id"],),
                )
            )
            vouchers.append(
                Voucher(
                    voucher_id=row["voucher_id"],
                    owner_counter=row["owner_counter"],
                    paid_price=int(row["paid_price"]),
                    cap_amount=int(row["cap_amount"]),
                    valid_from_day=int(row["valid_from_day"]),
                    valid_until_day=int(row["valid_until_day"]),
                    bottles=bottles,
                    counters=counters,
                )
            )
        return vouchers

    def _price(self, period_id: str, bottle_id: str, day: int) -> int:
        row = self.conn.execute(
            "SELECT price FROM bottle_prices WHERE period_id = ? AND bottle_id = ? "
            "AND day = ?",
            (period_id, bottle_id, day),
        ).fetchone()
        if row is not None:
            return int(row["price"])
        row = self.conn.execute(
            "SELECT price FROM bottle_prices WHERE period_id = ? AND bottle_id = ? "
            "AND day IS NULL",
            (period_id, bottle_id),
        ).fetchone()
        if row is None:
            raise ValidationError(
                f"no list price for bottle {bottle_id!r} on day {day} in "
                f"period {period_id!r}"
            )
        return int(row["price"])

    def _locked_claim_keys(self) -> set[tuple[str, int]]:
        rows = self.conn.execute(
            """
            SELECT bl.reservation_id, bl.seq
              FROM bill_lines bl JOIN bills b ON b.period_id = bl.period_id
             WHERE b.status IN ('active', 'closed')
            """
        ).fetchall()
        return {(r["reservation_id"], int(r["seq"])) for r in rows}

    def _voucher_locked(self, voucher_id: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1
              FROM bill_lines bl JOIN bills b ON b.period_id = bl.period_id
             WHERE bl.voucher_id = ? AND b.status IN ('active', 'closed')
             LIMIT 1
            """,
            (voucher_id,),
        ).fetchone()
        return row is not None

    def _active_bill_row(self, period_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM bills WHERE period_id = ? AND status IN "
            "('active', 'closed') ORDER BY version DESC LIMIT 1",
            (period_id,),
        ).fetchone()

    def _next_version(self, period_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM bills WHERE period_id = ?",
            (period_id,),
        ).fetchone()
        return int(row["v"]) + 1

    def _require_period(self, period_id: str) -> None:
        if self.conn.execute(
            "SELECT 1 FROM billing_periods WHERE period_id = ?", (period_id,)
        ).fetchone() is None:
            raise NotFoundError(f"unknown period {period_id!r}")

    def _require_bottle(self, bottle_id: str) -> None:
        if self.conn.execute(
            "SELECT 1 FROM bottles WHERE bottle_id = ?", (bottle_id,)
        ).fetchone() is None:
            raise NotFoundError(f"unknown bottle {bottle_id!r}")


def _require_text_list(raw: object, name: str) -> list[str]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValidationError(f"{name} must be a non-empty list")
    out = []
    for item in raw:
        text = require_text(item, f"{name} entry")
        if text in out:
            raise ValidationError(f"duplicate {name} entry {text!r}")
        out.append(text)
    return out
