#!/usr/bin/env python3
"""End-to-end walkthrough: real (temporary) old database -> exact flows ->
whole-cent settlement -> reopen and re-balance, plus the no-rounding failure.

Run it with ``python3 examples/settlement_walkthrough.py``.  It writes the
artifacts it prints into ``demo/out/settlement/`` so another duty officer can
re-check them, and it never touches the repository's main demo database.

The four scenes:

1. Original diamond: X and Y (amount 1 each) are directly in L, L's parents A
   and B are cabinets.  Every ticket is 1/2 to A and 1/2 to B.  Targets A=1,
   B=1.  The diagonal settlement (X->A, Y->B) rounds both cells up for free:
   minimum fee 0.
2. X is moved to be directly in B only.  X is already paid 1 to B, so Y is
   the only ticket that can move; it must round up to A.  Minimum fee 3.
3. The original shares are restored, but the teller has locked X at "paid 1
   to B".  The whole account must go the same direction: X->B and Y->A both
   round up.  Minimum fee 6.
4. Re-open X (withdraw + new revision) and re-settle; the balances reproduce.
5. Infeasible showcase: every column target sums to the right total, but one
   cabinet has no fractional cell that could provide the cent it is owed, so
   no whole-cent settlement exists and nothing is moved.
"""

from __future__ import annotations

import json
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cs import settle, split  # noqa: E402
from cs.store import Store  # noqa: E402

OUT = ROOT / "demo" / "out" / "settlement"

CATALOG = {
    "format": "classification-catalog/1",
    "revision": "G1",
    "nodes": [
        {"id": "R", "name": "全部"},
        {"id": "A", "name": "柜A"},
        {"id": "B", "name": "柜B"},
        {"id": "L", "name": "交叉子类"},
    ],
    "edges": [["A", "R"], ["B", "R"], ["L", "A"], ["L", "B"]],
    "cabinets": ["A", "B"],
}

FACTS = [
    {"ticket_id": "X", "revision": 1, "amount": 1, "classes": ["L"]},
    {"ticket_id": "Y", "revision": 1, "amount": 1, "classes": ["L"]},
]

# Fee per (ticket, cabinet) cell for an upward rounding.  The diagonal cells
# are free; the crossed cells cost 3 each.
FEES = {("X", "A"): 0, ("Y", "B"): 0, ("X", "B"): 3, ("Y", "A"): 3}


def dump(name: str, value) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def ledger_view(ledger) -> dict:
    """Compact printable view of a split-ledger/1 payload."""

    out = {}
    for ticket in ledger["tickets"]:
        out[ticket["ticket_id"]] = {
            "revision": ticket["revision"],
            "amount": ticket["amount"],
            "direct_classes": ticket["direct_classes"],
            "shares": {
                share["cabinet"]: {
                    "fraction": share["share"]["text"],
                    "flows": [
                        {
                            "path": f["path"],
                            "fraction": f["share"]["text"],
                        }
                        for f in share["flows"]
                    ],
                }
                for share in ticket["shares"]
            },
        }
    return out


def settle_from_ledger(ledger, targets, *, locked=None) -> dict:
    shares: dict[str, dict[str, Fraction]] = {}
    amounts: dict[str, int] = {}
    for ticket in ledger["tickets"]:
        amounts[ticket["ticket_id"]] = ticket["amount"]
        shares[ticket["ticket_id"]] = {
            share["cabinet"]: Fraction(share["share"]["num"], share["share"]["den"])
            for share in ticket["shares"]
        }
    return settle.solve_settlement(
        shares,
        amounts,
        targets,
        locked=locked,
        fees=FEES,
        cut=ledger["cut"],
    )


