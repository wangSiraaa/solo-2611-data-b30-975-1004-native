import os
import tempfile
import unittest
from fractions import Fraction

from cs import settle, split
from cs.store import Store


def parents(*edges):
    return split.catalog.build_parents(edges)


class SplitTests(unittest.TestCase):
    def test_equal_split_down_to_cabinets(self):
        p = parents(("L", "A"), ("L", "B"))
        s = split.compute_ticket_split("T", 3, 100, ["L"], p, {"A", "B"})
        self.assertEqual(s.shares(), {"A": Fraction(50), "B": Fraction(50)})
        self.assertEqual(s.total, 100)
        self.assertTrue(all(f.path[0] == "L" for f in s.flows))

    def test_direct_classes_are_deduplicated(self):
        p = parents(("L", "A"), ("M", "B"))
        s = split.compute_ticket_split("T", 1, 12, ["L", "L", "M"], p, {"A", "B"})
        # Two distinct direct classes -> 6 each, then each parent.
        self.assertEqual(s.shares(), {"A": Fraction(6), "B": Fraction(6)})

    def test_cabinet_stops_before_its_parents(self):
        p = parents(("L", "A"), ("A", "R"))
        s = split.compute_ticket_split("T", 1, 10, ["L"], p, {"A"})
        self.assertEqual(s.shares(), {"A": Fraction(10)})
        self.assertEqual([f.path for f in s.flows], [("L", "A")])

    def test_direct_class_that_is_a_cabinet(self):
        p = parents(("L", "A"))
        s = split.compute_ticket_split("T", 1, 10, ["L"], p, {"L"})
        self.assertEqual(s.shares(), {"L": Fraction(10)})
        self.assertEqual([f.path for f in s.flows], [("L",)])

    def test_no_parent_and_not_cabinet_goes_public(self):
        p = parents(("L", "A"))
        s = split.compute_ticket_split("T", 1, 10, ["L"], p, {"B"})
        self.assertEqual(s.shares(), {split.PUBLIC_CABINET: Fraction(10)})

    def test_multiple_routes_to_same_cabinet_merge(self):
        # L -> A and L -> M -> A; A is a cabinet.
        p = parents(("L", "A"), ("L", "M"), ("M", "A"))
        s = split.compute_ticket_split("T", 1, 12, ["L"], p, {"A"})
        # 6 direct to A, 6 to M then to A; merged but both witness paths kept.
        self.assertEqual(s.shares(), {"A": Fraction(12)})
        self.assertEqual(
            sorted(f.path for f in s.flows), [("L", "A"), ("L", "M", "A")]
        )

    def test_fractions_are_exact(self):
        p = parents(("L", "A"), ("L", "B"), ("L", "C"))
        s = split.compute_ticket_split("T", 1, 1, ["L"], p, {"A", "B", "C"})
        self.assertEqual(set(s.shares().values()), {Fraction(1, 3)})

    def test_ledger_verification_walks_paths(self):
        edges = [("L", "A"), ("L", "M"), ("M", "A"), ("L", "B")]
        ledger = split.build_ledger(
            [
                type(
                    "F",
                    (),
                    {"ticket_id": "T1", "revision": 1, "amount": 5, "classes": ("L",)},
                )()
            ],
            edges,
            {"A", "B"},
            {"catalog_revision": "G1", "fact_revision": "F1", "fact_watermark": 1},
        )
        good = split.verify_ledger(ledger, edges=edges, cabinets={"A", "B"})
        self.assertTrue(good["ok"], good)
        # Tamper with a witness path: it must fail.
        ledger["tickets"][0]["shares"][0]["flows"][0]["path"] = ["L", "ZZZ"]
        bad = split.verify_ledger(ledger, edges=edges, cabinets={"A", "B"})
        self.assertFalse(bad["ok"])


