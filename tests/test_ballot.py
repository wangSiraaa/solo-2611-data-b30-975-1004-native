"""Tests for the distance-only ballot register and the cheapest agenda.

The worked example is the one from the request: proposals A, B, C and in-force
tickets x, y, z whose distance orders are A<B<C, B<C<A and C<A<B.  That yields
the majority cycle A>B, B>C, C>A.  Adding one direct edge from y's direct class
to A makes y tie on A/B yet vote A over C; A/C flips from 1:2 to 2:1 and A/B
from 2:1 to 2:0.  With chair order A, B, C the cycle survives the venue ban on
B/C only in the edited catalog.
"""

import os
import tempfile
import unittest

from cs import ballot
from cs.store import CatalogError, Store


class Ticket:
    def __init__(self, ticket_id, classes, amount=0):
        self.ticket_id = ticket_id
        self.amount = amount
        self.classes = tuple(classes)


def branch_edges(tag, order):
    """A ticket's three independent bands of length 1, 2 and 3 to ``order``."""

    direct = f"c{tag}"
    a1 = f"a1{tag}"
    b1, b2 = f"b1{tag}", f"b2{tag}"
    return [
        (direct, order[0]),
        (direct, a1),
        (a1, order[1]),
        (direct, b1),
        (b1, b2),
        (b2, order[2]),
    ], direct


def scenario(extra_y_to_a):
    edges = []
    classes = {}
    for tag, order in (
        ("x", ["A", "B", "C"]),
        ("y", ["B", "C", "A"]),
        ("z", ["C", "A", "B"]),
    ):
        branch, direct = branch_edges(tag, order)
        edges.extend(branch)
        classes[tag] = [direct]
    if extra_y_to_a:
        edges.append((classes["y"][0], "A"))
    tickets = [Ticket(tag, classes[tag]) for tag in ("x", "y", "z")]
    return edges, tickets


def tally(edges, tickets, left, right):
    return ballot.tally_pair(left, right, edges=edges, tickets=tickets)


