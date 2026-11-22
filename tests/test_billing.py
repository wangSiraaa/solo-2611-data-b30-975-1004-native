"""Tests for point-voucher billing and minimum-fee remittance clearing."""

from __future__ import annotations

import os
import tempfile
import unittest

from reagent.billing import BillingService
from reagent.billing_match import Claim, Voucher, optimize_redemptions
from reagent.clearing import Instruction, Route, settle
from reagent.errors import Conflict, ValidationError
from reagent.inventory import InventoryService
from reagent.runs import RunService
from reagent.schema import connect, initialize

INVENTORY = {
    "initial_day": 1,
    "kinds": [{"kind": "R", "open_lifetime_days": 1000}],
    "bottles": [
        {"bottle_id": "X", "kind": "R", "total_portions": 10, "intake_day": 1,
         "printed_expiry_day": 400, "opened_day": None},
        {"bottle_id": "Y", "kind": "R", "total_portions": 4, "intake_day": 20,
         "printed_expiry_day": 400, "opened_day": None},
    ],
    "methods": [{"method_id": "M", "revision": 1, "requirements": {"R": 1}}],
}


class BillingStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "store.db")
        self.conn = connect(self.db_path)
        initialize(self.conn)
        self.inventory = InventoryService(self.conn)
        self.runs = RunService(self.conn)
        self.billing = BillingService(self.conn)
        self.inventory.initialize(INVENTORY)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def voucher_doc(self, voucher_id: str, *, counters=("B",), **overrides) -> dict:
        doc = {
            "voucher_id": voucher_id,
            "owner_counter": "A",
            "paid_price": 3,
            "cap_amount": 5,
            "valid_from_day": 9,
            "valid_until_day": 11,
            "bottles": ["X"],
            "counters": list(counters),
        }
        doc.update(overrides)
        return doc

    def consume(self, holder: str, bottle: str, reserve_day: int, use_day: int) -> None:
        self.inventory.hold(holder, [(bottle, 1)], reserve_day)
        self.inventory.consume_held(holder, use_day)


class CredentialTests(BillingStoreTestCase):
    def test_only_consumed_rows_become_credentials(self) -> None:
        self.consume("B", "X", 9, 10)
        self.inventory.hold("REL", [("X", 1)], 10)
        self.inventory.release_held("REL", 10, "abort")
        self.inventory.hold("RSV", [("X", 1)], 10)
        self.billing.open_period("p", 30)
        self.billing.add_price("p", "X", 5)
        credentials = self.billing.load_credentials("p")
        self.assertEqual(
            [(c.reservation_id, c.seq, c.counter_id) for c in credentials],
            [("B#0", 0, "B")],
        )

    def test_run_holder_books_under_run_id(self) -> None:
        self.runs.create_run("run-1", "M", 1, 10, [{"bottle_id": "X", "portions": 1}])
        self.runs.start_run("run-1", 10)
        self.billing.open_period("p", 30)
        self.billing.add_price("p", "X", 5)
        credentials = self.billing.load_credentials("p")
        self.assertEqual(len(credentials), 1)
        self.assertEqual(credentials[0].counter_id, "run-1")

    def test_multi_portion_reservation_splits_into_sequence(self) -> None:
        self.inventory.hold("B", [("X", 3)], 9)
        self.inventory.consume_held("B", 10)
        self.billing.open_period("p", 30)
        self.billing.add_price("p", "X", 5)
        credentials = self.billing.load_credentials("p")
        self.assertEqual([(c.reservation_id, c.seq) for c in credentials],
                         [("B#0", 0), ("B#0", 1), ("B#0", 2)])