class SettlementTests(unittest.TestCase):
    def test_worked_example_minimum_zero_then_three_then_six(self):
        shares = {
            "X": {"A": Fraction(1, 2), "B": Fraction(1, 2)},
            "Y": {"A": Fraction(1, 2), "B": Fraction(1, 2)},
        }
        amounts = {"X": 1, "Y": 1}
        targets = {"A": 1, "B": 1}
        fees = {("X", "A"): 0, ("Y", "B"): 0, ("X", "B"): 3, ("Y", "A"): 3}

        original = settle.solve_settlement(shares, amounts, targets, fees=fees)
        self.assertEqual(original["status"], settle.STATUS_SETTLED)
        self.assertEqual(original["total_fee"], 0)
        paid = {(c["ticket_id"], c["cabinet"]): c["paid"] for c in original["cells"]}
        self.assertEqual(paid, {("X", "A"): 1, ("X", "B"): 0, ("Y", "A"): 0, ("Y", "B"): 1})

        # X directly in B, already paid 1: only Y->A may round up.
        shares_b = {"X": {"B": Fraction(1)}, "Y": shares["Y"]}
        direct_b = settle.solve_settlement(shares_b, amounts, targets, fees=fees)
        self.assertEqual(direct_b["status"], settle.STATUS_SETTLED)
        self.assertEqual(direct_b["total_fee"], 3)
        paid_b = {(c["ticket_id"], c["cabinet"]): c["paid"] for c in direct_b["cells"]}
        self.assertEqual(paid_b[("Y", "A")], 1)
        self.assertEqual(paid_b[("X", "B")], 1)
        self.assertEqual(paid_b[("Y", "B")], 0)

        # Original shares but X->B is locked to 1: X->A=0, Y->A=1, both up.
        locked = settle.solve_settlement(
            shares, amounts, targets, fees=fees, locked={("X", "B"): 1}
        )
        self.assertEqual(locked["status"], settle.STATUS_SETTLED)
        self.assertEqual(locked["total_fee"], 6)
        paid_l = {(c["ticket_id"], c["cabinet"]): c["paid"] for c in locked["cells"]}
        self.assertEqual(paid_l, {("X", "A"): 0, ("X", "B"): 1, ("Y", "A"): 1, ("Y", "B"): 0})

    def test_verifier_accepts_and_rechecks(self):
        shares = {"T": {"A": Fraction(1, 2), "B": Fraction(1, 2)}}
        amounts, targets = {"T": 1}, {"A": 1, "B": 0}
        report = settle.solve_settlement(shares, amounts, targets)
        result = settle.verify_settlement(
            report, shares=shares, amounts=amounts, targets=targets, re_solve=True
        )
        self.assertTrue(result["ok"], result)

    def test_locked_cell_is_not_moved(self):
        shares = {"T": {"A": Fraction(3, 2), "B": Fraction(3, 2)}}
        amounts, targets = {"T": 3}, {"A": 1, "B": 2}
        report = settle.solve_settlement(
            shares, amounts, targets, locked={("T", "B"): 2}
        )
        self.assertEqual(report["status"], settle.STATUS_SETTLED)
        cell = next(c for c in report["cells"] if c["cabinet"] == "B")
        self.assertEqual(cell["paid"], 2)
        self.assertEqual(cell["rounding"], "locked")

    def test_locked_outside_floor_ceil_is_infeasible(self):
        shares = {"T": {"A": Fraction(1, 2), "B": Fraction(1, 2)}}
        report = settle.solve_settlement(
            shares, {"T": 1}, {"A": 1, "B": 0}, locked={("T", "A"): 5}
        )
        self.assertEqual(report["status"], settle.STATUS_INFEASIBLE)
        self.assertTrue(report["reasons"])

    def test_column_unreachable_is_infeasible_without_moving_money(self):
        # Two tickets split 1/2-1/2 across A/B, but the teller also has a
        # cabinet C target of 1 with no fractional traffic to it.
        shares = {
            "P": {"A": Fraction(1, 2), "B": Fraction(1, 2)},
            "Q": {"A": Fraction(1, 2), "B": Fraction(1, 2)},
        }
        amounts = {"P": 1, "Q": 1}
        targets = {"A": 0, "B": 1, "C": 1}
        report = settle.solve_settlement(shares, amounts, targets)
        self.assertEqual(report["status"], settle.STATUS_INFEASIBLE)
        self.assertTrue(any("C" in r for r in report["reasons"]))
        # No payment is fabricated for the impossible column.
        self.assertTrue(all(c["paid"] is None for c in report["cells"]))
        verify = settle.verify_settlement(
            report, shares=shares, amounts=amounts, targets=targets
        )
        self.assertTrue(verify["ok"], verify)

    def test_row_target_mismatch_is_infeasible(self):
        shares = {"T": {"A": Fraction(1, 2), "B": Fraction(1, 2)}}
        report = settle.solve_settlement(shares, {"T": 2}, {"A": 1, "B": 1})
        self.assertEqual(report["status"], settle.STATUS_INFEASIBLE)

    def test_minimum_fee_is_optimal_under_exchange_argument(self):
        # Two rows, two cols, fee matrix encourages a cross assignment.
        shares = {
            "X": {"A": Fraction(1, 2), "B": Fraction(1, 2)},
            "Y": {"A": Fraction(1, 2), "B": Fraction(1, 2)},
        }
        amounts = {"X": 1, "Y": 1}
        targets = {"A": 1, "B": 1}
        fees = {("X", "A"): 5, ("Y", "B"): 5, ("X", "B"): 0, ("Y", "A"): 0}
        report = settle.solve_settlement(shares, amounts, targets, fees=fees)
        self.assertEqual(report["total_fee"], 0)
        paid = {(c["ticket_id"], c["cabinet"]): c["paid"] for c in report["cells"]}
        self.assertEqual(paid[("X", "B")], 1)
        self.assertEqual(paid[("Y", "A")], 1)


