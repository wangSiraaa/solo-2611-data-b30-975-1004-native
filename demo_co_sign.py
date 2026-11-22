#!/usr/bin/env python3
"""Walk through whole-bottle seals (封单) and offline co-signing (会签).

Usage::

    python3 demo_co_sign.py [--db PATH]

Starts from a fresh database, builds two runs that share a bottle (one also
spans a second bottle), seals the closure, plans the minimum-fee co-signing
scheme, then verifies and posts a voucher. It finishes with the two failure
stories from the specification: a still-effective historical group that blocks
a scheme, and a deputy-only intersection that can never be mutually recognised.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reagent.inventory import InventoryService  # noqa: E402
from reagent.runs import RunService  # noqa: E402
from reagent.schema import connect, initialize  # noqa: E402
from reagent.seals import SealService  # noqa: E402

DOCUMENT = {
    "initial_day": 1,
    "kinds": [{"kind": "R", "open_lifetime_days": 100}],
    "bottles": [
        {
            "bottle_id": "X",
            "kind": "R",
            "total_portions": 2,
            "intake_day": 1,
            "printed_expiry_day": 500,
            "opened_day": None,
        },
        {
            "bottle_id": "Y",
            "kind": "R",
            "total_portions": 1,
            "intake_day": 1,
            "printed_expiry_day": 500,
            "opened_day": None,
        },
    ],
    "methods": [
        {"method_id": "M", "revision": 1, "requirements": {"R": 1}},
        {"method_id": "M2", "revision": 1, "requirements": {"R": 2}},
    ],
}

GROUPS = [
    {"name": "AB", "seats": ["A", "B"], "fee": 1},
    {"name": "AC", "seats": ["A", "C"], "fee": 1},
    {"name": "BC", "seats": ["B", "C"], "fee": 1},
]
SHIFTS = [
    {"shift_id": "s2", "on_duty": ["A", "C"]},
    {"shift_id": "s3", "on_duty": ["B", "C"]},
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    if args.db:
        db_path = args.db
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db_path + suffix)
            except FileNotFoundError:
                pass
    else:
        handle, db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)

    conn = connect(db_path)
    initialize(conn)
    inventory = InventoryService(conn)
    runs = RunService(conn)
    seals = SealService(conn)
    inventory.initialize(DOCUMENT)

    # -- build two runs sharing bottle X; r1 also spans bottle Y -----------
    runs.create_run(
        "r1", "M2", 1, 1,
        [{"bottle_id": "X", "portions": 1}, {"bottle_id": "Y", "portions": 1}],
    )
    runs.create_run("r2", "M", 1, 1, [{"bottle_id": "X", "portions": 1}])
    print("== runs ==")
    for rid in ("r1", "r2"):
        view = runs.get_run(rid)
        print(f"  {rid}: {view['reservations'][0]['bottle_id']} ... active "
              f"{view['allocations']['R']['active']} portion(s)")

    for kind in ("ordinary",):
        seals.register_seats(
            [{"seat_id": s, "seat_type": kind} for s in ("A", "B", "C")], day=1
        )

    # -- seal the closure of r1 --------------------------------------------
    seal = seals.create_seal(
        "S1",
        ["r1"],
        {"r1": ["A", "B"], "r2": ["C"]},
        day=1,
    )
    print("\n== seal S1 (sealing never deducts inventory) ==")
    print(f"  closure runs: {seal['closure_runs']}")
    print(f"  bottles:      {sorted(seal['bottles'])}")
    for rid, info in seal["report"]["runs"].items():
        print(f"  {rid}: {info['reason']}  witnesses={info['witness_seats']}")
    print(f"  clean: {seal['clean']}")

    # -- plan the minimum-fee scheme ---------------------------------------
    scheme = seals.create_scheme("SCH1", "S1", GROUPS, SHIFTS, [], day=1)
    plan = scheme["plan"]
    print("\n== scheme SCH1 ==")
    print(f"  feasible:  {scheme['feasible']}")
    print(f"  total fee: {scheme['total_fee']}")
    print(f"  activated: {plan['activated_groups']}")
    print(f"  ineligible (miss a run's witnesses): {plan['ineligible_groups']}")
    for shift_id, names in plan["per_shift"].items():
        print(f"  shift {shift_id}: {names}")

    # -- verify and post a voucher -----------------------------------------
    print("\n== delivery window s2 ==")
    ok = seals.verify_voucher("S1", "SCH1", "s2", "T1", ["A", "C"])
    print(f"  voucher signed by A,C -> accepted={ok['accepted']}")
    no = seals.verify_voucher("S1", "SCH1", "s2", "T1", ["A"])
    print(f"  voucher signed by A   -> accepted={no['accepted']}")
    posted = seals.post_settlement("S1", "SCH1", "s2", "T1", ["A", "C"], day=1)
    print(f"  posted: {posted['posted']} under group {posted['group']}, "
          f"settlement {posted['settlement_no']}")
    replay = seals.post_settlement("S1", "SCH1", "s2", "T1", ["A", "C"], day=2)
    print(f"  replay same (seal, number) -> replayed={replay['replayed']}")
    try:
        seals.post_settlement("S1", "SCH1", "s3", "T2", ["B", "C"], day=2)
        print("  second distinct settlement: UNEXPECTEDLY POSTED")
    except Exception as exc:  # noqa: BLE001 - demo trace
        print(f"  second distinct settlement rejected: {type(exc).__name__}")

    # -- failure story 1: a still-effective historical group ---------------
    seals.register_historical_group("oldA", ["A"], 2)
    blocked = seals.create_scheme("SCH2", "S1", GROUPS, SHIFTS, [], day=2)
    print("\n== historical group oldA=<A> still effective ==")
    print(f"  feasible: {blocked['feasible']}")
    print(f"  blocked:  {blocked['plan']['blocked_groups']}")
    seals.recall_historical_group("oldA", 3)
    allowed = seals.create_scheme("SCH3", "S1", GROUPS, SHIFTS, [], day=3)
    print("  after recall confirmed:")
    print(f"    feasible: {allowed['feasible']}  activated: "
          f"{allowed['plan']['activated_groups']}")

    # -- failure story 2: deputy-only intersection -------------------------
    # A and B are ordinary; D is a deputy with an independent paper book. G1
    # and G2 each contain one ordinary and D, so each satisfies the self-rule,
    # but the pair shares *only* the deputy -> two windows could each issue a
    # voucher and both look valid.
    deputy_groups = [
        {"name": "G1", "seats": ["A", "D"], "fee": 1},
        {"name": "G2", "seats": ["B", "D"], "fee": 1},
    ]
    deputy_shifts = [
        {"shift_id": "w1", "on_duty": ["A", "D"]},
        {"shift_id": "w2", "on_duty": ["B", "D"]},
    ]
    seats_for_d = [{"seat_id": "D", "seat_type": "deputy"}]
    seals.create_seal("S2", ["r1"], {"r1": ["A", "B"], "r2": ["D"]}, day=3)
    counter = seals.create_scheme(
        "SCH4", "S2", deputy_groups, deputy_shifts, seats_for_d, day=3
    )
    print("\n== deputy-only intersection counterexample ==")
    for c in counter["plan"]["conflicts"]:
        print(f"  {c}")
    print(f"  feasible: {counter['feasible']}  reason: {counter['plan']['reason']}")

    conn.close()
    if not args.db:
        os.unlink(db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