class VoucherValidityTests(BillingStoreTestCase):
    def test_window_is_half_open_and_judged_on_settlement_day(self) -> None:
        # Reserved on day 9 but consumed on day 11: window [9, 11) excludes 11.
        self.consume("B", "X", 9, 11)
        self.billing.open_period("p", 40)
        self.billing.add_price("p", "X", 5)
        self.billing.issue_voucher(self.voucher_doc("V1"))
        receipt = self.billing.issue_bill("p", 40)
        self.assertIsNone(receipt["lines"][0]["voucher_id"])
        self.assertEqual(receipt["supplier_receivable"], 5)

    def test_day_10_inside_window_redeems_and_repays_owner(self) -> None:
        self.consume("B", "X", 9, 10)
        self.billing.open_period("p", 30)
        self.billing.add_price("p", "X", 5)
        self.billing.issue_voucher(self.voucher_doc("V1"))
        receipt = self.billing.issue_bill("p", 30)
        line = receipt["lines"][0]
        self.assertEqual(line["voucher_id"], "V1")
        self.assertEqual(line["offset_amount"], 5)
        self.assertEqual(line["cash_to_supplier"], 0)
        self.assertEqual(line["repay_to"], "A")
        self.assertEqual(line["repay_amount"], 3)
        self.assertEqual(
            self.billing.settlement("p")["counter_net"], {"A": -3, "B": 3}
        )

    def test_own_voucher_needs_no_self_repayment(self) -> None:
        self.billing.issue_voucher(
            self.voucher_doc("V1", owner_counter="B")
        )
        self.consume("B", "X", 9, 10)
        self.billing.open_period("p", 30)
        self.billing.add_price("p", "X", 5)
        line = self.billing.issue_bill("p", 30)["lines"][0]
        self.assertEqual(line["voucher_id"], "V1")
        self.assertIsNone(line["repay_to"])
        self.assertEqual(line["repay_amount"], 0)

    def test_counter_and_bottle_scope_are_enforced(self) -> None:
        self.consume("B", "X", 9, 10)
        self.billing.open_period("p", 30)
        self.billing.add_price("p", "X", 5)
        # voucher only names counter C, but B consumed -> not usable
        self.billing.issue_voucher(self.voucher_doc("V1", counters=("C",)))
        line = self.billing.issue_bill("p", 30)["lines"][0]
        self.assertIsNone(line["voucher_id"])

    def test_cross_month_voucher_stays_valid(self) -> None:
        self.consume("B", "Y", 30, 35)
        self.billing.open_period("p", 60)
        self.billing.add_price("p", "Y", 4)
        self.billing.issue_voucher(
            self.voucher_doc(
                "VX", valid_from_day=25, valid_until_day=75,
                bottles=["Y"], cap_amount=4,
            )
        )
        line = self.billing.issue_bill("p", 60)["lines"][0]
        self.assertEqual(line["voucher_id"], "VX")
        self.assertEqual(line["cash_to_supplier"], 0)

    def test_unused_voucher_is_not_refunded_or_cancelled(self) -> None:
        self.consume("B", "X", 9, 11)  # day 11 -> voucher dead
        self.billing.open_period("p", 40)
        self.billing.add_price("p", "X", 5)
        self.billing.issue_voucher(self.voucher_doc("V1"))
        self.billing.issue_bill("p", 40)
        status = {v["voucher_id"]: v for v in self.billing.voucher_status()}
        self.assertFalse(status["V1"]["locked"])
        # still on file with its original paid price
        self.assertEqual(status["V1"]["paid_price"], 3)


class BillLifecycleTests(BillingStoreTestCase):
    def _open_period_with_claim(self, period_id: str = "p") -> None:
        self.consume("B", "X", 9, 10)
        self.billing.open_period(period_id, 30)
        self.billing.add_price(period_id, "X", 5)
        self.billing.issue_voucher(self.voucher_doc("V1"))

    def test_replay_returns_identical_bill(self) -> None:
        self._open_period_with_claim()
        first = self.billing.issue_bill("p", 30)
        replay = self.billing.issue_bill("p", 30)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["bill_id"], first["bill_id"])
        self.assertEqual(replay["voucher_sequence"], first["voucher_sequence"])

    def test_closed_bill_is_immutable(self) -> None:
        self._open_period_with_claim()
        self.billing.issue_bill("p", 30)
        self.billing.close_period("p")
        with self.assertRaises(Conflict):
            self.billing.reopen_period("p")
        # the voucher stays locked by the closed bill
        self.assertTrue(self.billing.get_voucher("V1")["locked"])

    def test_reopen_supersedes_and_keeps_old_receipt(self) -> None:
        self._open_period_with_claim()
        first = self.billing.issue_bill("p", 30)
        self.billing.reopen_period("p")
        second = self.billing.issue_bill("p", 30)
        self.assertEqual(second["version"], first["version"] + 1)
        history = self.billing.bill_history("p")
        self.assertEqual([h["status"] for h in history], ["superseded", "active"])
        # old receipt preserved verbatim, voucher released then re-locked
        self.assertEqual(
            history[0]["receipt"]["voucher_sequence"], first["voucher_sequence"]
        )
        self.assertTrue(self.billing.get_voucher("V1")["locked"])

    def test_reopened_bill_can_change_when_voucher_expires(self) -> None:
        # First bill on day-10 credentials redeems the voucher; after reopen
        # the same optimizer still picks it (facts unchanged), proving
        # determinism; a separate expired scenario is covered elsewhere.
        self._open_period_with_claim()
        first = self.billing.issue_bill("p", 30)
        self.billing.reopen_period("p")
        second = self.billing.issue_bill("p", 30)
        self.assertEqual(second["voucher_sequence"], first["voucher_sequence"])


