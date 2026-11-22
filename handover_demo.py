#!/usr/bin/env python3
"""Reproduce the worked example from the handover specification.

Three tickets: T1, T2 are directly classified at L and were held by desk A;
T3 is directly classified at B and was held by desk B. T1+T2 arrive sealed in
one group. Desks A, B, R (registered on their same-named nodes) have quotas
2, 3 and enough headroom; every cross-desk move costs one workhour.

The script walks through the two re-adjudications required by the text:
  1. original catalog          -> sealed group stays at A, loose stays at B, 0h
  2. edge L->A removed          -> both units go to B, 2h
  3. edge restored, one A slot already taken -> whole group still goes to B,
     never split one ticket per desk
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from cs.store import Store  # noqa: E402

CATALOG = {
    "format": "classification-catalog/1",
    "revision": "G1",
    "nodes": [
        {"id": "R", "name": "全部"},
        {"id": "A", "name": "办件"},
        {"id": "B", "name": "办件"},
        {"id": "L", "name": "交叉子类"},
    ],
    "edges": [["A", "R"], ["B", "R"], ["L", "A"], ["L", "B"]],
}
DESKS = [
    {"desk_id": "A", "node_id": "A", "name": "办件一席"},
    {"desk_id": "B", "node_id": "B", "name": "办件二席"},
    {"desk_id": "R", "node_id": "R", "name": "总台"},
]
FACTS = [
    {"ticket_id": "T1", "revision": 1, "amount": 0, "classes": ["L"]},
    {"ticket_id": "T2", "revision": 1, "amount": 0, "classes": ["L"]},
    {"ticket_id": "T3", "revision": 1, "amount": 0, "classes": ["B"]},
]
STACK = {
    "stack_id": "stack-main",
    "tickets": [
        {"ticket_id": "T1", "from_desk": "A", "transfer_cost": 1},
        {"ticket_id": "T2", "from_desk": "A", "transfer_cost": 1},
        {"ticket_id": "T3", "from_desk": "B", "transfer_cost": 1},
    ],
    "sealed_groups": [["T1", "T2"]],
}


def plan_summary(signoff: dict) -> dict:
    plan = signoff["plan"]
    return {
        "status": signoff["status"],
        "total_workhours": plan["total_workhours"],
        "units": [
            {"unit": a["unit_id"], "tickets": a["ticket_ids"], "desk": a["desk_id"]}
            for a in plan["assignments"]
        ],
        "desk_totals": plan["desk_totals"],
    }


def main() -> int:
    db_path = Path(tempfile.mktemp(suffix=".db"))
    report: dict = {}
    try:
        with Store.open(str(db_path)) as store:
            store.init_catalog(CATALOG)
            store.apply_fact_revisions(FACTS, request_id="seed")
            store.register_desks(DESKS, request_id="desk-D1")

            # Routing check-out at the original cut: L reaches A, B and R; R is
            # pruned because A and B are strictly more specific.
            routing = store.routing_checkout(["T1", "T2", "T3"])
            report["routing_T1"] = {
                "retained": routing["actionable"][0]["retained_desks"],
                "pruned": [p["desk_id"] for p in routing["actionable"][0]["pruned_desks"]],
            }

            store.create_roster(
                "roster-1", {"A": 2, "B": 3, "R": 5}, description="接班一班"
            )

            # ---- verdict 1: original catalog, full quotas ----------------
            first = store.handover("roster-1", STACK, signoff_id="H1")
            report["verdict_1_original"] = plan_summary(first)

            # ---- real catalog change: remove L->A (atomic bundle) --------
            store.apply_bundle(
                {"remove_edges": [["L", "A"]], "add_edges": [], "fact_revisions": []},
                request_id="drop-LA",
            )
            second = store.handover("roster-1", STACK, signoff_id="H2")
            report["verdict_2_edge_removed"] = plan_summary(second)

            # ---- restore the edge; one A slot was already taken -----------
            store.apply_bundle(
                {"add_edges": [["L", "A"]], "remove_edges": [], "fact_revisions": []},
                request_id="restore-LA",
            )
            # The old handover is wound down; another order takes one of A's
            # two slots with a loose ticket that legitimately only sits at A.
            store.release_handover("H2")
            store.apply_fact_revisions(
                [{"ticket_id": "T4", "revision": 1, "amount": 0, "classes": ["A"]}],
                request_id="add-T4",
            )
            store.handover(
                "roster-1",
                {
                    "stack_id": "stack-other",
                    "tickets": [
                        {"ticket_id": "T4", "from_desk": "A", "transfer_cost": 1}
                    ],
                    "sealed_groups": [],
                },
                signoff_id="H-other",
            )
            third = store.handover("roster-1", STACK, signoff_id="H3")
            report["verdict_3_one_slot_taken"] = plan_summary(third)

            # The split temptation: prove the group never lands one ticket in A
            # and one in B.
            for signoff_id in ("H1", "H2", "H3"):
                for assignment in store.get_handover(signoff_id)["plan"]["assignments"]:
                    if assignment["kind"] == "sealed":
                        desks = {a["to_desk"] for a in assignment["per_ticket"]}
                        assert len(desks) == 1, "sealed group was split!"

            # Re-reading the first sign-off must still show the old cut/plan.
            report["H1_immutable"] = {
                "cut": store.get_handover("H1")["cut"],
                "workhours": store.get_handover("H1")["plan"]["total_workhours"],
            }
            report["roster_after_all"] = store.get_roster("roster-1")

        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        if db_path.exists():
            db_path.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