class BallotTests(unittest.TestCase):
    def test_shortest_path_wins_and_weight_is_one(self):
        # x: A1 B2 C3; y: B1 C2 A3; z: C1 A2 B3.
        edges, tickets = scenario(False)
        ab = tally(edges, tickets, "A", "B")
        self.assertEqual((ab["left_votes"], ab["right_votes"]), (2, 1))
        self.assertEqual(ab["winner"], "A")
        bc = tally(edges, tickets, "B", "C")
        self.assertEqual((bc["left_votes"], bc["right_votes"]), (2, 1))
        ac = tally(edges, tickets, "A", "C")
        self.assertEqual((ac["left_votes"], ac["right_votes"]), (1, 2))
        self.assertEqual(ac["winner"], "C")

    def test_zero_amount_still_votes(self):
        edges, tickets = scenario(False)
        self.assertTrue(all(t.amount == 0 for t in tickets))
        self.assertEqual(tally(edges, tickets, "A", "B")["left_votes"], 2)

    def test_many_direct_classes_still_one_vote(self):
        # A ticket with both a length-1 and a length-3 route to A, and a
        # length-2 route to B, is a single ballot won by A.  It is never
        # counted twice and the shorter route is the witness.
        edges = [("c", "A"), ("c", "a1"), ("a1", "A"), ("c", "b1"), ("b1", "B")]
        ticket = Ticket("t", ["c"])
        result = tally(edges, [ticket], "A", "B")
        self.assertEqual((result["left_votes"], result["right_votes"]), (1, 0))
        ballot_paths = result["ballots"]["left"][0]["to_left"]
        self.assertEqual(ballot_paths["distance"], 1)
        self.assertEqual(ballot_paths["path"], ["c", "A"])

    def test_equal_distance_abstains_and_is_contested(self):
        edges = [("c", "A"), ("c", "B")]
        ticket = Ticket("t", ["c"])
        result = tally(edges, [ticket], "A", "B")
        self.assertEqual((result["left_votes"], result["right_votes"]), (0, 0))
        self.assertEqual(result["abstentions"], 1)
        self.assertEqual(result["contested"], 1)
        self.assertEqual(result["contested_ticket_ids"], ["t"])
        # Both witness paths are exposed so the contested route is auditable.
        row = result["ballots"]["abstain"][0]
        self.assertEqual(row["to_left"]["path"], ["c", "A"])
        self.assertEqual(row["to_right"]["path"], ["c", "B"])

    def test_reachable_beats_unreachable(self):
        # Ticket t reaches A but has no route at all to B.
        edges = [("c", "A")]
        ticket = Ticket("t", ["c"])
        result = tally(edges, [ticket], "A", "B")
        self.assertEqual(result["left_votes"], 1)
        self.assertEqual(result["right_votes"], 0)
        row = result["ballots"]["left"][0]
        self.assertIsNone(row["to_right"]["distance"])
        self.assertIsNone(row["to_right"]["path"])

    def test_both_unreachable_abstains_not_contested(self):
        edges = [("c", "A")]
        ticket = Ticket("t", ["c"])
        result = tally(edges, [ticket], "B", "C")
        self.assertEqual(result["abstentions"], 1)
        self.assertEqual(result["contested"], 0)
        self.assertEqual(result["left_votes"], 0)

    def test_pair_order_is_not_multiplicative(self):
        # A ticket directly in both A and B, each distance 0, abstains.
        ticket = Ticket("t", ["A", "B"])
        result = tally([], [ticket], "A", "B")
        self.assertEqual(result["abstentions"], 1)
        self.assertEqual(result["contested"], 1)

    def test_pair_symmetric_vote_counts(self):
        edges, tickets = scenario(False)
        ab = tally(edges, tickets, "A", "B")
        ba = tally(edges, tickets, "B", "A")
        self.assertEqual(ab["left_votes"], ba["right_votes"])
        self.assertEqual(ab["right_votes"], ba["left_votes"])
        self.assertEqual(ab["winner"], ba["winner"])

    def test_order_does_not_change_distance_vote(self):
        edges, tickets = scenario(False)
        plain = tally(edges, tickets, "A", "B")
        ordered = ballot.tally_pair(
            "A", "B", edges=edges, tickets=tickets, order=["B", "C", "A"]
        )
        self.assertEqual(plain["left_votes"], ordered["left_votes"])
        self.assertEqual(plain["winner"], ordered["winner"])
        self.assertFalse(ordered["tie_broken_by_order"])

    def test_order_breaks_only_a_side_tie(self):
        # One ballot abstains (equal distance), so both sides have 0 votes; the
        # chair order seats the earlier proposal even though nobody voted.
        edges = [("c", "A"), ("c", "B")]
        tickets = [Ticket("t", ["c"])]
        result = ballot.tally_pair(
            "A", "B", edges=edges, tickets=tickets, order=["B", "A"]
        )
        self.assertEqual(result["winner"], "B")
        self.assertEqual(result["resolved_by"], "order")
        self.assertTrue(result["tie_broken_by_order"])
        # Without an order the same vote is an unresolved tie.
        plain = tally(edges, tickets, "A", "B")
        self.assertIsNone(plain["winner"])
        self.assertEqual(plain["resolved_by"], "tie")