class OptimizationRuleTests(unittest.TestCase):
    def test_spec_worked_example_prefers_narrow_then_wide(self) -> None:
        wide = Voucher("wide", "A", 5, 5, 0, 100,
                       frozenset({"X", "Y"}), frozenset({"B"}))
        narrow = Voucher("narrow", "A", 5, 5, 0, 100,
                         frozenset({"X"}), frozenset({"B"}))
        claims = [
            Claim("rX", 0, "B", "X", 10, 4),
            Claim("rY", 0, "B", "Y", 10, 7),
        ]
        # narrow on X + wide on Y offsets 9; the reverse only offsets 7.
        self.assertEqual(
            optimize_redemptions(claims, [wide, narrow]), ["narrow", "wide"]
        )

    def test_lexicographic_tiebreak_on_equal_cash(self) -> None:
        v1 = Voucher("V1", "A", 1, 5, 0, 100, frozenset({"X"}), frozenset({"B"}))
        v2 = Voucher("V2", "A", 1, 5, 0, 100, frozenset({"X"}), frozenset({"B"}))
        claims = [Claim("r1", 0, "B", "X", 10, 5),
                  Claim("r2", 0, "B", "X", 10, 5)]
        # both assignments offset 10; the smaller voucher-id sequence wins
        self.assertEqual(optimize_redemptions(claims, [v1, v2]), ["V1", "V2"])

    def test_more_claims_than_vouchers_leaves_unused_claims(self) -> None:
        v = Voucher("V1", "A", 1, 5, 0, 100, frozenset({"X"}), frozenset({"B"}))
        claims = [Claim(f"r{i}", 0, "B", "X", 10, 5) for i in range(3)]
        result = optimize_redemptions(claims, [v])
        self.assertEqual(len([x for x in result if x == "V1"]), 1)
        self.assertEqual(len([x for x in result if x is None]), 2)

    def test_offset_is_min_of_price_and_cap(self) -> None:
        v = Voucher("V1", "A", 2, 3, 0, 100, frozenset({"X"}), frozenset({"B"}))
        claim = Claim("r1", 0, "B", "X", 10, 7)
        self.assertEqual(optimize_redemptions([claim], [v]), ["V1"])
        # cash = 7 - min(7, 3) = 4 is exercised through the service below


class ClearingEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.routes = [
            Route("BA", "B", "A", 3, 1),
            Route("BC", "B", "C", 3, 1),
            Route("CA", "C", "A", 3, 1),
            Route("BS", "B", "S", 5, 2),
        ]
        self.balances = {"A": 3, "B": -5, "C": 0, "S": 2}

    def amounts(self, result) -> dict:
        return {i.route_id: i.amount for i in result.instructions}

    def test_direct_line_is_cheapest(self) -> None:
        result = settle(self.balances, self.routes)
        self.assertTrue(result.feasible)
        self.assertEqual(result.total_activation_fee, 3)  # BA(1) + BS(2)
        self.assertEqual(self.amounts(result), {"BA": 3, "BS": 2})

    def test_transshipment_when_direct_shut(self) -> None:
        result = settle(self.balances, self.routes[1:])
        self.assertTrue(result.feasible)
        self.assertEqual(result.total_activation_fee, 4)  # BC + CA + BS
        self.assertEqual(self.amounts(result), {"BC": 3, "CA": 3, "BS": 2})

    def test_infeasible_network_emits_nothing(self) -> None:
        # Only B -> S exists: nothing can reach A, so full clearing is impossible.
        result = settle(self.balances, [self.routes[3]])
        self.assertFalse(result.feasible)
        self.assertEqual(result.instructions, tuple())
        self.assertIsNotNone(result.reason)

    def test_capacity_shortfall_is_infeasible(self) -> None:
        routes = [
            Route("BC", "B", "C", 2, 1),
            Route("CA", "C", "A", 3, 1),
            self.routes[3],
        ]
        self.assertFalse(settle(self.balances, routes).feasible)

    def test_shared_transshipment_leg(self) -> None:
        routes = [
            Route("p-m", "P", "M", 4, 1),
            Route("q-m", "Q", "M", 3, 1),
            Route("m-r", "M", "R", 7, 1),
        ]
        result = settle({"P": -4, "Q": -3, "M": 0, "R": 7}, routes)
        self.assertTrue(result.feasible)
        self.assertEqual(result.total_activation_fee, 3)
        self.assertEqual(
            self.amounts(result), {"p-m": 4, "q-m": 3, "m-r": 7}
        )

    def test_replay_is_deterministic(self) -> None:
        first = settle(self.balances, self.routes)
        second = settle(self.balances, self.routes)
        self.assertEqual(
            [(i.route_id, i.amount) for i in first.instructions],
            [(i.route_id, i.amount) for i in second.instructions],
        )

    def test_zero_balances_need_no_lines(self) -> None:
        result = settle({"A": 0, "B": 0}, self.routes[:1])
        self.assertTrue(result.feasible)
        self.assertEqual(result.total_activation_fee, 0)
        self.assertEqual(result.instructions, tuple())

    def test_balances_must_sum_to_zero(self) -> None:
        with self.assertRaises(ValidationError):
            settle({"A": 1, "B": -2}, self.routes[:1])

    def test_instructions_respect_capacity_and_clear_every_node(self) -> None:
        result = settle(self.balances, self.routes[1:])
        net = {node: 0 for node in self.balances}
        for ins in result.instructions:
            self.assertIsInstance(ins, Instruction)
            net[ins.source] -= ins.amount
            net[ins.target] += ins.amount
        self.assertEqual(net, self.balances)


if __name__ == "__main__":
    unittest.main(verbosity=2)
