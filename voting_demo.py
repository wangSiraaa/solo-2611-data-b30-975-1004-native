#!/usr/bin/env python3
"""Walk the voting scenario end to end through the real catalog + store.

The story from the request, reproduced with an actual classification graph:

* proposals A, B, C; tickets x, y, z with distance orders A<B<C, B<C<A and
  C<A<B, so the distance vote gives the cycle A>B, B>C, C>A;
* sending A tabled costs 2 (B/C then A/B);
* add one direct edge from y's direct class to A (a real catalog submission);
  y now ties on A/B yet votes A over C, so A/C flips 1:2 -> 2:1 and A/B from
  2:1 -> 2:0;
* with chair order A, B, C and the venue banning B/C, the edited catalog can
  still seat A via A/C then A/B, while the original catalog cannot;
* settle strictly from a frozen ballot register, and refuse silently changing
  the vote.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from cs.store import Store  # noqa: E402

PROPOSALS = ["A", "B", "C"]
PAIRS = [["A", "B"], ["B", "C"], ["A", "C"]]
COSTS = {"A|B": 1, "B|C": 1, "A|C": 1}


def branch(tag, order):
    """Three independent bands of length 1, 2, 3 ending at ``order``."""

    direct = f"c{tag}"
    a1 = f"a1{tag}"
    b1, b2 = f"b1{tag}", f"b2{tag}"
    edges = [
        (direct, order[0]),
        (direct, a1),
        (a1, order[1]),
        (direct, b1),
        (b1, b2),
        (b2, order[2]),
    ]
    return edges, direct


def seed_catalog():
    edges = []
    classes = {}
    for tag, order in (
        ("x", ["A", "B", "C"]),
        ("y", ["B", "C", "A"]),
        ("z", ["C", "A", "B"]),
    ):
        band, direct = branch(tag, order)
        edges.extend(band)
        classes[tag] = direct
    nodes = {"A", "B", "C"}
    for child, parent in edges:
        nodes.add(child)
        nodes.add(parent)
    return edges, classes, sorted(nodes)


def show_vote(pair):
    print(
        f"    {pair['left']} vs {pair['right']}: "
        f"{pair['left_votes']}:{pair['right_votes']} "
        f"-> {pair['winner']} "
        f"(abstain {pair['abstentions']}, contested {pair['contested']})"
    )


def show_plan(label, plan):
    if plan["feasible"]:
        rounds = " -> ".join(
            f"{r['left']}/{r['right']}={r['winner']}" for r in plan["rounds"]
        )
        print(f"  {label}: feasible, cost {plan['total_cost']}  [{rounds}]")
    else:
        print(f"  {label}: INFEASIBLE - {plan['reason']}")


def main() -> int:
    edges, classes, nodes = seed_catalog()
    db_path = Path(tempfile.mktemp(suffix=".db"))
    try:
        with Store.open(str(db_path)) as store:
            store.init_catalog(
                {
                    "format": "classification-catalog/1",
                    "revision": "G1",
                    "nodes": [{"id": n} for n in nodes],
                    "edges": [list(e) for e in edges],
                }
            )
            store.apply_fact_revisions(
                [
                    {
                        "ticket_id": tag,
                        "revision": 1,
                        "amount": 0,
                        "classes": [classes[tag]],
                    }
                    for tag in ("x", "y", "z")
                ],
                request_id="seed-facts",
            )

            print("# 1. Original graph: freeze a register and audit both sides")
            reg1 = store.publish_register(
                PROPOSALS, PAIRS, name="original", register_id="REG-original"
            )
            for pair in reg1["payload"]["pairs"]:
                show_vote(pair)
            show_plan(
                "send A",
                store.settle(
                    "A",
                    PROPOSALS,
                    PAIRS,
                    costs=COSTS,
                    order=PROPOSALS,
                    register="REG-original",
                ),
            )
            show_plan(
                "send A, venue bans B/C",
                store.settle(
                    "A",
                    PROPOSALS,
                    PAIRS,
                    costs=COSTS,
                    order=PROPOSALS,
                    forbidden=[["B", "C"]],
                    register="REG-original",
                ),
            )

            print("\n# 2. Submit a real edge: y's direct class now has an edge to A")
            committed = store.submit(
                {
                    "operation": "catalog",
                    "add_edges": [[classes["y"], "A"]],
                    "request_id": "submit-y-to-A",
                }
            )
            print(
                f"  committed {committed.catalog_revision}; "
                f"delta add={committed.edge_delta['add']}"
            )

            print("\n# 3. Re-weigh at the new cut with a freshly frozen register")
            reg2 = store.publish_register(
                PROPOSALS, PAIRS, name="after-edge", register_id="REG-after"
            )
            for pair in reg2["payload"]["pairs"]:
                show_vote(pair)
            show_plan(
                "send A, venue bans B/C",
                store.settle(
                    "A",
                    PROPOSALS,
                    PAIRS,
                    costs=COSTS,
                    order=PROPOSALS,
                    forbidden=[["B", "C"]],
                    register="REG-after",
                ),
            )

            print("\n# 4. Old register is immutable: the committee cannot re-weigh it")
            reread = store.get_register("REG-original")
            print(
                f"  REG-original cut {reread['payload']['cut']} "
                f"(unchanged); A/B still "
                f"{reread['payload']['pairs'][0]['left_votes']}:"
                f"{reread['payload']['pairs'][0]['right_votes']}"
            )
            print(
                "  settling A from REG-original with B/C banned stays infeasible:"
            )
            show_plan(
                "send A",
                store.settle(
                    "A",
                    PROPOSALS,
                    PAIRS,
                    costs=COSTS,
                    order=PROPOSALS,
                    forbidden=[["B", "C"]],
                    register="REG-original",
                ),
            )

            print("\n# 5. Chair order is just a tie-break, never a global ranking")
            plain = store.settle(
                "A",
                PROPOSALS,
                PAIRS,
                costs=COSTS,
                order=["C", "B", "A"],
                register="REG-after",
            )
            show_plan("send A with order C,B,A", plain)
        return 0
    finally:
        if db_path.exists():
            db_path.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