class ScenarioTests(unittest.TestCase):
    """The exact A/B/C scenario stated in the request, before and after."""

    def test_original_cycle_and_two_round_cost(self):
        edges, tickets = scenario(False)
        pairs = {}
        for left, right in (("A", "B"), ("B", "C"), ("A", "C")):
            result = tally(edges, tickets, left, right)
            pairs[(left, right)] = {
                "left": left,
                "right": right,
                "winner": result["winner"],
                "cost": 1,
            }
        from cs import agenda

        # Send A up: only B/C then A/B works, cost 2.
        plan = agenda.cheapest_agenda("A", ["A", "B", "C"], pairs)
        self.assertTrue(plan["feasible"])
        self.assertEqual(plan["total_cost"], 2)
        self.assertEqual(
            [(r["left"], r["right"]) for r in plan["rounds"]],
            [("B", "C"), ("A", "B")],
        )

        # Banning B/C removes that first round, so A cannot be sent at all.
        pairs[("B", "C")] = dict(pairs[("B", "C")], cost=None)
        blocked = agenda.cheapest_agenda("A", ["A", "B", "C"], pairs)
        self.assertFalse(blocked["feasible"])

    def test_edge_edit_flips_flips_and_enables_ban_route(self):
        edges, tickets = scenario(True)
        from cs import agenda

        ab = tally(edges, tickets, "A", "B")
        ac = tally(edges, tickets, "A", "C")
        bc = tally(edges, tickets, "B", "C")
        # y is contested on A/B yet votes A over C.
        self.assertEqual((ab["left_votes"], ab["right_votes"]), (2, 0))
        self.assertEqual(ab["contested"], 1)
        self.assertEqual((ac["left_votes"], ac["right_votes"]), (2, 1))
        self.assertEqual(ac["winner"], "A")
        self.assertEqual(bc["winner"], "B")
        y_row = next(
            r
            for r in ab["ballots"]["abstain"]
            if r["ticket_id"] == "y"
        )
        self.assertEqual(y_row["to_left"]["distance"], 1)
        self.assertEqual(y_row["to_right"]["distance"], 1)
        y_ac = next(
            r for r in ac["ballots"]["left"] if r["ticket_id"] == "y"
        )
        self.assertEqual((y_ac["to_left"]["distance"], y_ac["to_right"]["distance"]), (1, 2))

        pairs = {}
        for left, right, result in (("A", "B", ab), ("B", "C", bc), ("A", "C", ac)):
            pairs[(left, right)] = {
                "left": left,
                "right": right,
                "winner": result["winner"],
                "cost": 1,
            }
        # Even if the venue bans B/C, start with an A pair, then the other
        # A pair, cost 2.  Either A round may come first (equal cost).
        pairs[("B", "C")] = dict(pairs[("B", "C")], cost=None)
        plan = agenda.cheapest_agenda(
            "A", ["A", "B", "C"], pairs, order=["A", "B", "C"]
        )
        self.assertTrue(plan["feasible"])
        self.assertEqual(plan["total_cost"], 2)
        self.assertEqual(
            {frozenset((r["left"], r["right"])) for r in plan["rounds"]},
            {frozenset(("A", "B")), frozenset(("A", "C"))},
        )
        self.assertTrue(all(r["winner"] == "A" for r in plan["rounds"]))


