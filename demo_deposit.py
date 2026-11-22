#!/usr/bin/env python3
"""Run the 719 container-deposit scenario and print a hand-checkable trace.

Usage::

    python3 demo_deposit.py [--db PATH] [--out PATH]

Scenario (all deposits, prices and freight are fictional accounting data):

* A and B are the same model from the same supplier, each carrying a deposit of
  4 and a return-by day of 12.
* A is used up on day 10 through the ordinary run path; its last consumption
  is the only reason it has no active reservation left.
* B still has one portion on hand, but that portion expired (visible expiry
  day 12) and cannot be led.  It stays ``unallocated`` until two different
  counters clear the same stocktake version and the exact remaining quantity.
* One trip arrives on day 12; a two-slot box costs 5.

The trace shows: with only A the plan loses 1 and ships nothing; after the
second counter clears B the plan ships both and nets 3.  It also exercises the
return-by-miss branch (same vouchers, trip arrives 13), the two counters signing
different stocktake versions, and a bottle that an old holder reserved before
expiry and still held at expiry.  The shipping plan and per-bottle conservation
are written to a JSON file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reagent import connect, initialize  # noqa: E402
from reagent.cli import execute_operations  # noqa: E402
from reagent.deposits import DepositService  # noqa: E402
from reagent.errors import Conflict  # noqa: E402
from reagent.inventory import InventoryService  # noqa: E402
from reagent.runs import RunService  # noqa: E402

ROOT = Path(__file__).resolve().parent
MATERIALS = ROOT / "materials" / "719"


def _fresh(base_path: str, suffix: str, document: dict):
    db_path = f"{base_path}.{suffix}"
    for clean in (db_path, db_path + "-wal", db_path + "-shm"):
        try:
            os.unlink(clean)
        except FileNotFoundError:
            pass
    conn = connect(db_path)
    initialize(conn)
    InventoryService(conn).initialize(document)
    return conn, db_path


def _cleanup(db_path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(db_path + suffix)
        except FileNotFoundError:
            pass


def _plan_line(label: str, plan: dict) -> str:
    return (
        f"{label:26} bottles={plan['bottle_count']} "
        f"deposits={plan['deposit_total']} box_cost={plan['box_cost']} "
        f"profit={plan['profit']} trips={plan['trips_used']}"
    )


def _print_plan(label: str, plan: dict) -> None:
    print(_plan_line(label, plan))
    for box in plan["boxes"]:
        print(
            f"{'':28}box {box['trip_id']}/{box['model_id']} "
            f"supplier={box['supplier']} price={box['price']} "
            f"bottles={box['bottles']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default=str(ROOT / "materials" / "719" /
                                             "deposit_results.json"))
    args = parser.parse_args()

    if args.db:
        base_path = args.db
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(base_path + suffix)
            except FileNotFoundError:
                pass
    else:
        handle, base_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)

    inventory_doc = json.loads(
        (MATERIALS / "deposit_inventory.json").read_text("utf-8")
    )
    operations_doc = json.loads(
        (MATERIALS / "deposit_operations.json").read_text("utf-8")
    )

    conn = connect(base_path)
    initialize(conn)
    inventory = InventoryService(conn)
    inventory.initialize(inventory_doc)

    print("== operations ==")
    first_plan = None
    final_shipment = None
    for entry in execute_operations(conn, operations_doc):
        op = entry["op"]
        result = entry["result"]
        if op == "explain_voucher":
            print(
                f"{op:18} {result['bottle_id']:3} day={result['day']} "
                f"eligible={result['eligible']} voucher={result['voucher_id']}"
            )
            for blocker in result["blockers"]:
                print(f"{'':18} blocked: {blocker}")
            continue
        if op == "apply_voucher":
            print(
                f"{op:18} {result['bottle_id']:3} deposit={result['deposit']} "
                f"return_by={result['latest_return_day']} "
                f"status={result['status']}"
            )
            continue
        if op == "plan":
            if first_plan is None:
                first_plan = result
            _print_plan(op, result)
            continue
        if op == "ship":
            final_shipment = result
            _print_plan(op, result)
            continue
        if op == "sign_clearance":
            last = result["signoffs"][-1]["counter_id"]
            print(
                f"{op:18} {result['request_id']} counter={last} "
                f"status={result['status']}"
            )
            continue
        if op == "open_clearance":
            print(
                f"{op:18} {result['request_id']} portions={result['portions']} "
                f"version={result['inventory_version']} status={result['status']}"
            )
            continue
        if op == "create":
            print(
                f"{op:18} {entry['run_id']:10} -> {result['state']} "
                f"requirements={result['requirements']}"
            )
            continue
        if op == "start":
            print(
                f"{op:18} {entry['run_id']:10} -> {result['state']} "
                f"day={result['started_day']}"
            )
            continue
        if op == "conservation":
            continue
        print(f"{op:18} {result}")

    results: dict = {
        "initial_plan_one_voucher": first_plan,
        "final_shipment": final_shipment,
        "branches": {},
    }

    # -- branch: same vouchers, trip arrives day 13 --------------------------
    print("\n== branch: same vouchers, trip arrives day 13 ==")
    branch, branch_path = _fresh(base_path, "late", inventory_doc)
    runs = RunService(branch)
    deposits = DepositService(branch)
    runs.create_run("use-A", "USE1", 1, 10, [{"bottle_id": "A", "portions": 1}])
    runs.start_run("use-A", 10)
    deposits.add_supplier_kind("SUP", "R")
    deposits.add_trip("T13", 12, 13, 3)
    deposits.add_box_model("TWO", 2, ["R", "S"])
    deposits.add_trip_offer("T13", "TWO", 5)
    deposits.apply_voucher("A", 12)
    deposits.open_clearance("B", 1, 12)
    deposits.sign_clearance("CLR:B", "count-1", 12)
    deposits.sign_clearance("CLR:B", "count-2", 12)
    deposits.apply_voucher("B", 12)
    late_plan = deposits.plan_shipment(12)
    _print_plan("late trip (arr 13)", late_plan)
    results["branches"]["late_trip"] = late_plan
    branch.close()
    _cleanup(branch_path)

    # -- branch: two counters sign different stocktake versions -------------
    print("\n== branch: two counters sign different versions ==")
    branch, branch_path = _fresh(base_path, "versions", inventory_doc)
    deposits = DepositService(branch)
    deposits.open_clearance("B", 1, 12)
    first = deposits.sign_clearance("CLR:B", "count-1", 12, inventory_version=0)
    second = deposits.sign_clearance("CLR:B", "count-2", 12, inventory_version=1)
    try:
        deposits.apply_voucher("B", 12)
        voucher = "UNEXPECTED-ELIGIBLE"
    except Conflict:
        voucher = "blocked"
    print(
        f"{'signatures':18} versions={[s['inventory_version'] for s in second['signoffs']]} "
        f"status={second['status']} voucher={voucher}"
    )
    results["branches"]["different_versions"] = {
        "status": second["status"],
        "signoffs": second["signoffs"],
        "voucher": voucher,
    }
    branch.close()
    _cleanup(branch_path)

    # -- branch: old holder reserved before expiry, still held at expiry ----
    print("\n== branch: old holder reserved before expiry, still held at expiry ==")
    branch, branch_path = _fresh(base_path, "oldholder", inventory_doc)
    old_inventory = InventoryService(branch)
    deposits = DepositService(branch)
    old_inventory.hold("old-holder", [("B", 1)], 10)
    alloc = old_inventory.query("B", 12)
    try:
        deposits.open_clearance("B", 1, 12)
        cleared = "UNEXPECTED-CLEARED"
    except Conflict:
        cleared = "blocked"
    try:
        deposits.apply_voucher("B", 12)
        voucher = "UNEXPECTED-ELIGIBLE"
    except Conflict:
        voucher = "blocked"
    print(
        f"{'B day 12':18} reserved={alloc['reserved']} "
        f"unallocated={alloc['unallocated']} status={alloc['status']} "
        f"clearance={cleared} voucher={voucher}"
    )
    results["branches"]["old_holder_at_expiry"] = {
        "reserved": alloc["reserved"],
        "unallocated": alloc["unallocated"],
        "status": alloc["status"],
        "clearance": cleared,
        "voucher": voucher,
    }
    branch.close()
    _cleanup(branch_path)

    # -- conservation ------------------------------------------------------
    print("\n== final conservation (total == unalloc + reserved + consumed + cleared) ==")
    print(
        f"{'bottle':6} {'total':>5} {'unalloc':>7} {'reserved':>8} "
        f"{'consumed':>8} {'cleared':>7}"
    )
    conservation = inventory.conservation()
    ok = True
    for row in conservation:
        rhs = (
            row["unallocated"]
            + row["reserved"]
            + row["consumed"]
            + row["cleared"]
        )
        ok = ok and row["total_portions"] == rhs
        print(
            f"{row['bottle_id']:6} {row['total_portions']:>5} "
            f"{row['unallocated']:>7} {row['reserved']:>8} "
            f"{row['consumed']:>8} {row['cleared']:>7}"
            f"  {'OK' if row['total_portions'] == rhs else 'FAIL'}"
        )
    print(f"conservation all bottles: {'OK' if ok else 'FAIL'}")
    results["conservation"] = conservation
    results["conservation_ok"] = ok

    if args.out:
        Path(args.out).write_text(
            json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        print(f"\nresults written to {args.out}")

    conn.close()
    if not args.db:
        _cleanup(base_path)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
