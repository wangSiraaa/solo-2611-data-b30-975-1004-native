#!/usr/bin/env python3
"""Point-voucher billing and remittance clearing demo (reopenable real books).

One physical fact set drives the three fixed outcomes:

1. day-10 true usage inside the voucher window -> the voucher redeems, B
   repays owner A the paid price, the supplier receives zero for that portion;
2. the same bottle consumed on day 11 -> the window ``[9, 11)`` has ended, so
   the voucher is useless, B pays the full list price and A absorbs its
   expired prepayment;
3. day-10 credentials are restored through the reopenable books: the open
   bill is superseded (its receipt is kept) and re-issued, then the direct
   B -> A line is shut so the repayment transships two hops through C.

One shared database also re-checks the credential rules:

* an ordinary holder and a run both produce credentials under their
  registered holder id;
* a released-only reservation and a reserved-only row produce none;
* a cross-month voucher stays usable because its window has not ended;
* replaying a bill returns the identical bill id and voucher sequence.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reagent.billing import BillingService  # noqa: E402
from reagent.clearing import Route, settle  # noqa: E402
from reagent.inventory import InventoryService  # noqa: E402
from reagent.runs import RunService  # noqa: E402
from reagent.schema import connect, initialize  # noqa: E402

WINDOW_FROM, WINDOW_UNTIL = 9, 11  # half-open [9, 11): days 9 and 10 only
LIST_PRICE = 5
PAID_PRICE = 3
CAP = 5


def build_store(db_path: str):
    document = {
        "initial_day": 1,
        "kinds": [{"kind": "R", "open_lifetime_days": 1000}],
        "bottles": [
            {"bottle_id": "X", "kind": "R", "total_portions": 8, "intake_day": 1,
             "printed_expiry_day": 400, "opened_day": None},
            {"bottle_id": "Y", "kind": "R", "total_portions": 2, "intake_day": 20,
             "printed_expiry_day": 400, "opened_day": None},
        ],
        "methods": [{"method_id": "M", "revision": 1, "requirements": {"R": 1}}],
    }
    conn = connect(db_path)
    initialize(conn)
    inventory = InventoryService(conn)
    inventory.initialize(document)
    return conn, inventory, RunService(conn), BillingService(conn)


def issue_named_voucher(billing: BillingService, voucher_id: str) -> None:
    billing.issue_voucher(
        {
            "voucher_id": voucher_id,
            "owner_counter": "A",
            "paid_price": PAID_PRICE,
            "cap_amount": CAP,
            "valid_from_day": WINDOW_FROM,
            "valid_until_day": WINDOW_UNTIL,
            "bottles": ["X"],
            "counters": ["B"],
        }
    )


def print_bill(title: str, billing: BillingService, period_id: str) -> dict:
    receipt = billing.get_bill(period_id)
    print(f"\n== {title} ==")
    print(f"bill {receipt['bill_id']} status={receipt['status']}")
    for line in receipt["lines"]:
        print(
            "  credential {reservation_id}#{seq} holder={counter_id} "
            "day={settle_day} price={list_price} voucher={voucher_id} "
            "offset={offset_amount} cash={cash_to_supplier} "
            "repay={repay_to}:{repay_amount}".format(**line)
        )
    totals = billing.settlement(period_id)
    print("  supplier_receivable =", totals["supplier_receivable"],
          " counter_net =", totals["counter_net"])
    return receipt


def show_clearing(title: str, balances: dict, routes: list) -> None:
    print(f"\n-- clearing: {title} --")
    result = settle(balances, routes)
    if not result.feasible:
        print("  no full settlement, no half-batch instructions:", result.reason)
        return
    print("  minimum activation fee =", result.total_activation_fee)
    for ins in result.instructions:
        print(f"  {ins.source} -> {ins.target} amount={ins.amount} "
              f"(line {ins.route_id}, fee {ins.activation_fee})")


def main() -> int:
    handle, db_path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    try:
        conn, inventory, runs, billing = build_store(db_path)

        # ------------------------------------------------------------------
        # All day-10 physical facts first (the business clock never rewinds).
        # ------------------------------------------------------------------
        # Ordinary holder B reserves on day 9, truly consumes on day 10.
        inventory.hold("B", [("X", 1)], 9)
        inventory.consume_held("B", 10)
        # A run consumes under its run_id holder, proving runs and ordinary
        # holders both book as the registered holder.
        runs.create_run("run-1", "M", 1, 10, [{"bottle_id": "X", "portions": 1}])
        runs.start_run("run-1", 10)
        # Two more day-10 true usages back the reopenable period (outcome 3).
        inventory.hold("B3", [("X", 1)], 10)
        inventory.consume_held("B3", 10)
        inventory.hold("B4", [("X", 1)], 10)
        inventory.consume_held("B4", 10)
        # Released-only and reserved-only rows must never become credentials.
        inventory.hold("REL", [("X", 1)], 10)
        inventory.release_held("REL", 10, "abort")
        inventory.hold("RSV", [("X", 1)], 10)

        billing.open_period("p1", 30)
        billing.add_price("p1", "X", LIST_PRICE)
        issue_named_voucher(billing, "V1")

        # Credential hygiene is checked BEFORE any bill locks facts.
        all_credentials = billing.load_credentials("p1")
        print("true-usage credentials (released/reserved excluded):",
              [(c.reservation_id, c.seq, c.counter_id, c.settle_day)
               for c in all_credentials])
        assert sorted((c.reservation_id, c.seq) for c in all_credentials) == [
            ("B#0", 0), ("B3#0", 0), ("B4#0", 0), ("run-1#0", 0)
        ]
        # p1 writes off only B's credential, leaving B3/B4 for the reopenable
        # period p3 and the run credential to demonstrate an unused claim.
        b_credentials = [c for c in all_credentials if c.counter_id == "B"]

        # ------------------------------------------------------------------
        # Outcome 1: voucher valid on day 10, direct B -> A line available.
        # ------------------------------------------------------------------
        first = billing.issue_bill("p1", 30, credentials=b_credentials)
        print_bill("outcome 1: day-10 usage, voucher valid", billing, "p1")
        b_line = next(line for line in first["lines"] if line["reservation_id"] == "B#0")
        assert b_line["voucher_id"] == "V1"
        assert b_line["cash_to_supplier"] == 0
        assert b_line["repay_to"] == "A" and b_line["repay_amount"] == 3

        # Replay returns the identical bill id and voucher sequence.
        replay = billing.issue_bill("p1", 30)
        assert replay["replayed"] is True
        assert replay["bill_id"] == first["bill_id"]
        assert replay["voucher_sequence"] == first["voucher_sequence"]
        print("replay identical:", replay["bill_id"], replay["voucher_sequence"])

        # A closed bill's write-offs are frozen and cannot be reopened.
        billing.close_period("p1")
        try:
            billing.reopen_period("p1")
        except Exception as exc:  # noqa: BLE001
            print("closed period immutable ->", str(exc))
        else:
            raise AssertionError("closed period must not reopen")

        # ------------------------------------------------------------------
        # Outcome 3 (billing half): reopenable books. A separate open period
        # owns the B3/B4 day-10 credentials. Its first bill is superseded and
        # re-issued, while the closed p1 write-offs stay frozen.
        # ------------------------------------------------------------------
        billing.open_period("p3", 30)
        billing.add_price("p3", "X", LIST_PRICE)
        billing.issue_voucher(
            {
                "voucher_id": "V5", "owner_counter": "A", "paid_price": 3,
                "cap_amount": 5, "valid_from_day": WINDOW_FROM,
                "valid_until_day": WINDOW_UNTIL,
                "bottles": ["X"], "counters": ["B3", "B4"],
            }
        )
        first3 = billing.issue_bill(
            "p3", 30,
            credentials=[c for c in billing.load_credentials("p3")
                         if c.counter_id in ("B3", "B4")],
        )
        history_before = len(billing.bill_history("p3"))
        billing.reopen_period("p3")
        billing.issue_bill(
            "p3", 30,
            credentials=[c for c in billing.load_credentials("p3")
                         if c.counter_id in ("B3", "B4")],
        )  # re-runs the optimizer over unlocked facts
        history = billing.bill_history("p3")
        assert len(history) == history_before + 1
        assert history[0]["status"] == "superseded"
        receipt3 = print_bill(
            "outcome 3: reopened bill (old receipt superseded, facts kept)",
            billing, "p3",
        )
        assert receipt3["version"] == 2
        assert history[0]["receipt"]["voucher_sequence"] == first3["voucher_sequence"]
        print("bill history:", [(h["bill_id"], h["status"]) for h in history])
        active_locked = {v["voucher_id"] for v in billing.voucher_status() if v["locked"]}
        print("vouchers locked by active/closed bills:", sorted(active_locked))

        # ------------------------------------------------------------------
        # Outcome 2: day-11 true usage; the bottle is valid but the voucher
        # window [9, 11) has ended, so the voucher is useless.
        # ------------------------------------------------------------------
        billing.open_period("p2", 40)
        billing.add_price("p2", "X", LIST_PRICE)
        issue_named_voucher(billing, "V2")
        inventory.hold("B2", [("X", 1)], 11)
        inventory.consume_held("B2", 11)
        billing.issue_bill(
            "p2", 40,
            credentials=[c for c in billing.load_credentials("p2")
                         if c.counter_id == "B2"],
        )
        receipt2 = print_bill("outcome 2: day-11 usage, voucher expired", billing, "p2")
        line2 = receipt2["lines"][0]
        assert line2["voucher_id"] is None
        assert line2["cash_to_supplier"] == LIST_PRICE
        assert billing.settlement("p2")["supplier_receivable"] == LIST_PRICE
        print("V2 stays on file unused: no refund, A absorbs the prepayment")

        # ------------------------------------------------------------------
        # Cross-month voucher: window spans the month boundary, never cancelled.
        # ------------------------------------------------------------------
        billing.open_period("p-cross", 60)
        billing.add_price("p-cross", "Y", 4)
        billing.add_price("p-cross", "X", LIST_PRICE)
        billing.issue_voucher(
            {
                "voucher_id": "VX", "owner_counter": "A", "paid_price": 2,
                "cap_amount": 4, "valid_from_day": 25, "valid_until_day": 75,
                "bottles": ["Y"], "counters": ["B"],
            }
        )
        inventory.hold("B", [("Y", 1)], 35)
        inventory.consume_held("B", 35)
        billing.issue_bill(
            "p-cross", 60,
            credentials=[c for c in billing.load_credentials("p-cross")
                         if c.bottle_id == "Y"],
        )
        cross = print_bill("cross-month voucher still valid after month boundary",
                           billing, "p-cross")
        assert cross["lines"][0]["voucher_id"] == "VX"

        # ------------------------------------------------------------------
        # Clearing seat for the fixed route configurations.
        # ------------------------------------------------------------------
        direct = Route("BA", "B", "A", 3, 1)
        via_c = [Route("BC", "B", "C", 3, 1), Route("CA", "C", "A", 3, 1)]
        to_supplier = Route("BS", "B", "S", 5, 2)

        show_clearing("outcome 1: direct line is cheapest",
                      {"A": 3, "B": -3, "C": 0, "S": 0},
                      [direct, *via_c, to_supplier])
        show_clearing("outcome 2: full cash to the supplier",
                      {"A": 0, "B": -5, "C": 0, "S": 5},
                      [direct, *via_c, to_supplier])
        show_clearing("outcome 3: B->A shut, two hops through C",
                      {"A": 3, "B": -3, "C": 0, "S": 0},
                      [*via_c, to_supplier])
        show_clearing("infeasible network emits no half-batch",
                      {"A": 3, "B": -3, "C": 0, "S": 0},
                      [to_supplier])
        conn.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db_path + suffix)
            except FileNotFoundError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
