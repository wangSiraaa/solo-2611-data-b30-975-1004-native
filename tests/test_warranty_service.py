"""Warranty service tests: terms, per-unit enrollment, outages, versions, returns."""

from __future__ import annotations

import unittest

from basket_service import open_service
from basket_service.errors import BasketError


def two_unit_service():
    """Two same-price R units; optimizer gives the single EXT coupon to uA."""

    svc = open_service(":memory:")
    basket = [
        {"unit_id": "uA", "sku": "R", "price": 1000},
        {"unit_id": "uB", "sku": "R", "price": 1000},
    ]
    coupons = [{"coupon_id": "EXT", "D": 100, "requires": {"R": 1}}]
    snap = svc.publish_snapshot([{"sku": "R", "price": 1000, "quantity": 2}], coupons, snapshot_id="s1")
    quote = svc.quote(basket, [], "q1", snap)
    svc.register_sale("S1", quote=quote)
    svc.publish_warranty_term("t-base", "R", 3)
    svc.publish_warranty_term("t-ext", "R", 3, coupon_id="EXT", bonus_days=2)
    return svc, quote


class EnrollmentTests(unittest.TestCase):
    def test_budget_follows_actual_per_unit_coupon_allocation(self):
        svc, quote = two_unit_service()
        owner = {a["unit_id"]: a["coupon_id"] for a in quote["allocation"]}
        self.assertEqual(owner["uA"], "EXT")
        self.assertIsNone(owner["uB"])
        rows = svc.enroll_warranties("S1", 0)
        budget = {r["unit_id"]: r["warranty_days"] for r in rows}
        self.assertEqual(budget["uA"], 5)
        self.assertEqual(budget["uB"], 3)

    def test_addendum_goes_to_peer_unit_not_this_one(self):
        # A second equal-price sale where the coupon lands on n2: n1 gets base only.
        svc, _ = two_unit_service()
        basket = [
            {"unit_id": "n1", "sku": "R", "price": 1000},
            {"unit_id": "n2", "sku": "R", "price": 1000},
        ]
        quote = svc.quote(basket, [], "q2", "s1")
        svc.register_sale("S2", quote=quote)
        rows = svc.enroll_warranties("S2", 0)
        budget = {r["unit_id"]: r["warranty_days"] for r in rows}
        coupon_owner = next(a["unit_id"] for a in quote["allocation"] if a["coupon_id"] == "EXT")
        base_only = "n1" if coupon_owner == "n2" else "n2"
        self.assertEqual(budget[base_only], 3)
        self.assertEqual(budget[coupon_owner], 5)

    def test_missing_coupon_addendum_is_an_error(self):
        svc = open_service(":memory:")
        basket = [{"unit_id": "uA", "sku": "R", "price": 1000}]
        coupons = [{"coupon_id": "EXT", "D": 100, "requires": {"R": 1}}]
        snap = svc.publish_snapshot([{"sku": "R", "price": 1000}], coupons, snapshot_id="s")
        quote = svc.quote(basket, [], "q", snap)
        svc.register_sale("S", quote=quote)
        svc.publish_warranty_term("t-base", "R", 3)  # no EXT addendum published
        with self.assertRaises(BasketError) as ctx:
            svc.enroll_warranties("S", 0)
        self.assertEqual(ctx.exception.code, "UNKNOWN_COUPON_TERM")

    def test_unknown_sale_and_term_conflicts(self):
        svc, _ = two_unit_service()
        with self.assertRaises(BasketError) as ctx:
            svc.enroll_warranties("NOPE", 0)
        self.assertEqual(ctx.exception.code, "UNKNOWN_SALE")
        with self.assertRaises(BasketError):
            svc.publish_warranty_term("t-base", "R", 9)  # term id reuse, different content

    def test_enrollment_is_idempotent(self):
        svc, _ = two_unit_service()
        first = svc.enroll_warranties("S1", 0)
        second = svc.enroll_warranties("S1", 0)
        self.assertEqual(first, second)


