import json
import os
import tempfile
import unittest

from cs.store import (
    Cut,
    HandoverError,
    RoutingError,
    Store,
)

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


def fresh_store(testcase) -> Store:
    path = tempfile.mktemp(suffix=".db")
    testcase.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
    store = Store.open(path)
    testcase.addCleanup(store.close)
    store.init_catalog(CATALOG)
    store.apply_fact_revisions(FACTS, request_id="seed")
    store.register_desks(DESKS, request_id="desk-D1")
    store.create_roster("roster-1", {"A": 2, "B": 3, "R": 5}, description="接班一班")
    return store


class RegistryRoutingStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = fresh_store(self)

    def test_registry_revision_and_cut(self):
        result = self.store.register_desks(DESKS, request_id="again")
        self.assertTrue(result["noop"])
        changed = self.store.register_desks(
            DESKS + [{"desk_id": "L1", "node_id": "L", "name": "交叉席"}],
            request_id="desk-D2",
        )
        self.assertEqual(changed["registry_revision"], "D2")
        self.assertEqual(self.store.registry_revision(), "D2")

    def test_desk_on_unknown_node_rejected(self):
        with self.assertRaises(RoutingError):
            self.store.register_desks(
                [{"desk_id": "X", "node_id": "NOPE", "name": "x"}],
                request_id="bad",
            )

    def test_routing_checkout_prunes_ancestor(self):
        result = self.store.routing_checkout(["T1"])
        entry = result["actionable"][0]
        self.assertEqual(entry["retained_desks"], ["A", "B"])
        self.assertEqual([p["desk_id"] for p in entry["pruned_desks"]], ["R"])
        self.assertEqual(result["cut"]["catalog_revision"], "G1")

    def test_empty_set_returns_for_registration(self):
        # Remove L->A and L->B: no desk is reachable from L any more.
        self.store.submit(
            {
                "operation": "catalog",
                "remove_edges": [["L", "A"], ["L", "B"]],
            },
            request_id="isolate-L",
        )
        result = self.store.routing_checkout(["T1"])
        self.assertEqual(result["actionable"], [])
        self.assertEqual(result["returned"][0]["ticket_id"], "T1")

    def test_withdrawn_ticket_not_processed(self):
        self.store.withdraw("T1", 1)
        result = self.store.routing_checkout(["T1", "T2"])
        self.assertEqual(result["not_processed"][0]["ticket_id"], "T1")
        self.assertEqual([a["ticket_id"] for a in result["actionable"]], ["T2"])

    def test_routing_signoff_pinned_to_cut(self):
        signed = self.store.sign_routing("route-1", ["T1"])
        old_payload = signed["payload"]
        self.store.submit(
            {"operation": "catalog", "remove_edges": [["L", "A"]]}, request_id="c1"
        )
        # Stored sign-off is immutable and still on the old cut.
        reread = self.store.get_routing_signoff("route-1")["payload"]
        self.assertEqual(reread, old_payload)
        self.assertEqual(reread["cut"]["catalog_revision"], "G1")
        # A fresh checkout reflects the new catalog revision.
        fresh = self.store.routing_checkout(["T1"])
        self.assertEqual(fresh["cut"]["catalog_revision"], "G2")

    def test_routing_signoff_immutable(self):
        self.store.sign_routing("route-x", ["T1"])
        self.store.submit(
            {"operation": "catalog", "remove_edges": [["L", "A"]]}, request_id="c2"
        )
        with self.assertRaises(RoutingError):
            self.store.sign_routing("route-x", ["T1"])

    def test_dispute_same_name_two_desks_and_excluded(self):
        # Register a second desk on A: two desks may share a display name.
        self.store.register_desks(
            DESKS + [{"desk_id": "A2", "node_id": "A", "name": "办件一席"}],
            request_id="desk-D2",
        )
        explanation = self.store.routing_explain("T1")
        status = {e["desk_id"]: e["status"] for e in explanation["explanations"]}
        self.assertEqual(status["A"], "retained")
        self.assertEqual(status["A2"], "retained")
        self.assertEqual(status["R"], "excluded-ancestor")

    def test_historical_registry_revision_still_loads(self):
        self.store.register_desks(
            [{"desk_id": "A", "node_id": "A", "name": "一席"}], request_id="desk-D2"
        )
        old = self.store.load_desk_registry("D1")
        self.assertEqual({d.desk_id for d in old}, {"A", "B", "R"})


class HandoverStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = fresh_store(self)

    def test_first_verdict_zero_workhours(self):
        result = self.store.handover("roster-1", STACK, signoff_id="H1")
        self.assertEqual(result["status"], "signed")
        plan = result["plan"]
        self.assertEqual(plan["total_workhours"], 0)
        sealed = next(a for a in plan["assignments"] if a["kind"] == "sealed")
        self.assertEqual(sealed["desk_id"], "A")
        roster = self.store.get_roster("roster-1")
        totals = {q["desk_id"]: q for q in roster["quotas"]}
        self.assertEqual(totals["A"]["used"], 2)
        self.assertEqual(totals["B"]["used"], 1)

    def test_edge_removal_re_adjudicates_to_b(self):
        self.store.handover("roster-1", STACK, signoff_id="H1")
        self.store.apply_bundle(
            {"remove_edges": [["L", "A"]], "add_edges": [], "fact_revisions": []},
            request_id="drop-LA",
        )
        result = self.store.handover("roster-1", STACK, signoff_id="H2")
        self.assertEqual(result["plan"]["total_workhours"], 2)
        desks = {a["desk_id"] for a in result["plan"]["assignments"]}
        self.assertEqual(desks, {"B"})
        # H1 is superseded and no longer consumes slots; roster shows H2 only.
        self.assertEqual(self.store.get_handover("H1")["status"], "superseded")
        totals = {q["desk_id"]: q for q in self.store.get_roster("roster-1")["quotas"]}
        self.assertEqual(totals["A"]["used"], 0)
        self.assertEqual(totals["B"]["used"], 3)

    def test_other_order_takes_slot_then_group_moves_whole(self):
        self.store.handover("roster-1", STACK, signoff_id="H1")
        # The old handover is wound down; another order then occupies one of
        # A's two slots before the main stack is re-adjudicated.
        self.store.release_handover("H1")
        self.store.apply_fact_revisions(
            [{"ticket_id": "T4", "revision": 1, "amount": 0, "classes": ["A"]}],
            request_id="add-T4",
        )
        other = {
            "stack_id": "stack-other",
            "tickets": [{"ticket_id": "T4", "from_desk": "A", "transfer_cost": 1}],
            "sealed_groups": [],
        }
        self.store.handover("roster-1", other, signoff_id="H-other")
        # Restore the original catalog shape and re-sign the main stack.
        self.store.apply_bundle(
            {"add_edges": [["L", "A"]], "remove_edges": [], "fact_revisions": []},
            request_id="restore-LA",
        )
        result = self.store.handover("roster-1", STACK, signoff_id="H3")
        sealed = next(a for a in result["plan"]["assignments"] if a["kind"] == "sealed")
        # Only one A slot remains: the two-ticket seal cannot split, so it goes B.
        self.assertEqual(sealed["desk_id"], "B")
        self.assertEqual(result["plan"]["total_workhours"], 2)
        roster = {q["desk_id"]: q for q in self.store.get_roster("roster-1")["quotas"]}
        self.assertEqual(roster["A"]["used"], 1)  # only the other order remains
        self.assertEqual(roster["B"]["used"], 3)
        # The released document is retained for the audit trail.
        self.assertEqual(self.store.get_handover("H1")["status"], "released")

    def test_infeasible_handover_keeps_previous_state(self):
        self.store.handover("roster-1", STACK, signoff_id="H1")
        # A tight roster on a new roster id: every desk quota below seal size.
        self.store.create_roster("roster-tight", {"A": 1, "B": 1, "R": 0})
        failed = self.store.handover("roster-tight", STACK, signoff_id="H-fail")
        self.assertEqual(failed["status"], "unchanged")
        self.assertFalse(failed["plan"]["feasible"])
        self.assertTrue(failed["plan"]["blockers"])
        # No slots were consumed on the tight roster.
        self.assertTrue(
            all(q["used"] == 0 for q in self.store.get_roster("roster-tight")["quotas"])
        )

    def test_withdrawn_ticket_rejected_from_handover(self):
        self.store.withdraw("T3", 1)
        with self.assertRaises(HandoverError):
            self.store.handover("roster-1", STACK, signoff_id="H-bad")

    def test_stack_guardrails(self):
        oversized = {
            "stack_id": "big",
            "tickets": [
                {"ticket_id": f"X{i}", "from_desk": "A", "transfer_cost": 1}
                for i in range(121)
            ],
            "sealed_groups": [],
        }
        with self.assertRaises(HandoverError):
            self.store.handover("roster-1", oversized, signoff_id="H-size")
        many_seals = {
            "stack_id": "seals",
            "tickets": [
                {"ticket_id": tid, "from_desk": "A", "transfer_cost": 1}
                for tid in ("T1", "T2", "T3")
            ],
            "sealed_groups": [["T1", "T2", "T3"]] * 9,
        }
        with self.assertRaises(HandoverError):
            self.store.handover("roster-1", many_seals, signoff_id="H-seals")

    def test_routing_empty_blocks_handover(self):
        self.store.submit(
            {"operation": "catalog", "remove_edges": [["L", "A"], ["L", "B"]]},
            request_id="isolate",
        )
        with self.assertRaises(HandoverError):
            self.store.handover("roster-1", STACK, signoff_id="H-noroute")

    def test_roster_unknown_desk_rejected(self):
        with self.assertRaises(HandoverError):
            self.store.create_roster("roster-x", {"NOPE": 2})

    def test_signed_document_lists_net_slot_occupation(self):
        result = self.store.handover("roster-1", STACK, signoff_id="H1")
        totals = {t["desk_id"]: t for t in result["plan"]["desk_totals"]}
        self.assertEqual(totals["A"]["used"], 2)
        self.assertEqual(totals["A"]["capacity"], 2)
        self.assertEqual(totals["B"]["used"], 1)
        self.assertEqual(totals["B"]["capacity"], 3)

    def test_handover_idempotent_by_request(self):
        first = self.store.handover(
            "roster-1", STACK, signoff_id="H1", request_id="req-1"
        )
        second = self.store.handover(
            "roster-1", STACK, signoff_id="H1", request_id="req-1"
        )
        self.assertEqual(first["status"], second["status"])

    def test_old_summary_product_still_readable(self):
        product = self.store.publish(["A", "B"], name="before", product_id="P1")
        self.store.apply_bundle(
            {"remove_edges": [["L", "A"]], "fact_revisions": []}, request_id="b"
        )
        reread = self.store.get_product("P1")["payload"]
        self.assertEqual(reread, product["payload"])
        self.assertEqual(reread["format"], "classification-summary/1")

    def test_loose_only_shift(self):
        loose_stack = {
            "stack_id": "loose",
            "tickets": [
                {"ticket_id": "T3", "from_desk": "B", "transfer_cost": 1}
            ],
            "sealed_groups": [],
        }
        result = self.store.handover("roster-1", loose_stack, signoff_id="HL")
        self.assertTrue(all(a["kind"] == "loose" for a in result["plan"]["assignments"]))
        self.assertEqual(result["plan"]["total_workhours"], 0)

    def test_large_seal_consumes_most_of_a_desk(self):
        # A three-ticket seal all eligible at B (cap 3) takes the whole desk;
        # a fourth B-only loose ticket then has nowhere left and is rejected.
        for index in range(4, 6):
            self.store.apply_fact_revisions(
                [
                    {
                        "ticket_id": f"T{index}",
                        "revision": 1,
                        "amount": 0,
                        "classes": ["B"],
                    }
                ],
                request_id=f"add-{index}",
            )
        big_stack = {
            "stack_id": "big",
            "tickets": [
                {"ticket_id": tid, "from_desk": "B", "transfer_cost": 1}
                for tid in ("T3", "T4", "T5")
            ],
            "sealed_groups": [["T3", "T4", "T5"]],
        }
        accepted = self.store.handover("roster-1", big_stack, signoff_id="HB")
        self.assertEqual(accepted["status"], "signed")
        b_total = next(t for t in accepted["plan"]["desk_totals"] if t["desk_id"] == "B")
        self.assertEqual((b_total["used"], b_total["remaining"]), (3, 0))
        overflow = {
            "stack_id": "overflow",
            "tickets": [
                {"ticket_id": "T3", "from_desk": "B", "transfer_cost": 1}
            ],
            "sealed_groups": [],
        }
        rejected = self.store.handover("roster-1", overflow, signoff_id="HO")
        self.assertEqual(rejected["status"], "unchanged")
        self.assertTrue(rejected["plan"]["blockers"])

    def test_resign_after_catalog_change_pins_each_plan_to_its_cut(self):
        first = self.store.handover("roster-1", STACK, signoff_id="H1")
        self.assertEqual(first["cut"]["catalog_revision"], "G1")
        self.store.apply_bundle(
            {"remove_edges": [["L", "A"]], "fact_revisions": []}, request_id="edge"
        )
        second = self.store.handover("roster-1", STACK, signoff_id="H2")
        self.assertEqual(second["cut"]["catalog_revision"], "G2")
        # Both documents remain readable, each on its own cut.
        self.assertEqual(
            self.store.get_handover("H1")["cut"]["catalog_revision"], "G1"
        )
        self.assertEqual(
            self.store.get_handover("H2")["cut"]["catalog_revision"], "G2"
        )

    def test_routing_uses_live_registry_after_revision(self):
        before = self.store.routing_checkout(["T1"])
        self.assertEqual(
            set(before["actionable"][0]["retained_desks"]), {"A", "B"}
        )
        # Deregister desk B: only A (plus the pruned R) remains reachable.
        self.store.register_desks(
            [
                {"desk_id": "A", "node_id": "A", "name": "办件一席"},
                {"desk_id": "R", "node_id": "R", "name": "总台"},
            ],
            request_id="desk-D2",
        )
        after = self.store.routing_checkout(["T1"])
        self.assertEqual(after["actionable"][0]["retained_desks"], ["A"])
        # The historical registry revision D1 still resolves desk B.
        old = self.store.routing_checkout(["T1"], registry_revision="D1")
        self.assertEqual(
            set(old["actionable"][0]["retained_desks"]), {"A", "B"}
        )


if __name__ == "__main__":
    unittest.main()