class StoreSplitTests(unittest.TestCase):
    def _store(self, testcase, catalog, facts):
        path = tempfile.mktemp(suffix=".db")
        testcase.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        store = Store.open(path)
        store.init_catalog(catalog)
        store.apply_fact_revisions(facts, request_id="seed")
        testcase.addCleanup(store.close)
        return store

    def test_single_ticket_query_reports_source_revision(self):
        store = self._store(
            self,
            {
                "format": "classification-catalog/1",
                "revision": "G1",
                "nodes": [{"id": "R"}, {"id": "A"}, {"id": "B"}, {"id": "L"}],
                "edges": [["A", "R"], ["B", "R"], ["L", "A"], ["L", "B"]],
                "cabinets": ["A", "B"],
            },
            [{"ticket_id": "T1", "revision": 7, "amount": 1, "classes": ["L"]}],
        )
        result = store.split_ticket("T1")
        self.assertEqual(result["revision"], 7)
        self.assertEqual(result["cut"]["catalog_revision"], "G1")
        self.assertEqual(result["cut"]["fact_revision"], "F1")
        shares = {s["cabinet"]: s["share"]["text"] for s in result["shares"]}
        self.assertEqual(shares, {"A": "1/2", "B": "1/2"})

    def test_split_and_fact_change_in_one_bundle_share_a_cut(self):
        store = self._store(
            self,
            {
                "format": "classification-catalog/1",
                "revision": "G1",
                "nodes": [{"id": "A"}, {"id": "B"}, {"id": "L"}],
                "edges": [["L", "A"], ["L", "B"]],
                "cabinets": ["A", "B"],
            },
            [{"ticket_id": "T1", "revision": 1, "amount": 10, "classes": ["L"]}],
        )
        # An "old bundle" updates edges and the amount at once.
        committed = store.apply_bundle(
            {
                "remove_edges": [["L", "B"]],
                "add_edges": [],
                "fact_revisions": [
                    {"ticket_id": "T1", "revision": 2, "amount": 12, "classes": ["L"]}
                ],
            },
            request_id="OLD-BUNDLE",
        )
        self.assertEqual(committed.cut.catalog_revision, "G2")
        self.assertEqual(committed.cut.fact_revision, "F2")
        ledger = store.build_split_ledger()
        self.assertEqual(ledger["cut"], committed.cut.as_dict())
        ticket = ledger["tickets"][0]
        self.assertEqual(ticket["revision"], 2)
        self.assertEqual(ticket["amount"], 12)
        shares = {s["cabinet"]: s["share"]["text"] for s in ticket["shares"]}
        self.assertEqual(shares, {"A": "12/1"})  # only the post-bundle edge
        self.assertTrue(
            split.verify_ledger(
                ledger,
                edges=store.load_catalog_state().edges,
                cabinets=store.load_catalog_state().cabinets,
            )["ok"]
        )

    def test_rejected_bundle_does_not_change_ledger(self):
        store = self._store(
            self,
            {
                "format": "classification-catalog/1",
                "revision": "G1",
                "nodes": [{"id": "A"}, {"id": "B"}, {"id": "L"}],
                "edges": [["L", "A"], ["L", "B"]],
                "cabinets": ["A", "B"],
            },
            [{"ticket_id": "T1", "revision": 1, "amount": 10, "classes": ["L"]}],
        )
        before = store.build_split_ledger()
        with self.assertRaises(Exception):
            store.apply_bundle(
                {
                    "remove_edges": [["L", "B"]],
                    "add_edges": [["A", "L"]],
                    "fact_revisions": [
                        {"ticket_id": "T1", "revision": 2, "amount": 99, "classes": ["L"]}
                    ],
                },
                request_id="BAD",
            )
        after = store.build_split_ledger()
        self.assertEqual(before, after)

    def test_classic_summary_is_unchanged_by_cabinets(self):
        store = self._store(
            self,
            {
                "format": "classification-catalog/1",
                "revision": "G1",
                "nodes": [{"id": "R"}, {"id": "A"}, {"id": "B"}, {"id": "L"}],
                "edges": [["A", "R"], ["B", "R"], ["L", "A"], ["L", "B"]],
                "cabinets": ["A", "B"],
            },
            [
                {"ticket_id": "T1", "revision": 1, "amount": 100, "classes": ["L"]},
                {"ticket_id": "T2", "revision": 1, "amount": 0, "classes": ["A"]},
                {"ticket_id": "T3", "revision": 1, "amount": 50, "classes": ["B"]},
            ],
        )
        result = store.query(["A", "B"])
        group = {g["target_id"]: g for g in result["groups"]}
        # Coverage counting: a ticket in L counts once in both A and B.
        self.assertEqual((group["A"]["count"], group["A"]["amount"]), (2, 100))
        self.assertEqual((group["B"]["count"], group["B"]["amount"]), (2, 150))
        self.assertEqual(result["union"]["count"], 3)


if __name__ == "__main__":
    unittest.main()