class OutageAndVersionTests(unittest.TestCase):
    def _enrolled(self):
        svc, _ = two_unit_service()
        svc.enroll_warranties("S1", 0)
        return svc

    def test_overlapping_outages_pause_once_and_match_brief(self):
        svc = self._enrolled()
        svc.register_outage("o1", "S1", "uA", 1, 3)
        svc.register_outage("o2", "S1", "uA", 2, 4)
        warranty = svc.get_warranty("S1", "uA")
        self.assertEqual(warranty["paused_days"], 3)
        self.assertEqual(warranty["expiry_day"], 8)
        self.assertEqual(warranty["latest_start_day"], 7)
        self.assertEqual(warranty["current_version"], 3)  # v1 at enrollment + two outages

    def test_late_back_dated_report_recomputes_but_keeps_old_answer(self):
        svc = self._enrolled()
        first = svc.query_warranty("Q1", "S1", "uA", 4)
        self.assertEqual(first["warranty_version"], 1)
        self.assertTrue(first["covered"])
        # Back-dated business interval, registered much later (import order != date).
        svc.register_outage("late", "S1", "uA", 1, 3, registered_day=20)
        self.assertEqual(svc.get_warranty("S1", "uA")["current_version"], 2)
        self.assertEqual(svc.get_warranty("S1", "uA")["expiry_day"], 7)
        stored = svc.get_warranty_query("Q1")
        self.assertEqual(stored["warranty_version"], 1)
        self.assertTrue(stored["covered"])
        history = svc.warranty_versions("S1", "uA")
        self.assertEqual([v["version"] for v in history], [1, 2])
        reconstructed_v1 = svc.warranty_version_summary("S1", "uA", 1)
        self.assertEqual(reconstructed_v1["expiry_day"], 5)  # budget 5, no outage at v1
        self.assertEqual(reconstructed_v1["paused_days"], 0)

    def test_outage_after_budget_exhausted_does_not_revive(self):
        svc = self._enrolled()
        # uB has a 3-day budget; with no outage it expires on day 3.
        svc.register_outage("dead", "S1", "uB", 5, 7, registered_day=7)
        warranty = svc.get_warranty("S1", "uB")
        self.assertEqual(warranty["expiry_day"], 3)
        self.assertEqual(warranty["paused_days"], 0)
        answer = svc.query_warranty("Q2", "S1", "uB", 6)
        self.assertFalse(answer["covered"])
        self.assertEqual(answer["ineffectual_outage_days"], 2)

    def test_duplicate_and_conflicting_outage_records(self):
        svc = self._enrolled()
        svc.register_outage("o1", "S1", "uA", 1, 3)
        # Same record id replays.
        svc.register_outage("o1", "S1", "uA", 1, 3)
        with self.assertRaises(BasketError) as ctx:
            svc.register_outage("o1", "S1", "uA", 2, 4)
        self.assertEqual(ctx.exception.code, "RECORD_CONFLICT")
        with self.assertRaises(BasketError) as ctx:
            svc.register_outage("o2", "S1", "uA", 1, 3)  # identical interval
        self.assertEqual(ctx.exception.code, "RECORD_CONFLICT")

    def test_invalid_interval_and_unknown_warranty(self):
        svc = self._enrolled()
        with self.assertRaises(BasketError) as ctx:
            svc.register_outage("bad", "S1", "uA", 4, 4)
        self.assertEqual(ctx.exception.code, "INVALID_INTERVAL")
        with self.assertRaises(BasketError) as ctx:
            svc.register_outage("x", "S1", "ghost", 1, 2)
        self.assertEqual(ctx.exception.code, "UNKNOWN_WARRANTY")


class ReturnExitTests(unittest.TestCase):
    def test_return_requires_ledger_return_and_ends_cover(self):
        svc, _ = two_unit_service()
        svc.enroll_warranties("S1", 0)
        with self.assertRaises(BasketError) as ctx:
            svc.mark_return_exit("S1", "uB", 2, "r-b")
        self.assertEqual(ctx.exception.code, "RETURN_CONFLICT")
        svc.return_units("r-b", "S1", ["uB"], {"uB": "RESTOCK"})
        with self.assertRaises(BasketError) as ctx:
            svc.mark_return_exit("S1", "uB", 2, "wrong-request")
        self.assertEqual(ctx.exception.code, "RETURN_CONFLICT")
        svc.mark_return_exit("S1", "uB", 2, "r-b")
        warranty = svc.get_warranty("S1", "uB")
        self.assertEqual(warranty["end_reason"], "RETURNED_EXIT")
        self.assertEqual(warranty["cover_until_day"], 2)
        self.assertEqual(warranty["latest_start_day"], 1)
        answer = svc.query_warranty("Q", "S1", "uB", 2)
        self.assertFalse(answer["covered"])
        self.assertEqual(answer["reason"], "RETURNED_EXIT")


class BusinessDateTests(unittest.TestCase):
    def test_business_day_not_import_order_for_old_sale(self):
        # Events are inserted out of order; the integer business date must win.
        svc, _ = two_unit_service()
        svc.enroll_warranties("S1", 30)  # old sale, deal on business day 30
        svc.register_outage("later-insert", "S1", "uA", 31, 33)  # inserted first
        warranty = svc.get_warranty("S1", "uA")
        self.assertEqual(warranty["sold_day"], 30)
        # Budget 5 (uA holds the EXT coupon addendum), paused on 31 and 32:
        # expiry = sold 30 + 5 cover days + 2 paused days = 37.
        self.assertEqual(warranty["expiry_day"], 37)
        self.assertEqual(warranty["paused_days"], 2)


if __name__ == "__main__":
    unittest.main()