def cell_table(report) -> dict:
    return {
        f"{c['ticket_id']}->{c['cabinet']}": {
            "share": c["share"]["text"],
            "paid": c["paid"],
            "rounding": c["rounding"],
            "fee": c["fee"],
        }
        for c in report["cells"]
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    db_path = Path(tempfile.mktemp(suffix=".db"))
    summary = {"format": "settlement-walkthrough/1", "scenes": []}
    try:
        store = Store.open(str(db_path))
        store.init_catalog(CATALOG)
        store.apply_fact_revisions(FACTS, request_id="seed")

        # ---------------------------------------------------------- scene 1
        original_cut = store.current_cut()
        ledger1 = store.build_split_ledger()
        verify1 = split.verify_ledger(
            ledger1,
            edges=store.load_catalog_state().edges,
            cabinets=store.load_catalog_state().cabinets,
        )
        report1 = settle_from_ledger(ledger1, {"A": 1, "B": 1})
        check1 = settle.verify_settlement(
            report1,
            amounts={
                ledger1_ticket["ticket_id"]: ledger1_ticket["amount"]
                for ledger1_ticket in ledger1["tickets"]
            },
            targets={"A": 1, "B": 1},
            fees=FEES,
            re_solve=True,
        )
        scene1 = {
            "title": "original shares; diagonal rounding is free",
            "cut": ledger1["cut"],
            "flows": ledger_view(ledger1),
            "flow_verification": verify1["ok"],
            "payment": cell_table(report1),
            "total_fee": report1["total_fee"],
            "settlement_verification": check1["ok"],
        }
        summary["scenes"].append(scene1)
        dump("scene1_original_ledger.json", ledger1)
        dump("scene1_original_settlement.json", report1)

        # ---------------------------------------------------------- scene 2
        # Only X is moved to be directly in B; X's amount is unchanged.
        store.apply_fact_revision(
            "X", 2, 1, ["B"], request_id="move-X-to-B"
        )
        ledger2 = store.build_split_ledger()
        # X has already been paid its full 1 to B.
        report2 = settle_from_ledger(ledger2, {"A": 1, "B": 1}, locked={("X", "B"): 1})
        scene2 = {
            "title": "X directly in B, already paid 1 to B; Y must round up to A",
            "cut": ledger2["cut"],
            "flows": ledger_view(ledger2),
            "payment": cell_table(report2),
            "total_fee": report2["total_fee"],
        }
        summary["scenes"].append(scene2)
        dump("scene2_x_direct_b_ledger.json", ledger2)
        dump("scene2_x_direct_b_settlement.json", report2)

        # ---------------------------------------------------------- scene 3
        # Restore the original shares (a new revision) but keep the teller's
        # lock "X has already paid 1 to B".
        store.apply_fact_revision(
            "X", 3, 1, ["L"], request_id="restore-X-shares"
        )
        ledger3 = store.build_split_ledger()
        report3 = settle_from_ledger(ledger3, {"A": 1, "B": 1}, locked={("X", "B"): 1})
        scene3 = {
            "title": "original shares restored, X->B locked at 1; both ups cost 6",
            "cut": ledger3["cut"],
            "flows": ledger_view(ledger3),
            "payment": cell_table(report3),
            "total_fee": report3["total_fee"],
        }
        summary["scenes"].append(scene3)
        dump("scene3_locked_ledger.json", ledger3)
        dump("scene3_locked_settlement.json", report3)

        # ---------------------------------------------------------- scene 4
        # Re-open X (withdraw then a higher revision) and re-settle.  The
        # balances must reproduce exactly ("reopen and re-balance").
        store.withdraw("X", 3, request_id="reopen-withdraw")
        reopened = store.apply_fact_revision(
            "X", 4, 1, ["L"], request_id="reopen-X"
        )
        ledger4 = store.build_split_ledger()
        report4 = settle_from_ledger(ledger4, {"A": 1, "B": 1})
        # Re-reading the *original* cut must still give the original ledger;
        # the old products are immutable and historically reproducible.
        old_ledger = store.build_split_ledger(snapshot=original_cut)
        historical = settle_from_ledger(old_ledger, {"A": 1, "B": 1})
        scene4 = {
            "title": "reopen X at revision 4 and re-settle; balances reproduce",
            "cut": ledger4["cut"],
            "reopened_revision": next(
                t["revision"] for t in ledger4["tickets"] if t["ticket_id"] == "X"
            ),
            "reopen_cut_fact_revision": reopened.cut.fact_revision,
            "flows": ledger_view(ledger4),
            "total_fee": report4["total_fee"],
            "original_cut": original_cut.as_dict(),
            "original_cut_reproduces": (
                old_ledger["cut"] == original_cut.as_dict()
                and historical["total_fee"] == scene1["total_fee"]
                and cell_table(historical) == scene1["payment"]
            ),
        }
        summary["scenes"].append(scene4)
        dump("scene4_reopened_ledger.json", ledger4)
        dump("scene4_reopened_settlement.json", report4)

        # ---------------------------------------------------------- scene 5
        # Infeasible: column targets add up correctly, but cabinet C can only
        # receive exact integer shares, so its owed cent has no cell to round.
        infeasible_shares = {
            "P": {"A": Fraction(1, 2), "B": Fraction(1, 2)},
            "Q": {"C": Fraction(1)},
            "R": {"C": Fraction(1)},
        }
        infeasible_amounts = {"P": 1, "Q": 1, "R": 1}
        infeasible_targets = {"A": 0, "B": 0, "C": 3}
        infeasible = settle.solve_settlement(
            infeasible_shares,
            infeasible_amounts,
            infeasible_targets,
            cut=ledger4["cut"],
        )
        check5 = settle.verify_settlement(
            infeasible,
            shares=infeasible_shares,
            amounts=infeasible_amounts,
            targets=infeasible_targets,
        )
        scene5 = {
            "title": "column targets sum correctly but C has no cell to round up",
            "amount_total": sum(infeasible_amounts.values()),
            "target_total": sum(infeasible_targets.values()),
            "status": infeasible["status"],
            "reasons": infeasible["reasons"],
            "no_payment_fabricated": all(c["paid"] is None for c in infeasible["cells"]),
            "verification": check5["ok"],
        }
        summary["scenes"].append(scene5)
        dump("scene5_infeasible_settlement.json", infeasible)

        # --------------------------------------------------- same-cut bundle
        # One old bundle updates an edge and an amount together; the exact
        # shares must be the post-bundle ones from that single cut.
        bundle = store.apply_bundle(
            {
                "remove_edges": [["L", "B"]],
                "add_edges": [],
                "fact_revisions": [
                    {"ticket_id": "Y", "revision": 2, "amount": 2, "classes": ["L"]}
                ],
            },
            request_id="OLD-BUNDLE",
        )
        ledger_bundle = store.build_split_ledger()
        bundle_scene = {
            "title": "old bundle changed edge L->B and Y's amount; one cut only",
            "cut": ledger_bundle["cut"],
            "committed_cut": bundle.cut.as_dict(),
            "ledger_cut_matches_commit": ledger_bundle["cut"] == bundle.cut.as_dict(),
            "flows": ledger_view(ledger_bundle),
        }
        summary["scenes"].append(bundle_scene)
        dump("scene_bundle_ledger.json", ledger_bundle)

        store.close()
        dump("walkthrough.json", summary)

        # ------------------------------------------------------------ report
        print("=" * 72)
        print("SETTLEMENT WALKTHROUGH")
        print("=" * 72)
        for index, scene in enumerate(summary["scenes"], 1):
            print(f"\n## Scene {index}: {scene['title']}")
            if "cut" in scene:
                print(f"   cut: {scene['cut']}")
            if "flows" in scene:
                for ticket_id, info in scene["flows"].items():
                    legs = ", ".join(
                        f"{cab}={share['fraction']} ({'/'.join(flow['path'])})"
                        for cab, share in info["shares"].items()
                        for flow in share["flows"]
                    )
                    print(f"   {ticket_id} rev{info['revision']} amt{info['amount']}: {legs}")
            if "payment" in scene:
                for cell, info in scene["payment"].items():
                    print(
                        f"   pay {cell}: share {info['share']} -> {info['paid']} "
                        f"[{info['rounding']}, fee {info['fee']}]"
                    )
                print(f"   minimum total fee: {scene['total_fee']}")
            if scene.get("flow_verification") is not None:
                print(f"   flow verification: {scene['flow_verification']}")
            if scene.get("settlement_verification") is not None:
                print(f"   settlement verification: {scene['settlement_verification']}")
            if scene.get("original_cut_reproduces") is not None:
                print(
                    f"   original cut {scene['original_cut']} reproduces: "
                    f"{scene['original_cut_reproduces']}"
                )
            if scene.get("ledger_cut_matches_commit") is not None:
                print(
                    f"   ledger cut == committed bundle cut: "
                    f"{scene['ledger_cut_matches_commit']}"
                )
            if scene.get("status") == settle.STATUS_INFEASIBLE:
                print(
                    f"   amounts total {scene['amount_total']} == targets total "
                    f"{scene['target_total']}, yet:"
                )
                for reason in scene["reasons"]:
                    print(f"     - {reason}")
                print(f"   no payment fabricated: {scene['no_payment_fabricated']}")
                print(f"   verification: {scene['verification']}")

        print(f"\nartifacts written under {OUT}")
        return 0
    finally:
        store_cleanup = db_path
        if store_cleanup.exists():
            store_cleanup.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
