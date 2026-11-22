#!/usr/bin/env python3
"""Scale walkthrough on a real (temporary) old database.

200 tickets, 21 receiving cabinets, a handful of cross-cutting leaves and
multi-level paths, with many whole-cent cells already paid.  Everything here
is produced from an actual SQLite database built by ``python -m cs init`` from
``examples/data/`` — not from an in-memory mock.

    python3 examples/scale_walkthrough.py

Artifacts: ``demo/out/settlement/scale_*.json``.

Scenes
------
A. Build the ledger at one cut, verify every witness path, settle the whole
   account (row sums = ticket amounts, column sums = teller targets, already
   paid integer cells fixed), and independently re-check the minimum fee.
B. Re-open one ticket (withdraw, higher revision) — the balances reproduce and
   the original cut still re-reads to the original flows.
C. Infeasible: the teller's column targets sum to the ticket total, but one
   cabinet is owed a cent it has no fractional cell available to round up, so
   the ledger refuses to pay and moves nothing.
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

DATA = ROOT / "examples" / "data"
OUT = ROOT / "demo" / "out" / "settlement"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def dump(name: str, value) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def ledger_shares(ledger):
    """Return (shares, amounts, exact totals) from a split-ledger/1 payload."""

    shares: dict[str, dict[str, Fraction]] = {}
    amounts: dict[str, int] = {}
    totals: dict[str, Fraction] = {}
    for ticket in ledger["tickets"]:
        amounts[ticket["ticket_id"]] = ticket["amount"]
        shares[ticket["ticket_id"]] = {}
        for share in ticket["shares"]:
            value = Fraction(share["share"]["num"], share["share"]["den"])
            shares[ticket["ticket_id"]][share["cabinet"]] = value
            totals[share["cabinet"]] = totals.get(share["cabinet"], Fraction(0)) + value
    return shares, amounts, totals


def teller_targets(totals: dict[str, Fraction], ticket_total: int) -> dict[str, int]:
    """A whole-cent target for every cabinet: floor everything, then hand the
    remaining cents to the cabinets with the largest fractional part.

    The result sums exactly to ``ticket_total`` and every target lies between
    the floor and ceiling of that cabinet's exact total, so a whole-cent
    settlement of the account exists (classical matrix-rounding result).
    """

    cabinets = sorted(totals)
    targets = {c: totals[c].numerator // totals[c].denominator for c in cabinets}
    remaining = ticket_total - sum(targets.values())
    order = sorted(
        cabinets,
        key=lambda c: (-(totals[c] - targets[c]), c),
    )
    for index in range(remaining):
        targets[order[index % len(order)]] += 1
    return targets


def already_paid(shares, *, fee_by_cell: dict) -> tuple[dict, dict]:
    """Lock cells that already have an integer share; assign a rounding fee.

    Integer cells need no rounding, so locking them is a no-op for
    feasibility; fractional cells stay free.  Fees are deterministic so the
    example is reproducible.
    """

    locked: dict[tuple[str, str], int] = {}
    fees: dict[tuple[str, str], int] = {}
    for ticket_id, per_cabinet in shares.items():
        for cabinet, value in per_cabinet.items():
            if value.denominator == 1:
                locked[(ticket_id, cabinet)] = int(value)
            else:
                fees[(ticket_id, cabinet)] = fee_by_cell.get(
                    (ticket_id, cabinet), (len(ticket_id) + len(cabinet)) % 4
                )
    return locked, fees


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    db_path = Path(tempfile.mktemp(suffix=".db"))
    catalog = load_json(DATA / "catalog_20_cabinets.json")
    facts = load_jsonl(DATA / "facts_200.jsonl")
    summary: dict = {"format": "settlement-scale-walkthrough/1", "scenes": []}
    store = Store.open(str(db_path))
    try:
        store.init_catalog(catalog)
        store.apply_fact_revisions(
            [
                {
                    "ticket_id": f["ticket_id"],
                    "revision": f["revision"],
                    "amount": f["amount"],
                    "classes": f["classes"],
                }
                for f in facts
            ],
            request_id="seed-200",
        )
        original_cut = store.current_cut()

        # ------------------------------------------------------------ scene A
        ledger = store.build_split_ledger()
        flows_check = split.verify_ledger(
            ledger,
            edges=store.load_catalog_state().edges,
            cabinets=store.load_catalog_state().cabinets,
        )
        shares, amounts, totals = ledger_shares(ledger)
        ticket_total = sum(amounts.values())
        targets = teller_targets(totals, ticket_total)
        locked, fees = already_paid(shares, fee_by_cell={})
        report = settle.solve_settlement(
            shares, amounts, targets, locked=locked, fees=fees, cut=ledger["cut"]
        )
        verify = settle.verify_settlement(
            report,
            shares=shares,
            amounts=amounts,
            targets=targets,
            locked=locked,
            fees=fees,
            re_solve=True,
        )
        fractional_cells = sum(1 for c in report["cells"] if c["rounding"] in ("floor", "ceil"))
        scene_a = {
            "title": "200 tickets / 21 cabinets: exact flows then whole-cent settlement",
            "cut": ledger["cut"],
            "tickets": len(amounts),
            "cabinets": len(totals),
            "ticket_total": ticket_total,
            "targets_sum": sum(targets.values()),
            "already_locked_cells": len(locked),
            "fractional_cells": fractional_cells,
            "status": report["status"],
            "total_fee": report["total_fee"],
            "flow_verification": flows_check["ok"],
            "settlement_verification": verify["ok"],
            "column_check": [
                {"cabinet": c["cabinet"], "target": c["target"], "paid": c["paid"], "ok": c["ok"]}
                for c in report["columns"]
            ],
        }
        summary["scenes"].append(scene_a)
        dump("scale_ledger.json", ledger)
        dump("scale_settlement.json", report)

        # ------------------------------------------------------------ scene B
        # Re-open one ticket that carries a fractional share, then re-settle.
        victim = next(
            ticket["ticket_id"]
            for ticket in ledger["tickets"]
            if any(Fraction(s["share"]["num"], s["share"]["den"]).denominator != 1 for s in ticket["shares"])
        )
        victim_revision = next(
            ticket["revision"] for ticket in ledger["tickets"] if ticket["ticket_id"] == victim
        )
        victim_amount = amounts[victim]
        victim_classes = next(
            ticket["direct_classes"] for ticket in ledger["tickets"] if ticket["ticket_id"] == victim
        )
        store.withdraw(victim, victim_revision, request_id="scale-withdraw")
        store.apply_fact_revision(
            victim,
            victim_revision + 1,
            victim_amount,
            victim_classes,
            request_id="scale-reopen",
        )
        reopened_ledger = store.build_split_ledger()
        reopened_shares, reopened_amounts, reopened_totals = ledger_shares(reopened_ledger)
        reopened_targets = teller_targets(reopened_totals, sum(reopened_amounts.values()))
        reopened_report = settle.solve_settlement(
            reopened_shares,
            reopened_amounts,
            reopened_targets,
            locked={
                key: value for key, value in locked.items() if key[0] != victim
            },
            fees={
                key: value for key, value in fees.items() if key[0] != victim
            },
            cut=reopened_ledger["cut"],
        )
        old_ledger = store.build_split_ledger(snapshot=original_cut)
        scene_b = {
            "title": f"withdraw and re-open {victim}, then re-settle",
            "victim": victim,
            "old_revision": victim_revision,
            "new_revision": victim_revision + 1,
            "cut": reopened_ledger["cut"],
            "status": reopened_report["status"],
            "total_fee": reopened_report["total_fee"],
            "original_cut_still_reads_same_flows": (
                old_ledger["cut"] == original_cut.as_dict()
                and [
                    (t["ticket_id"], t["revision"], t["amount"], t["shares"])
                    for t in old_ledger["tickets"]
                ]
                == [
                    (t["ticket_id"], t["revision"], t["amount"], t["shares"])
                    for t in ledger["tickets"]
                ]
            ),
        }
        summary["scenes"].append(scene_b)
        dump("scale_reopened_ledger.json", reopened_ledger)
        dump("scale_reopened_settlement.json", reopened_report)

        # ------------------------------------------------------------ scene C
        # Build an account whose column targets total exactly the ticket total,
        # but one cabinet is owed a cent and has only integer cells (no cell
        # may be rounded up).  We take the real reopened ledger's shares and
        # move one cent of target onto a "brick wall" cabinet that has no
        # fractional cell, taking it from a cabinet that has spare fractional
        # headroom, so the column total is unchanged.
        integer_only = sorted(
            cabinet
            for cabinet in reopened_totals
            if all(
                share.denominator == 1
                for per in reopened_shares.values()
                for cab, share in per.items()
                if cab == cabinet
            )
        )
        if not integer_only:
            raise RuntimeError("expected at least one integer-only cabinet at this scale")
        brick = integer_only[0]
        # Take the cent from a cabinet whose target is above the floor of its
        # exact total, so that cabinet stays feasible; only the brick (an
        # integer-only cabinet) is pushed one above what any cell can pay.
        generous = next(
            cabinet
            for cabinet in sorted(reopened_targets)
            if cabinet != brick
            and reopened_targets[cabinet]
            > reopened_totals[cabinet].numerator // reopened_totals[cabinet].denominator
        )
        broken_targets = dict(reopened_targets)
        broken_targets[brick] += 1
        broken_targets[generous] -= 1
        broken = settle.solve_settlement(
            reopened_shares,
            reopened_amounts,
            broken_targets,
            locked={k: v for k, v in locked.items() if k[0] != victim},
            fees={k: v for k, v in fees.items() if k[0] != victim},
        )
        broken_check = settle.verify_settlement(
            broken,
            shares=reopened_shares,
            amounts=reopened_amounts,
            targets=broken_targets,
        )
        scene_c = {
            "title": "column targets total correctly, yet a cabinet has no cent to round",
            "brick_cabinet": brick,
            "cent_taken_from": generous,
            "ticket_total": sum(reopened_amounts.values()),
            "target_total": sum(broken_targets.values()),
            "status": broken["status"],
            "reasons": broken["reasons"],
            "no_payment_fabricated": all(c["paid"] is None for c in broken["cells"]),
            "verification": broken_check["ok"],
        }
        summary["scenes"].append(scene_c)
        dump("scale_infeasible_settlement.json", broken)

        dump("scale_walkthrough.json", summary)

        # -------------------------------------------------------------- print
        print("=" * 72)
        print("SCALE SETTLEMENT WALKTHROUGH (real temporary database)")
        print("=" * 72)
        for index, scene in enumerate(summary["scenes"], 1):
            print(f"\n## Scene {index}: {scene['title']}")
            for key, value in scene.items():
                if key in {"title", "reasons"}:
                    continue
                if key == "column_check":
                    bad = [c for c in value if not c["ok"]]
                    print(f"   columns checked: {len(value)} (all ok: {not bad})")
                    continue
                print(f"   {key}: {value}")
            for reason in scene.get("reasons", []):
                print(f"     - {reason}")
        print(f"\nartifacts written under {OUT}")
        return 0
    finally:
        store.close()
        if db_path.exists():
            db_path.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
