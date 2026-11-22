"""Cash receipt and change-cart tests.

Covers the brief end to end:

* round5 boundaries and the cumulative cash rule (actual cash is the
  difference of two rounded exact cumulatives, never a rounded difference);
* the two real two-unit quotes (combined discount vs. A-only discount):
  first IOU 5 vs. 0, whole-order cash 10 in both, car dispatch needed vs. not;
* zero-difference receipts still consuming return eligibility, one IOU per
  counter/slot, transfer sales untouched, retried/raced returns producing a
  single receipt and IOU and unrelated re-entries minting nothing;
* minimum-freight whole-bag transport, coin selection that preserves small
  change, idempotent plan application and manual payout without double coins;
* independent cash audit / day close and full reopen from SQLite.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from basket_service import open_service
from basket_service.cash import coin_combine, round5
from basket_service.errors import BasketError

PRODUCTS_6 = [{"sku": "A", "price": 6}, {"sku": "B", "price": 6}]
BASKET_AB = [
    {"unit_id": "a", "sku": "A", "price": 6},
    {"unit_id": "b", "sku": "B", "price": 6},
]
COMBO_COUPON = [{"coupon_id": "AB", "D": 4, "requires": {"A": 1, "B": 1}}]
SOLO_COUPON = [{"coupon_id": "AA", "D": 4, "requires": {"A": 1}}]


def quote_and_sell(svc, tag, coupon, *, payment="CASH", counter="C1"):
    snap = svc.publish_snapshot(PRODUCTS_6, coupon, snapshot_id=f"snap-{tag}")
    quote = svc.quote(BASKET_AB, [], f"q-{tag}", snap)
    if payment == "CASH":
        svc.register_sale(f"S-{tag}", quote=quote, payment_method="CASH", counter_id=counter)
    else:
        svc.register_sale(f"S-{tag}", quote=quote)
    return quote


class Round5Tests(unittest.TestCase):
    def test_boundaries(self):
        table = {0: 0, 1: 0, 2: 0, 3: 5, 4: 5, 5: 5, 6: 5, 7: 5, 8: 10, 9: 10,
                 10: 10, 11: 10, 12: 10, 13: 15, 14: 15, 17: 15, 18: 20}
        for cents, expected in table.items():
            self.assertEqual(round5(cents), expected, cents)

    def test_rejects_bad_inputs(self):
        for bad in (-1, 1.5, True, "5", None):
            with self.assertRaises(BasketError):
                round5(bad)

    def test_coin_combine_keeps_small_change_and_backtracks(self):
        self.assertEqual(coin_combine(10, {5: 2, 10: 1}), {10: 1})
        self.assertEqual(coin_combine(5, {5: 2}), {5: 1})
        self.assertIsNone(coin_combine(10, {25: 2}))
        # backtracking: greedy big coins cannot make 30 exactly but 25+5 can
        self.assertEqual(coin_combine(30, {25: 1, 10: 2, 5: 1}), {25: 1, 5: 1})
        # only the two fives can make 10 when no 10 coin exists
        self.assertEqual(coin_combine(10, {5: 2, 25: 1}), {5: 2})


class CashReceiptRuleTests(unittest.TestCase):
    def setUp(self):
        self.svc = open_service(":memory:")
        self.svc.register_counter("C1")

    def test_combined_discount_first_iou_is_five_total_ten(self):
        quote = quote_and_sell(self.svc, "combo", COMBO_COUPON)
        self.assertEqual([u["paid"] for u in quote["allocation"]], [4, 4])
        first = self.svc.return_units("r-a", "S-combo", ["a"], {"a": "RESTOCK"}, slot=0)
        cash = first["cash"]
        self.assertEqual(cash["exact_refund"], 4)
        self.assertEqual(cash["exact_cumulative"], 4)
        self.assertEqual((cash["cash_before"], cash["cash_cumulative"], cash["cash_due"]), (0, 5, 5))
        self.assertEqual(cash["iou_id"], "IOU-r-a")
        second = self.svc.return_units("r-b", "S-combo", ["b"], {"b": "RESTOCK"}, slot=2)
        self.assertEqual(
            (second["cash"]["cash_before"], second["cash"]["cash_cumulative"], second["cash"]["cash_due"]),
            (5, 10, 5),
        )
        self.assertEqual(first["cash"]["cash_due"] + second["cash"]["cash_due"], 10)
        # exact ledger stays in integer cents
        self.assertEqual(first["refund_total"], 4)
        self.assertEqual(second["cumulative_refund"], 8)

    def test_solo_discount_first_iou_is_zero_total_ten(self):
        quote = quote_and_sell(self.svc, "solo", SOLO_COUPON)
        self.assertEqual([u["paid"] for u in quote["allocation"]], [2, 6])
        first = self.svc.return_units("r-a", "S-solo", ["a"], {"a": "RESTOCK"}, slot=0)
        self.assertEqual(first["cash"]["cash_due"], 0)
        self.assertIsNone(first["cash"]["iou_id"])
        self.assertEqual(self.svc.list_ious(), [])
        second = self.svc.return_units("r-b", "S-solo", ["b"], {"b": "RESTOCK"}, slot=2)
        self.assertEqual((second["cash"]["cash_before"], second["cash"]["cash_cumulative"],
                          second["cash"]["cash_due"]), (0, 10, 10))
        self.assertEqual(first["cash"]["cash_due"] + second["cash"]["cash_due"], 10)

    def test_cumulative_uses_difference_of_rounds_not_round_of_difference(self):
        # One cash sale whose two units carry exact paid amounts 3 and 4.
        # Exact cumulative: 3 then 7; round5(3)=5, round5(7)=5 -> the second
        # receipt's actual cash is 5-5 = 0, even though round5(4) alone is 5.
        basket = [
            {"unit_id": "a", "sku": "A", "price": 3},
            {"unit_id": "b", "sku": "B", "price": 4},
        ]
        snap = self.svc.publish_snapshot(
            [{"sku": "A", "price": 3}, {"sku": "B", "price": 4}], [], snapshot_id="s-cum"
        )
        quote = self.svc.quote(basket, [], "q-cum", snap)
        self.svc.register_sale("Scum", quote=quote, payment_method="CASH", counter_id="C1")
        r1 = self.svc.return_units("r1", "Scum", ["a"], {"a": "RESTOCK"}, slot=0)
        r2 = self.svc.return_units("r2", "Scum", ["b"], {"b": "RESTOCK"}, slot=1)
        self.assertEqual(r1["cash"]["cash_due"], 5)
        self.assertEqual(r2["cash"]["cash_due"], 0)
        self.assertEqual(r2["cash"]["cash_before"], 5)
        self.assertEqual(r2["cash"]["cash_cumulative"], 5)

    def test_zero_due_receipt_consumes_return_eligibility(self):
        quote_and_sell(self.svc, "solo", SOLO_COUPON)
        self.svc.return_units("r-a", "S-solo", ["a"], {"a": "RESTOCK"}, slot=0)
        with self.assertRaises(BasketError) as ctx:
            self.svc.return_units("r-a2", "S-solo", ["a"], {"a": "RESTOCK"}, slot=1)
        self.assertEqual(ctx.exception.code, "UNIT_ALREADY_RETURNED")
        # the zero-due receipt is still queryable
        receipt = self.svc.get_receipt("r-a")
        self.assertEqual(receipt["cash_due"], 0)
        self.assertEqual([i["refund"] for i in receipt["items"]], [2])

    def test_after_sales_query_shows_exact_items_cash_delta_and_cumulative(self):
        quote_and_sell(self.svc, "combo", COMBO_COUPON)
        result = self.svc.return_units("r-a", "S-combo", ["a"], {"a": "RESTOCK"}, slot=0)
        block = result["cash"]
        self.assertEqual(block["items"][0]["original_paid"], 4)
        self.assertEqual(block["items"][0]["refund"], 4)
        self.assertEqual(block["cash_due"], 5)
        self.assertEqual(block["cash_cumulative"], 5)
        fetched = self.svc.get_return("r-a")
        self.assertEqual(fetched["cash"], block)
        self.assertTrue(fetched["repeated"])

    def test_transfer_sale_keeps_original_contract(self):
        quote = quote_and_sell(self.svc, "t", COMBO_COUPON, payment="TRANSFER")
        result = self.svc.return_units("r-t", "S-t", ["a"], {"a": "RESTOCK"})
        self.assertEqual(result["refund_total"], 4)
        self.assertNotIn("cash", result)
        self.assertEqual(self.svc.list_receipts(), [])
        with self.assertRaises(BasketError) as ctx:
            self.svc.return_units("r-t2", "S-t", ["b"], {"b": "RESTOCK"}, slot=0)
        self.assertEqual(ctx.exception.code, "INVALID_INPUT")

    def test_cash_sale_requires_known_counter_and_slot(self):
        basket = [{"unit_id": "a", "sku": "A", "price": 6}]
        snap = self.svc.publish_snapshot([{"sku": "A", "price": 6}], [], snapshot_id="sx")
        q = self.svc.quote(basket, [], "qx", snap)
        with self.assertRaises(BasketError) as ctx:
            self.svc.register_sale("Sx", quote=q, payment_method="CASH", counter_id="NOBODY")
        self.assertEqual(ctx.exception.code, "UNKNOWN_COUNTER")
        self.svc.register_sale("Sx", quote=q, payment_method="CASH", counter_id="C1")
        with self.assertRaises(BasketError) as ctx:
            self.svc.return_units("rx", "Sx", ["a"], {"a": "RESTOCK"})
        self.assertEqual(ctx.exception.code, "INVALID_INPUT")

    def test_retry_replays_one_receipt_and_one_iou(self):
        quote_and_sell(self.svc, "combo", COMBO_COUPON)
        first = self.svc.return_units("r-a", "S-combo", ["a"], {"a": "RESTOCK"}, slot=0)
        replay = self.svc.return_units("r-a", "S-combo", ["a"], {"a": "RESTOCK"}, slot=0)
        self.assertEqual(replay, first)
        self.assertEqual(len(self.svc.list_receipts("S-combo")), 1)
        self.assertEqual(len(self.svc.list_ious()), 1)
        with self.assertRaises(BasketError) as ctx:
            self.svc.return_units("r-a", "S-combo", ["b"], {"b": "RESTOCK"}, slot=1)
        self.assertEqual(ctx.exception.code, "REQUEST_CONFLICT")

    def test_unrelated_re_entries_cannot_mint_iou(self):
        quote_and_sell(self.svc, "combo", COMBO_COUPON)
        with self.assertRaises(BasketError) as ctx:
            self.svc.return_units("bad-unit", "S-combo", ["zzz"], {"zzz": "RESTOCK"}, slot=0)
        self.assertEqual(ctx.exception.code, "UNIT_INELIGIBLE")
        with self.assertRaises(BasketError) as ctx:
            self.svc.return_units("bad-sale", "GHOST", ["a"], {"a": "RESTOCK"}, slot=0)
        self.assertEqual(ctx.exception.code, "UNKNOWN_SALE")
        self.assertEqual(self.svc.list_ious(), [])
        self.assertEqual(self.svc.list_receipts(), [])

    def test_one_positive_iou_per_counter_slot(self):
        for sid, price in (("S1", 5), ("S2", 5)):
            basket = [{"unit_id": "a", "sku": "A", "price": price}]
            snap = self.svc.publish_snapshot([{"sku": "A", "price": price}], [], snapshot_id=f"s-{sid}")
            q = self.svc.quote(basket, [], f"q-{sid}", snap)
            self.svc.register_sale(sid, quote=q, payment_method="CASH", counter_id="C1")
        self.svc.return_units("r1", "S1", ["a"], {"a": "RESTOCK"}, slot=3)
        with self.assertRaises(BasketError) as ctx:
            self.svc.return_units("r2", "S2", ["a"], {"a": "RESTOCK"}, slot=3)
        self.assertEqual(ctx.exception.code, "IOU_SLOT_TAKEN")
        # zero-due receipt in the occupied slot is harmless (no IOU)
        basket = [{"unit_id": "a", "sku": "A", "price": 2}]
        snap = self.svc.publish_snapshot([{"sku": "A", "price": 2}], [], snapshot_id="s-zero")
        q = self.svc.quote(basket, [], "q-zero", snap)
        self.svc.register_sale("Szero", quote=q, payment_method="CASH", counter_id="C1")
        zero = self.svc.return_units("rz", "Szero", ["a"], {"a": "RESTOCK"}, slot=3)
        self.assertEqual(zero["cash"]["cash_due"], 0)


class CarDispatchTests(unittest.TestCase):
    def _combo_sale_with_first_iou(self, svc, sale_id="S-combo"):
        quote_and_sell(svc, "combo", COMBO_COUPON)
        return svc.return_units("r-a", sale_id, ["a"], {"a": "RESTOCK"}, slot=0)

    def test_first_iou_five_forces_dispatch_when_counter_empty(self):
        svc = open_service(":memory:")
        svc.register_counter("C1")
        self._combo_sale_with_first_iou(svc)
        svc.register_bag("BAG1", {5: 1}, ["C1"], 0, 2)
        plan = svc.plan_day()
        self.assertTrue(plan["feasible"])
        self.assertEqual(plan["total_freight"], 2)
        self.assertEqual([d["bag_id"] for d in plan["deliveries"]], ["BAG1"])
        result = svc.apply_plan(plan)
        self.assertFalse(result["repeated"])
        self.assertEqual(svc.list_ious("PAID")[0]["iou_id"], "IOU-r-a")
        # replay never re-issues coins
        self.assertTrue(svc.apply_plan(plan)["repeated"])

    def test_zero_first_iou_needs_no_car(self):
        svc = open_service(":memory:")
        svc.register_counter("C1")
        quote_and_sell(svc, "solo", SOLO_COUPON)
        svc.return_units("r-a", "S-solo", ["a"], {"a": "RESTOCK"}, slot=0)
        svc.register_bag("BAG1", {5: 1}, ["C1"], 0, 2)
        plan = svc.plan_day()
        self.assertTrue(plan["feasible"])
        self.assertEqual(plan["total_freight"], 0)
        self.assertEqual(plan["deliveries"], [])

    def test_holding_the_coin_already_avoids_dispatch(self):
        svc = open_service(":memory:")
        svc.register_counter("C1")
        svc.opening_balance("open", "C1", 5, 1)
        self._combo_sale_with_first_iou(svc)
        svc.register_bag("BAG1", {5: 1}, ["C1"], 0, 2)
        plan = svc.plan_day()
        self.assertEqual(plan["total_freight"], 0)
        self.assertEqual(plan["deliveries"], [])
        svc.apply_plan(plan)
        self.assertEqual(svc.list_ious("PAID")[0]["iou_id"], "IOU-r-a")

    def test_whole_bag_cannot_split_and_minimum_freight_wins(self):
        svc = open_service(":memory:")
        svc.register_counter("K1")
        svc.register_counter("K2")
        for sid, counter in (("S1", "K1"), ("S2", "K2")):
            basket = [{"unit_id": "a", "sku": "A", "price": 5}]
            snap = svc.publish_snapshot([{"sku": "A", "price": 5}], [], snapshot_id=f"s-{sid}")
            q = svc.quote(basket, [], f"q-{sid}", snap)
            svc.register_sale(sid, quote=q, payment_method="CASH", counter_id=counter)
        svc.return_units("r1", "S1", ["a"], {"a": "RESTOCK"}, slot=0)
        svc.return_units("r2", "S2", ["a"], {"a": "RESTOCK"}, slot=0)
        svc.register_bag("SHARED", {5: 1}, ["K1", "K2"], 0, 9)
        svc.register_bag("B-K1", {5: 1}, ["K1"], 0, 1)
        svc.register_bag("B-K2", {5: 1}, ["K2"], 0, 2)
        plan = svc.plan_day()
        self.assertEqual(plan["total_freight"], 3)
        routed = {d["counter_id"]: d["bag_id"] for d in plan["deliveries"]}
        self.assertEqual(routed, {"K1": "B-K1", "K2": "B-K2"})
        svc.apply_plan(plan)
        # the shared, never-delivered bag remains sealed and available
        self.assertEqual([b["bag_id"] for b in svc.list_bags()], ["B-K1", "B-K2", "SHARED"])

    def test_bag_arriving_after_slot_cannot_cover_iou(self):
        svc = open_service(":memory:")
        svc.register_counter("C1")
        basket = [{"unit_id": "a", "sku": "A", "price": 5}]
        snap = svc.publish_snapshot([{"sku": "A", "price": 5}], [], snapshot_id="s")
        q = svc.quote(basket, [], "q", snap)
        svc.register_sale("S", quote=q, payment_method="CASH", counter_id="C1")
        svc.return_units("r", "S", ["a"], {"a": "RESTOCK"}, slot=0)
        svc.register_bag("LATE", {5: 1}, ["C1"], 1, 1)
        plan = svc.plan_day()
        self.assertFalse(plan["feasible"])
        self.assertIn("IOU-r", plan["reason"])

    def test_ten_then_five_change_squeeze_reserved_by_exact_search(self):
        svc = open_service(":memory:")
        svc.register_counter("C1")
        svc.opening_balance("open", "C1", 5, 2)
        for sid, price in (("S10", 10), ("S5", 5)):
            basket = [{"unit_id": "a", "sku": "A", "price": price}]
            snap = svc.publish_snapshot([{"sku": "A", "price": price}], [], snapshot_id=f"s-{sid}")
            q = svc.quote(basket, [], f"q-{sid}", snap)
            svc.register_sale(sid, quote=q, payment_method="CASH", counter_id="C1")
        svc.return_units("r10", "S10", ["a"], {"a": "RESTOCK"}, slot=0)
        svc.return_units("r5", "S5", ["a"], {"a": "RESTOCK"}, slot=1)
        # A 10-coin bag arriving only at slot 1 cannot rescue the slot-0 IOU:
        # spending both opening fives on the 10 would starve the later five.
        svc.register_bag("LATE10", {10: 1}, ["C1"], 1, 1)
        self.assertFalse(svc.plan_day()["feasible"])
        # The same bag arriving at slot 0 lets the exact search keep a five
        # for the later IOU and pay the 10 with the delivered 10 coin.
        svc.register_bag("EARLY10", {10: 1}, ["C1"], 0, 3)
        plan = svc.plan_day()
        self.assertTrue(plan["feasible"])
        self.assertEqual(plan["total_freight"], 3)
        paid = {p["iou_id"]: p["coins"] for p in plan["payments"]}
        self.assertEqual(paid["IOU-r10"], {10: 1})
        self.assertEqual(paid["IOU-r5"], {5: 1})
        svc.apply_plan(plan)
        self.assertEqual(svc.counter_balance(), {"C1": {5: 1}})


class PayoutAndAuditTests(unittest.TestCase):
    def _one_iou_office(self, path=":memory:"):
        svc = open_service(path)
        svc.register_counter("C1")
        svc.opening_balance("open", "C1", 5, 2)
        basket = [{"unit_id": "a", "sku": "A", "price": 5}]
        snap = svc.publish_snapshot([{"sku": "A", "price": 5}], [], snapshot_id="s")
        q = svc.quote(basket, [], "q", snap)
        svc.register_sale("S", quote=q, payment_method="CASH", counter_id="C1")
        svc.return_units("r", "S", ["a"], {"a": "RESTOCK"}, slot=0)
        return svc

    def test_manual_pay_exact_tender_and_retry_no_double_coins(self):
        svc = self._one_iou_office()
        result = svc.pay_iou("IOU-r", "PAY-1")
        self.assertEqual(result["coins"], {5: 1})
        self.assertEqual(svc.counter_balance("C1"), {"C1": {5: 1}})
        # retried payout returns the same disbursement and spends nothing more
        retry = svc.pay_iou("IOU-r", "PAY-1")
        self.assertTrue(retry["repeated"])
        self.assertEqual(svc.counter_balance("C1"), {"C1": {5: 1}})
        # a settled IOU rejects a new disbursement id instead of issuing coins
        with self.assertRaises(BasketError) as ctx:
            svc.pay_iou("IOU-r", "PAY-2")
        self.assertEqual(ctx.exception.code, "IOU_ALREADY_PAID")

    def test_manual_tender_must_be_exact_and_available(self):
        svc = self._one_iou_office()
        with self.assertRaises(BasketError) as ctx:
            svc.pay_iou("IOU-r", "PAY-BAD", {25: 1})  # coin not held by the drawer
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_COINS")
        with self.assertRaises(BasketError) as ctx:
            svc.pay_iou("IOU-r", "PAY-BAD2", {5: 3})
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_COINS")
        with self.assertRaises(BasketError) as ctx:
            svc.pay_iou("IOU-r", "PAY-BAD3", {10: 1})  # coin not in the drawer
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_COINS")
        # stock the drawer with a 25: tendering it is possible inventory-wise
        # but wrong for a 5-cent IOU because customers take no change
        svc.opening_balance("open25", "C1", 25, 1)
        with self.assertRaises(BasketError) as ctx:
            svc.pay_iou("IOU-r", "PAY-BAD4", {25: 1})
        self.assertEqual(ctx.exception.code, "INCORRECT_TENDER")
        # nothing moved on failed tenders
        self.assertEqual(svc.counter_balance("C1"), {"C1": {5: 2, 25: 1}})

    def test_day_close_recomputes_each_receipt_to_the_cent(self):
        svc = open_service(":memory:")
        svc.register_counter("C1")
        quote_and_sell(svc, "combo", COMBO_COUPON)
        svc.return_units("r-a", "S-combo", ["a"], {"a": "RESTOCK"}, slot=0)
        svc.return_units("r-b", "S-combo", ["b"], {"b": "RESTOCK"}, slot=2)
        day = svc.close_day()
        self.assertTrue(day["ok"])
        self.assertEqual(day["totals"], {
            "exact_refund_total": 8, "cash_due_total": 10,
            "receipt_count": 2, "iou_count": 2, "zero_due_count": 0,
        })
        for line in day["receipts"]:
            self.assertEqual(line["exact_refund"], line["exact_refund_recomputed"])
            self.assertEqual(line["cash_cumulative"], round5(line["exact_cumulative"]))
        audit = svc.audit_cash()
        self.assertTrue(audit["ok"])
        self.assertTrue(audit["slot_uniqueness_ok"])
        self.assertTrue(audit["positive_due_has_iou"])

    def test_reopen_preserves_cash_books(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "cash.db")
            svc = self._one_iou_office(path)
            plan = svc.plan_day()
            svc.save_plan("plan-1", plan)
            svc.apply_plan(plan_id="plan-1")
            del svc
            reopened = open_service(path)
            self.assertEqual(reopened.get_receipt("r")["cash_due"], 5)
            self.assertEqual(reopened.get_iou("IOU-r")["status"], "PAID")
            self.assertEqual(reopened.get_plan("plan-1")["total_freight"], 0)
            self.assertTrue(reopened.close_day()["ok"])
            # two fives were opened, one was disbursed, one remains
            self.assertEqual(reopened.audit_cash()["balances"]["stored"], {"C1": {5: 1}})
            self.assertEqual(reopened.audit_cash()["balances"]["recomputed_from_events"], {"C1": {5: 1}})


class ConcurrentCashReturnTests(unittest.TestCase):
    def test_two_tellers_same_unit_one_receipt_one_iou(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "race.db")
            svc = open_service(path)
            svc.register_counter("C1")
            quote_and_sell(svc, "combo", COMBO_COUPON)
            del svc
            barrier = threading.Barrier(2)
            successes, failures = [], []

            def worker(rid):
                client = open_service(path)
                try:
                    barrier.wait()
                    client.return_units(rid, "S-combo", ["a"], {"a": "RESTOCK"}, slot=0)
                    successes.append(rid)
                except BasketError as exc:
                    failures.append(exc.code)

            threads = [threading.Thread(target=worker, args=(f"rr-{i}",)) for i in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(successes), 1)
            self.assertEqual(failures, ["UNIT_ALREADY_RETURNED"])
            final = open_service(path)
            self.assertEqual(len(final.list_receipts("S-combo")), 1)
            self.assertEqual(len(final.list_ious()), 1)
            self.assertEqual(final.inventory()["RESTOCK"].get("A", 0), 1)


class ConcurrentPayoutTests(unittest.TestCase):
    def _pending_iou_db(self, tmp_dir):
        path = str(Path(tmp_dir) / "payout.db")
        svc = open_service(path)
        svc.register_counter("C1")
        svc.opening_balance("open", "C1", 5, 3)
        basket = [{"unit_id": "a", "sku": "A", "price": 5}]
        snap = svc.publish_snapshot([{"sku": "A", "price": 5}], [], snapshot_id="s")
        q = svc.quote(basket, [], "q", snap)
        svc.register_sale("S", quote=q, payment_method="CASH", counter_id="C1")
        svc.return_units("r", "S", ["a"], {"a": "RESTOCK"}, slot=0)
        return path

    def test_same_disbursement_id_concurrent_pays_one_coin(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._pending_iou_db(tmp)
            barrier = threading.Barrier(2)
            fresh, replays, failures = [], [], []

            def worker():
                client = open_service(path)
                try:
                    barrier.wait()
                    result = client.pay_iou("IOU-r", "PAY-1")
                    (replays if result["repeated"] else fresh).append(result["disbursement_id"])
                except BasketError as exc:
                    failures.append(exc.code)

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(fresh), 1)
            self.assertEqual(len(replays), 1)
            self.assertEqual(failures, [])
            final = open_service(path)
            self.assertEqual(len(final.audit_cash()["disbursements"]), 1)
            self.assertEqual(final.counter_balance(), {"C1": {5: 2}})

    def test_different_disbursement_ids_race_one_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._pending_iou_db(tmp)
            barrier = threading.Barrier(2)
            successes, failures = [], []

            def worker(disbursement_id):
                client = open_service(path)
                try:
                    barrier.wait()
                    client.pay_iou("IOU-r", disbursement_id)
                    successes.append(disbursement_id)
                except BasketError as exc:
                    failures.append(exc.code)

            threads = [
                threading.Thread(target=worker, args=("PAY-A",)),
                threading.Thread(target=worker, args=("PAY-B",)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(successes), 1)
            self.assertEqual(failures, ["IOU_ALREADY_PAID"])
            final = open_service(path)
            self.assertEqual(len(final.audit_cash()["disbursements"]), 1)
            self.assertTrue(final.audit_cash()["ok"])


class ExtremeDayAndIntegrityTests(unittest.TestCase):
    def test_many_ious_with_three_bags_exact_routing(self):
        svc = open_service(":memory:")
        svc.register_counter("K1")
        for i in range(12):
            basket = [{"unit_id": "a", "sku": "A", "price": 5}]
            snap = svc.publish_snapshot([{"sku": "A", "price": 5}], [], snapshot_id=f"s{i}")
            q = svc.quote(basket, [], f"q{i}", snap)
            svc.register_sale(f"S{i}", quote=q, payment_method="CASH", counter_id="K1")
            svc.return_units(f"r{i}", f"S{i}", ["a"], {"a": "RESTOCK"}, slot=i)
        svc.register_bag("B1", {5: 4}, ["K1"], 0, 1)
        svc.register_bag("B2", {5: 4}, ["K1"], 4, 1)
        svc.register_bag("B3", {5: 4}, ["K1"], 8, 1)
        plan = svc.plan_day()
        self.assertTrue(plan["feasible"], plan["reason"])
        self.assertEqual(plan["total_freight"], 3)
        self.assertEqual(sorted(d["bag_id"] for d in plan["deliveries"]), ["B1", "B2", "B3"])
        self.assertEqual(len(plan["payments"]), 12)
        svc.apply_plan(plan)
        self.assertEqual(svc.list_ious("PENDING"), [])
        self.assertTrue(svc.close_day()["ok"])

    def test_many_bags_scattered_five_coin_need_picks_cheapest(self):
        svc = open_service(":memory:")
        for counter in ("K1", "K2", "K3"):
            svc.register_counter(counter)
            basket = [{"unit_id": "a", "sku": "A", "price": 5}]
            snap = svc.publish_snapshot([{"sku": "A", "price": 5}], [], snapshot_id=f"s-{counter}")
            q = svc.quote(basket, [], f"q-{counter}", snap)
            svc.register_sale(f"S-{counter}", quote=q, payment_method="CASH", counter_id=counter)
            svc.return_units(f"r-{counter}", f"S-{counter}", ["a"], {"a": "RESTOCK"}, slot=0)
        for i in range(6):
            svc.register_bag(f"BIG{i}", {25: 2, 10: 2}, ["K1", "K2", "K3"], 0, 8)
        for counter in ("K1", "K2", "K3"):
            svc.register_bag(f"FIVE-{counter}", {5: 1}, [counter], 0, 1)
        plan = svc.plan_day()
        self.assertTrue(plan["feasible"])
        self.assertEqual(plan["total_freight"], 3)
        self.assertEqual(
            sorted(d["bag_id"] for d in plan["deliveries"]),
            ["FIVE-K1", "FIVE-K2", "FIVE-K3"],
        )

    def test_mixed_denominations_largest_first_exact_tender(self):
        svc = open_service(":memory:")
        svc.register_counter("C1")
        svc.opening_balance("o25", "C1", 25, 2)
        svc.opening_balance("o10", "C1", 10, 1)
        basket = [{"unit_id": "a", "sku": "A", "price": 60}]
        snap = svc.publish_snapshot([{"sku": "A", "price": 60}], [], snapshot_id="s")
        q = svc.quote(basket, [], "q", snap)
        svc.register_sale("S", quote=q, payment_method="CASH", counter_id="C1")
        result = svc.return_units("r", "S", ["a"], {"a": "RESTOCK"}, slot=0)
        self.assertEqual(result["cash"]["cash_due"], 60)
        plan = svc.plan_day()
        self.assertEqual(plan["payments"][0]["coins"], {25: 2, 10: 1})
        svc.apply_plan(plan)
        self.assertEqual(svc.counter_balance(), {})

    def test_plan_drift_is_rejected_not_double_paid(self):
        svc = open_service(":memory:")
        svc.register_counter("C1")
        for sid, amount in (("S1", 5), ("S2", 5)):
            basket = [{"unit_id": "a", "sku": "A", "price": amount}]
            snap = svc.publish_snapshot([{"sku": "A", "price": amount}], [], snapshot_id=f"s-{sid}")
            q = svc.quote(basket, [], f"q-{sid}", snap)
            svc.register_sale(sid, quote=q, payment_method="CASH", counter_id="C1")
        svc.return_units("r1", "S1", ["a"], {"a": "RESTOCK"}, slot=0)
        svc.return_units("r2", "S2", ["a"], {"a": "RESTOCK"}, slot=1)
        svc.opening_balance("coins", "C1", 5, 2)
        svc.save_plan("P", svc.plan_day())
        svc.pay_iou("IOU-r1", "MANUAL-1")
        with self.assertRaises(BasketError) as ctx:
            svc.apply_plan(plan_id="P")
        self.assertEqual(ctx.exception.code, "IOU_ALREADY_PAID")
        self.assertEqual([i["iou_id"] for i in svc.list_ious("PENDING")], ["IOU-r2"])

    def test_node_budget_guard_never_returns_unverified_plan(self):
        svc = open_service(":memory:")
        svc.register_counter("C1")
        for i in range(4):
            basket = [{"unit_id": "a", "sku": "A", "price": 5}]
            snap = svc.publish_snapshot([{"sku": "A", "price": 5}], [], snapshot_id=f"s{i}")
            q = svc.quote(basket, [], f"q{i}", snap)
            svc.register_sale(f"S{i}", quote=q, payment_method="CASH", counter_id="C1")
            svc.return_units(f"r{i}", f"S{i}", ["a"], {"a": "RESTOCK"}, slot=i)
        for i in range(6):
            svc.register_bag(f"B{i}", {5: 1}, ["C1"], 0, 1)
        with self.assertRaises(BasketError) as ctx:
            svc.plan_day(node_budget=2)
        self.assertEqual(ctx.exception.code, "PLAN_TOO_LARGE")


class PlanRunnerTests(unittest.TestCase):
    def test_json_plan_drives_cash_office(self):
        svc = open_service(":memory:")
        plan = {
            "steps": [
                {"op": "register_counter", "counter_id": "C1"},
                {"op": "register_bag", "bag_id": "B1", "coins": {5: 1},
                 "delivers_to": ["C1"], "arrival_slot": 0, "freight": 2},
                {"op": "publish_snapshot", "products": PRODUCTS_6, "coupons": COMBO_COUPON, "snapshot_id": "s1"},
                {"name": "q1", "op": "quote", "basket": BASKET_AB, "coupons": [],
                 "request_id": "q1", "snapshot_id": "s1"},
                {"op": "register_sale", "sale_id": "S1", "quote": "$q1",
                 "payment_method": "CASH", "counter_id": "C1"},
                {"op": "return", "request_id": "r1", "sale_id": "S1",
                 "unit_ids": ["a"], "grade": {"a": "RESTOCK"}, "slot": 0},
                {"name": "dayplan", "op": "plan_day"},
                {"op": "save_plan", "plan_id": "P1", "plan": "$dayplan"},
                {"op": "apply_plan", "plan_id": "P1"},
                {"op": "close_day"},
            ]
        }
        result = svc.run(plan)
        self.assertTrue(result["ok"])
        self.assertTrue(all(s["ok"] for s in result["steps"]))
        self.assertEqual(svc.list_ious("PAID")[0]["iou_id"], "IOU-r1")


if __name__ == "__main__":
    unittest.main()
