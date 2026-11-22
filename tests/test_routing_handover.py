import unittest

from cs import handover as ho
from cs import routing
from cs.routing import Desk

EDGES = [("A", "R"), ("B", "R"), ("L", "A"), ("L", "B")]
NODES = {"R": "全部", "A": "办件", "B": "办件", "L": "交叉"}
DESKS = [
    Desk("A", "A", "办件一席"),
    Desk("B", "B", "办件二席"),
    Desk("R", "R", "总台"),
    Desk("A2", "A", "办件同名席"),
]


class RoutingPureTests(unittest.TestCase):
    def test_reachable_and_ancestor_prune(self):
        verdict = routing.route_ticket(["L"], DESKS, EDGES, NODES)
        # L reaches A, B, R. R is a strict ancestor of A and B -> pruned.
        self.assertEqual(set(verdict["retained_desks"]), {"A", "A2", "B"})
        pruned = {p["desk_id"]: p for p in verdict["pruned_desks"]}
        self.assertIn("R", pruned)
        self.assertEqual(
            {p["desk_id"] for p in pruned["R"]["kept_descendants"]}, {"A", "A2", "B"}
        )

    def test_same_node_desks_both_survive(self):
        verdict = routing.route_ticket(["L"], DESKS, EDGES, NODES)
        ids = set(verdict["retained_desks"])
        self.assertIn("A", ids)
        self.assertIn("A2", ids)  # same node, never a strict ancestor pair

    def test_parallel_incomparable_desks_kept(self):
        verdict = routing.route_ticket(["L"], DESKS, EDGES, NODES)
        # A and B are incomparable: both retained.
        self.assertEqual(
            verdict["retained_desks"], sorted(verdict["retained_desks"])
        )
        self.assertTrue({"A", "B"}.issubset(set(verdict["retained_desks"])))

    def test_direct_class_at_target_keeps_target(self):
        verdict = routing.route_ticket(["A"], DESKS, EDGES, NODES)
        self.assertEqual(set(verdict["retained_desks"]), {"A", "A2"})
        # R pruned in favour of the A desks; B unreachable from A.
        self.assertNotIn("B", verdict["retained_desks"])

    def test_no_reachable_desk_is_empty(self):
        desks = [Desk("L", "L", "交叉席")]
        verdict = routing.route_ticket(["A"], desks, EDGES, NODES)
        self.assertTrue(verdict["empty"])
        self.assertEqual(verdict["retained_desks"], [])

    def test_witness_path_present(self):
        verdict = routing.route_ticket(["L"], DESKS, EDGES, NODES)
        for entry in verdict["reachable_desks"]:
            self.assertTrue(entry["witness_path"])
            self.assertEqual(entry["witness_path"][0], "L")
            self.assertEqual(entry["witness_path"][-1], entry["node_id"])

    def test_checkout_separates_returned_and_withdrawn(self):
        tickets = [
            {"ticket_id": "T1", "classes": ["L"], "active": True},
            {"ticket_id": "T2", "classes": ["X"], "active": True},
            {"ticket_id": "T3", "classes": ["L"], "active": False},
        ]
        result = routing.checkout(tickets, DESKS, EDGES, NODES)
        self.assertEqual([t["ticket_id"] for t in result["actionable"]], ["T1"])
        self.assertEqual([t["ticket_id"] for t in result["returned"]], ["T2"])
        self.assertEqual([t["ticket_id"] for t in result["not_processed"]], ["T3"])

    def test_dispute_explains_same_name_and_exclusion(self):
        explanation = routing.explain_dispute(
            ["L"], DESKS, EDGES, NODES, focus_desk_ids=["A", "A2", "R", "B"]
        )
        status = {e["desk_id"]: e["status"] for e in explanation["explanations"]}
        self.assertEqual(status["A"], "retained")
        self.assertEqual(status["A2"], "retained")
        self.assertEqual(status["B"], "retained")
        self.assertEqual(status["R"], "excluded-ancestor")

    def test_dispute_unreachable_desk(self):
        explanation = routing.explain_dispute(
            ["A"], DESKS, EDGES, NODES, focus_desk_ids=["B"]
        )
        self.assertEqual(explanation["explanations"][0]["status"], "excluded-unreachable")

    def test_duplicate_desk_id_rejected(self):
        with self.assertRaises(ValueError):
            routing.normalise_desks(
                [
                    {"desk_id": "X", "node_id": "A"},
                    {"desk_id": "X", "node_id": "B"},
                ]
            )


def problem(sealed=None, quotas=None, holder=None, cost=None):
    ticket_ids = ["T1", "T2", "T3"]
    units = ho.build_units(ticket_ids, sealed or [])
    eligibility = {
        "T1": frozenset({"A", "B"}),
        "T2": frozenset({"A", "B"}),
        "T3": frozenset({"B"}),
    }
    return ho.HandoverProblem(
        units=units,
        eligibility=eligibility,
        quotas=quotas or {"A": 2, "B": 3},
        holder=holder or {"T1": "A", "T2": "A", "T3": "B"},
        transfer_cost=cost or {"T1": 1, "T2": 1, "T3": 1},
    )