class AgendaTests(unittest.TestCase):
    def _table(self, verdicts, cost=1):
        table = {}
        for (a, b), winner in verdicts.items():
            key = (a, b) if a < b else (b, a)
            table[key] = {"left": a, "right": b, "winner": winner, "cost": cost}
        return table

    def test_dominator_always_reachable_at_zero_cost(self):
        from cs import agenda

        # D beats everyone; every stage is free, so the cheapest agenda costs
        # nothing and every round that involves D ends with D winning.
        votes = {
            ("A", "B"): "A",
            ("A", "C"): "C",
            ("B", "C"): "B",
            ("A", "D"): "D",
            ("B", "D"): "D",
            ("C", "D"): "D",
        }
        table = self._table(votes, cost=0)
        plan = agenda.cheapest_agenda("D", ["A", "B", "C", "D"], table, order=["A", "B", "C", "D"])
        self.assertTrue(plan["feasible"])
        self.assertEqual(plan["total_cost"], 0)
        # D must survive every round it enters; the cycle among A/B/C may be
        # broken by non-D rounds that do not touch D.
        for round_ in plan["rounds"]:
            if "D" in (round_["left"], round_["right"]):
                self.assertEqual(round_["winner"], "D")
        self.assertEqual(plan["survivors_after"], ["D"])
        self.assertEqual(len(plan["rounds"]), 3)

    def test_cycle_target_needs_two_rounds(self):
        from cs import agenda

        votes = {("A", "B"): "A", ("B", "C"): "B", ("A", "C"): "C"}
        table = self._table(votes, cost=1)
        plan = agenda.cheapest_agenda("A", ["A", "B", "C"], table)
        self.assertEqual(plan["total_cost"], 2)
        self.assertEqual(len(plan["rounds"]), 2)

    def test_infeasible_is_explicit(self):
        from cs import agenda

        # Cycle where A loses to C and C loses to B and B loses to A: with
        # only two survivors a suitable loser may still exist... build a case
        # where A is always eliminated: B beats A, C beats A, and B/C are
        # banned so one of B,C must face A and beat it.
        votes = {("A", "B"): "B", ("A", "C"): "C", ("B", "C"): "B"}
        table = self._table(votes, cost=1)
        table[("B", "C")] = dict(table[("B", "C")], cost=None)
        plan = agenda.cheapest_agenda("A", ["A", "B", "C"], table)
        self.assertFalse(plan["feasible"])
        self.assertIn("loses every pair", plan["reason"])

    def test_zero_sized_agenda_for_single_proposal(self):
        from cs import agenda

        plan = agenda.cheapest_agenda("A", ["A"], {})
        self.assertTrue(plan["feasible"])
        self.assertEqual(plan["total_cost"], 0)
        self.assertEqual(plan["rounds"], [])

    def test_tied_pair_uses_only_chair_order(self):
        from cs import agenda

        # All three pairs tie; without an order A cannot be seated.
        votes = {("A", "B"): None, ("A", "C"): None, ("B", "C"): None}
        table = self._table(votes, cost=1)
        self.assertFalse(agenda.cheapest_agenda("A", ["A", "B", "C"], table)["feasible"])
        # With order A<B<C, ties seat A against anyone, so A survives.
        plan = agenda.cheapest_agenda(
            "A", ["A", "B", "C"], table, order=["A", "B", "C"]
        )
        self.assertTrue(plan["feasible"])
        self.assertEqual(plan["total_cost"], 2)

    def test_cheapest_of_many_agendas_wins_but_ties_are_any(self):
        from cs import agenda

        # Go through B or C first (each 1) then the cheap D?  Model: A beats
        # both, but the direct A/B round is expensive; eliminating B via C
        # first is cheaper.
        votes = {("A", "B"): "A", ("A", "C"): "A", ("B", "C"): "C"}
        table = self._table(votes, cost=1)
        table[("A", "B")]["cost"] = 100
        plan = agenda.cheapest_agenda("A", ["A", "B", "C"], table)
        # Seed round C beats B (1), then A beats C (1).
        self.assertEqual(plan["total_cost"], 2)
        self.assertEqual(
            [(r["left"], r["right"], r["winner"]) for r in plan["rounds"]],
            [("B", "C", "C"), ("A", "C", "A")],
        )

    def test_no_agenda_adds_proposals_or_skips_rounds(self):
        from cs import agenda

        votes = {("A", "B"): "A", ("A", "C"): "C", ("B", "C"): "B"}
        table = self._table(votes, cost=1)
        plan = agenda.cheapest_agenda("A", ["A", "B", "C"], table)
        seen = {"A", "B", "C"}
        for round_ in plan["rounds"]:
            self.assertIn(round_["left"], seen)
            self.assertIn(round_["right"], seen)
            seen.discard(round_["loser"])
        self.assertEqual(seen, {"A"})

    def test_chair_order_is_not_a_global_ranking(self):
        from cs import agenda

        # Cycle A>B>C>A.  Chair order C, B, A ranks C first, but C still
        # cannot be sent: B/C is the only winning first round for C (B beats
        # C is false... B>C is true so C loses to B), so order alone must not
        # seat C.  Concretely C beats A, A beats B, B beats C: C must first
        # beat A, then beat B... but B beats C head-to-head, so C needs B gone,
        # and B is only beaten by A.  A cannot be sent either (B beats A... ).
        # Use the actual cycle: A>B, B>C, C>A.  C's schedule: A beats B (B
        # out), then C beats A.  Chair order says nothing about A beating B.
        votes = {("A", "B"): "A", ("B", "C"): "B", ("A", "C"): "C"}
        table = self._table(votes, cost=1)
        # Chair order puts C first, but the only schedule seating C is
        # "A beats B, then C beats A"; the order never decided a round.
        plan = agenda.cheapest_agenda(
            "C", ["A", "B", "C"], table, order=["C", "B", "A"]
        )
        self.assertTrue(plan["feasible"])
        self.assertEqual(
            [(r["left"], r["right"]) for r in plan["rounds"]],
            [("A", "B"), ("A", "C")],
        )
        # Even though C is first in the order, and C/A is tied? no: C beats A.
        # The point: order did not make C win a round it loses.
        for round_ in plan["rounds"]:
            decided = votes.get(
                (round_["left"], round_["right"]),
                votes.get((round_["right"], round_["left"])),
            )
            self.assertEqual(round_["winner"], decided)

    def test_eighteen_proposals_scale(self):
        from cs import agenda

        order = [f"A{i}" for i in range(1, 18)]
        proposals = order + ["D"]
        table = {}
        for i in range(17):
            a, b = order[i], order[(i + 1) % 17]
            key = (a, b) if a < b else (b, a)
            table[key] = {"left": a, "right": b, "winner": a, "cost": 1}
        for a in order:
            key = (a, "D") if a < "D" else ("D", a)
            table[key] = {"left": "D", "right": a, "winner": "D", "cost": 1}
        plan = agenda.cheapest_agenda("D", proposals, table, order=proposals)
        self.assertTrue(plan["feasible"])
        self.assertEqual(len(plan["rounds"]), 17)
        # A member of the 17-cycle cannot be forced through: whoever it must
        # beat last beats it.  The search must terminate quickly and say so.
        member = agenda.cheapest_agenda("A9", proposals, table, order=proposals)
        self.assertFalse(member["feasible"])

    def test_dp_matches_exhaustive_search(self):
        """The subset DP must return the true minimum, not just a schedule."""

        from cs import agenda

        def brute(target, proposals, table, order):
            best = None

            def rec(remaining, cost, rounds):
                nonlocal best
                if len(remaining) == 1:
                    if remaining[0] == target and (
                        best is None or cost < best[0]
                    ):
                        best = (cost, tuple(rounds))
                    return
                for i in range(len(remaining)):
                    for j in range(i + 1, len(remaining)):
                        a, b = remaining[i], remaining[j]
                        key = (a, b) if a < b else (b, a)
                        pair = table[key]
                        if pair["cost"] is None:
                            continue
                        winner = pair["winner"]
                        if winner is None:
                            oi = {n: k for k, n in enumerate(order)}
                            if oi[a] < oi[b]:
                                winner = a
                            else:
                                winner = b
                        loser = b if winner == a else a
                        if loser == target:
                            continue
                        rec(
                            [x for x in remaining if x != loser],
                            cost + pair["cost"],
                            rounds + [(a, b, winner, loser)],
                        )

            rec(sorted(proposals), 0, [])
            return best

        cases = [
            ({("A", "B"): "A", ("B", "C"): "B", ("A", "C"): "C"}, [1, 1, 1], "A"),
            ({("A", "B"): "B", ("B", "C"): "C", ("A", "C"): "A"}, [1, 2, 3], "A"),
            (
                {
                    ("A", "B"): "A", ("A", "C"): "C", ("A", "D"): "D",
                    ("B", "C"): "B", ("B", "D"): "D", ("C", "D"): "D",
                },
                [1, 5, 2, 1, 1, 1],
                "A",
            ),
            ({("A", "B"): "A", ("B", "C"): "B", ("A", "C"): "C"}, [0, 4, 1], "B"),
            ({("A", "B"): None, ("B", "C"): None, ("A", "C"): None}, [1, 1, 1], "A"),
        ]
        for index, (votes, costs, target) in enumerate(cases):
            proposals = ["A", "B", "C"] + (
                ["D"] if any("D" in pair for pair in votes) else []
            )
            table = {}
            for (a, b), winner in votes.items():
                idx = sorted(votes).index((a, b))
                table[(a, b)] = {
                    "left": a,
                    "right": b,
                    "winner": winner,
                    "cost": costs[idx],
                }
            for order in (proposals, list(reversed(proposals)), None):
                expected = brute(target, proposals, table, order or proposals)
                plan = agenda.cheapest_agenda(
                    target, proposals, table, order=order
                )
                if order is None:
                    # No order cannot seat a tied pair; only compare the
                    # untied cases.
                    if any(w is None for w in votes.values()):
                        continue
                if expected is None:
                    self.assertFalse(
                        plan["feasible"], f"case {index} expected infeasible"
                    )
                else:
                    self.assertTrue(
                        plan["feasible"], f"case {index} expected feasible"
                    )
                    self.assertEqual(
                        plan["total_cost"],
                        expected[0],
                        f"case {index} order {order}: not the minimum",
                    )