class HandoverPureTests(unittest.TestCase):
    def test_zero_cost_when_everyone_stays(self):
        result = ho.solve(problem(sealed=[["T1", "T2"]]))
        self.assertTrue(result["feasible"])
        self.assertEqual(result["total_workhours"], 0)
        sealed = next(a for a in result["assignments"] if a["kind"] == "sealed")
        self.assertEqual(sealed["desk_id"], "A")

    def test_sealed_group_moves_whole(self):
        # A has only one slot: the two-ticket seal cannot fit, must move to B.
        result = ho.solve(problem(sealed=[["T1", "T2"]], quotas={"A": 1, "B": 3}))
        self.assertTrue(result["feasible"])
        sealed = next(a for a in result["assignments"] if a["kind"] == "sealed")
        self.assertEqual(sealed["desk_id"], "B")
        desks = {p["to_desk"] for p in sealed["per_ticket"]}
        self.assertEqual(desks, {"B"})
        self.assertEqual(result["total_workhours"], 2)

    def test_never_split_seal_across_two_desks(self):
        result = ho.solve(problem(sealed=[["T1", "T2"]], quotas={"A": 1, "B": 3}))
        sealed = next(a for a in result["assignments"] if a["kind"] == "sealed")
        self.assertEqual(len({p["to_desk"] for p in sealed["per_ticket"]}), 1)

    def test_infeasible_when_group_cannot_fit_anywhere(self):
        # Both desks hold less than the two-ticket seal.
        result = ho.solve(problem(sealed=[["T1", "T2"]], quotas={"A": 1, "B": 1}))
        self.assertFalse(result["feasible"])
        self.assertTrue(result["blockers"])
        self.assertEqual(result["assignments"], [])

    def test_loose_tickets_distribute_independently(self):
        result = ho.solve(problem(sealed=[]))
        self.assertTrue(result["feasible"])
        by_ticket = {
            p["ticket_id"]: p["to_desk"]
            for a in result["assignments"]
            for p in a["per_ticket"]
        }
        self.assertEqual(by_ticket, {"T1": "A", "T2": "A", "T3": "B"})

    def test_deterministic_tie_break(self):
        # Every ticket is eligible at both desks and nobody has a previous
        # desk, so all plans cost the same; the lexicographically smallest one
        # must be chosen and be stable across runs.
        p = problem(sealed=[])
        p.eligibility = {
            "T1": frozenset({"A", "B"}),
            "T2": frozenset({"A", "B"}),
            "T3": frozenset({"A", "B"}),
        }
        p.holder = {}  # everything costs 1 -> tie on total workhours
        first = ho.solve(p)
        second = ho.solve(p)
        self.assertEqual(
            [(a["unit_id"], a["desk_id"]) for a in first["assignments"]],
            [(a["unit_id"], a["desk_id"]) for a in second["assignments"]],
        )
        # Loose units are processed in sorted ticket order; each picks A first.
        self.assertEqual(
            [(a["unit_id"], a["desk_id"]) for a in first["assignments"]],
            [("L:T1", "A"), ("L:T2", "A"), ("L:T3", "B")],
        )

    def test_capacity_conflict_blocker(self):
        # Two loose tickets, each only eligible for B which has one slot:
        # each unit fits individually but they cannot fit together.
        units = ho.build_units(["T1", "T2"], [])
        p = ho.HandoverProblem(
            units=units,
            eligibility={"T1": frozenset({"B"}), "T2": frozenset({"B"})},
            quotas={"B": 1},
            holder={},
            transfer_cost={},
        )
        result = ho.solve(p)
        self.assertFalse(result["feasible"])
        self.assertEqual(result["blockers"][0]["reason"], "capacity-conflict")

    def test_insufficient_capacity_blocker_for_seal(self):
        # A sealed group needs two slots but every eligible desk holds one.
        units = ho.build_units(["T1", "T2"], [["T1", "T2"]])
        p = ho.HandoverProblem(
            units=units,
            eligibility={"T1": frozenset({"B"}), "T2": frozenset({"B"})},
            quotas={"B": 1},
            holder={},
            transfer_cost={},
        )
        result = ho.solve(p)
        self.assertFalse(result["feasible"])
        self.assertTrue(
            any(b["reason"] == "insufficient-capacity" for b in result["blockers"])
        )

    def test_build_units_rejects_ticket_in_two_seals(self):
        with self.assertRaises(ValueError):
            ho.build_units(["T1", "T2"], [["T1", "T2"], ["T1"]])

    def test_build_units_rejects_unknown_ticket(self):
        with self.assertRaises(ValueError):
            ho.build_units(["T1"], [["T1", "T9"]])


if __name__ == "__main__":
    unittest.main()