class StoreRegisterTests(unittest.TestCase):
    def _store(self):
        path = tempfile.mktemp(suffix=".db")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        store = Store.open(path)
        self.addCleanup(store.close)
        return store

    def _seed(self, store, extra_y_to_a):
        edges, tickets = scenario(extra_y_to_a)
        nodes = set()
        for child, parent in edges:
            nodes.add(child)
            nodes.add(parent)
        nodes.update({"A", "B", "C"})
        catalog_doc = {
            "format": "classification-catalog/1",
            "revision": "G1",
            "nodes": [{"id": n} for n in sorted(nodes)],
            "edges": [list(e) for e in edges],
        }
        store.init_catalog(catalog_doc)
        store.apply_fact_revisions(
            [
                {
                    "ticket_id": t.ticket_id,
                    "revision": 1,
                    "amount": 0,
                    "classes": list(t.classes),
                }
                for t in tickets
            ],
            request_id="seed",
        )
        return edges, tickets

    def test_register_freezes_both_sides_and_paths(self):
        store = self._store()
        self._seed(store, False)
        result = store.publish_register(
            ["A", "B", "C"],
            [["A", "B"], ["B", "C"], ["A", "C"]],
            name="original",
            register_id="R1",
        )
        payload = result["payload"]
        self.assertEqual(payload["format"], "ballot-register/1")
        self.assertEqual(payload["ticket_count"], 3)
        by_pair = {f'{p["left"]}|{p["right"]}': p for p in payload["pairs"]}
        self.assertEqual(by_pair["A|B"]["left_votes"], 2)
        self.assertEqual(by_pair["A|B"]["right_votes"], 1)
        self.assertEqual(by_pair["A|C"]["right_votes"], 2)
        # Every ballot carries both witness paths.
        for pair in payload["pairs"]:
            for side in ("left", "right", "abstain"):
                for row in pair["ballots"][side]:
                    self.assertIn("to_left", row)
                    self.assertIn("to_right", row)
        # A member can query any pair's counts, abstainers and contested routes.
        for pair in payload["pairs"]:
            self.assertIn("left_ticket_ids", pair)
            self.assertIn("right_ticket_ids", pair)
            self.assertIn("abstain_ticket_ids", pair)
            self.assertIn("contested_ticket_ids", pair)
            for row in pair["ballots"]["abstain"]:
                self.assertIn(row["ticket_id"], pair["abstain_ticket_ids"])
                if row["contested"]:
                    self.assertIn(row["ticket_id"], pair["contested_ticket_ids"])
                    self.assertIsNotNone(row["to_left"]["path"])
                    self.assertIsNotNone(row["to_right"]["path"])
        # Re-publishing the same register id is immutable.
        again = store.publish_register(
            ["A", "B", "C"],
            [["A", "B"], ["B", "C"], ["A", "C"]],
            name="original",
            register_id="R1",
        )
        self.assertEqual(again["payload"], payload)

    def test_register_rejects_foreign_pair_and_unknown_proposal(self):
        store = self._store()
        self._seed(store, False)
        with self.assertRaises(Exception):
            store.build_register(["A", "B"], [["A", "C"]])
        with self.assertRaises(Exception):
            store.build_register(["A", "Z"], [["A", "Z"]])

    def test_settle_from_register_requires_every_pair(self):
        store = self._store()
        self._seed(store, False)
        store.publish_register(["A", "B", "C"], [["A", "B"]], register_id="R-partial")
        with self.assertRaises(Exception):
            store.settle(
                "A",
                ["A", "B", "C"],
                [["A", "B"], ["A", "C"]],
                register="R-partial",
            )

    def test_all_pairs_share_one_cut(self):
        store = self._store()
        self._seed(store, False)
        payload = store.build_register(
            ["A", "B", "C"], [["A", "B"], ["B", "C"], ["A", "C"]]
        )
        # One cut for the whole register, equal to the store's current cut.
        self.assertEqual(payload["cut"], store.current_cut().as_dict())
        self.assertEqual(len(payload["pairs"]), 3)

    def test_register_is_cut_pinned(self):
        store = self._store()
        self._seed(store, False)
        before = store.publish_register(
            ["A", "B"], [["A", "B"]], register_id="R-before"
        )["payload"]
        # Add the edge; a new current register changes, the old one does not.
        edges, tickets = scenario(True)
        store.submit(
            {
                "operation": "catalog",
                "add_edges": [[tickets[1].classes[0], "A"]],
            },
            request_id="edit-y",
        )
        after = store.publish_register(
            ["A", "B"], [["A", "B"]], register_id="R-after"
        )["payload"]
        self.assertEqual(before["pairs"][0]["left_votes"], 2)
        self.assertEqual(before["pairs"][0]["right_votes"], 1)
        self.assertEqual(after["pairs"][0]["left_votes"], 2)
        self.assertEqual(after["pairs"][0]["right_votes"], 0)
        self.assertNotEqual(before["cut"], after["cut"])

    def test_settle_from_register_and_forbidden(self):
        store = self._store()
        self._seed(store, False)
        store.publish_register(
            ["A", "B", "C"],
            [["A", "B"], ["B", "C"], ["A", "C"]],
            register_id="R1",
        )
        plan = store.settle(
            "A",
            ["A", "B", "C"],
            [["A", "B"], ["B", "C"], ["A", "C"]],
            costs={f"A|B": 1, "B|C": 1, "A|C": 1},
            forbidden=[["B", "C"]],
            order=["A", "B", "C"],
            register="R1",
        )
        self.assertFalse(plan["feasible"])
        self.assertTrue(plan["from_register"])
        self.assertIn("B|C", {p["left"] + "|" + p["right"] for p in plan["pairs"] if p["forbidden"]})

        # The edited catalog, frozen in a fresh register, unlocks A via A/C.
        edges, tickets = scenario(True)
        store.submit(
            {"operation": "catalog", "add_edges": [[tickets[1].classes[0], "A"]]},
            request_id="edit-y",
        )
        store.publish_register(
            ["A", "B", "C"],
            [["A", "B"], ["B", "C"], ["A", "C"]],
            register_id="R2",
        )
        plan2 = store.settle(
            "A",
            ["A", "B", "C"],
            [["A", "B"], ["B", "C"], ["A", "C"]],
            costs={"A|B": 1, "B|C": 1, "A|C": 1},
            forbidden=[["B", "C"]],
            order=["A", "B", "C"],
            register="R2",
        )
        self.assertTrue(plan2["feasible"])
        self.assertEqual(plan2["total_cost"], 2)
        self.assertEqual(
            {frozenset((r["left"], r["right"])) for r in plan2["rounds"]},
            {frozenset(("A", "B")), frozenset(("A", "C"))},
        )

    def test_settle_persist_and_reload(self):
        store = self._store()
        self._seed(store, False)
        plan = store.settle(
            "A",
            ["A", "B", "C"],
            [["A", "B"], ["B", "C"], ["A", "C"]],
            costs={"A|B": 1, "B|C": 1, "A|C": 1},
            order=["A", "B", "C"],
            persist=True,
        )
        self.assertTrue(plan["feasible"])
        stored = store.get_settlement(plan["settlement_id"])
        self.assertEqual(stored["target"], "A")
        self.assertEqual(stored["payload"], plan)

    def test_cost_validation(self):
        store = self._store()
        self._seed(store, False)
        with self.assertRaises(Exception):
            store.settle(
                "A",
                ["A", "B"],
                [["A", "B"]],
                costs={"A|B": -1},
            )
        with self.assertRaises(Exception):
            store.settle("Z", ["A", "B"], [["A", "B"]])
        with self.assertRaises(Exception):
            store.settle("A", ["A", "B"], [["A", "B"]], forbidden=[["A", "C"]])


if __name__ == "__main__":
    unittest.main()
