"""Tests for the pure warranty-clock engine."""

from __future__ import annotations

import unittest

from basket_service.warranty_engine import (
    EXPIRED_BUDGET,
    EXPIRED_RETURN,
    Outage,
    compute_warranty,
    eligibility,
    explain_outages,
    merge_intervals,
    overlap_days,
)


class IntervalTests(unittest.TestCase):
    def test_merge_overlapping_and_adjacent(self):
        self.assertEqual(merge_intervals([(1, 4), (2, 5)]), [(1, 5)])
        self.assertEqual(merge_intervals([(1, 3), (3, 5)]), [(1, 5)])
        self.assertEqual(merge_intervals([(1, 2), (4, 6)]), [(1, 2), (4, 6)])

    def test_overlap_with_open_ended_bounds(self):
        self.assertEqual(overlap_days((1, 4), (3, None)), 1)
        self.assertEqual(overlap_days((5, None), (0, 5)), 0)
        self.assertEqual(overlap_days((2, 8), (3, 6)), 3)


class WarrantyClockTests(unittest.TestCase):
    def test_brief_worked_example_unit_a(self):
        # Budget 5 (3 base + 2 coupon), overlapping outages union to 3 days.
        summary = compute_warranty(
            sold_day=0,
            warranty_days=5,
            outages=[Outage(1, 3, "r1"), Outage(2, 4, "r2")],
        )
        self.assertEqual(summary["paused_days"], 3)
        self.assertEqual(summary["expiry_day"], 8)
        self.assertEqual(summary["latest_start_day"], 7)
        self.assertTrue(eligibility(summary, 7)["covered"])
        self.assertFalse(eligibility(summary, 8)["covered"])
        self.assertEqual(eligibility(summary, 8)["reason"], EXPIRED_BUDGET)

    def test_brief_worked_example_unit_b_no_coupon(self):
        summary = compute_warranty(
            sold_day=0,
            warranty_days=3,
            outages=[Outage(1, 3, "r1"), Outage(2, 4, "r2")],
        )
        self.assertEqual(summary["paused_days"], 3)
        self.assertEqual(summary["expiry_day"], 6)
        self.assertEqual(summary["latest_start_day"], 5)
        self.assertFalse(eligibility(summary, 7)["covered"])

    def test_literal_half_open_intervals_union_to_four_days(self):
        # The brief's literal [1,4)+[2,5) intervals span four distinct days
        # under the declared half-open/union rule; documented in the demo.
        summary = compute_warranty(
            sold_day=0, warranty_days=5, outages=[Outage(1, 4), Outage(2, 5)]
        )
        self.assertEqual(summary["outage_intervals"], [(1, 5)])
        self.assertEqual(summary["paused_days"], 4)
        self.assertEqual(summary["expiry_day"], 9)

    def test_post_expiry_outage_cannot_revive(self):
        summary = compute_warranty(sold_day=0, warranty_days=3, outages=[Outage(5, 7, "late")])
        self.assertEqual(summary["expiry_day"], 3)
        self.assertEqual(summary["paused_days"], 0)
        self.assertEqual(summary["ineffectual_outage_days"], 2)

    def test_back_dated_outage_crossing_old_expiry_extends(self):
        without = compute_warranty(sold_day=0, warranty_days=3, outages=[])
        self.assertEqual(without["expiry_day"], 3)
        with_backdate = compute_warranty(sold_day=0, warranty_days=3, outages=[Outage(2, 4, "back")])
        self.assertEqual(with_backdate["paused_days"], 2)
        self.assertEqual(with_backdate["expiry_day"], 5)

    def test_outage_before_sale_is_ignored(self):
        summary = compute_warranty(sold_day=10, warranty_days=2, outages=[Outage(0, 10, "early")])
        self.assertEqual(summary["paused_days"], 0)
        self.assertEqual(summary["expiry_day"], 12)

    def test_return_ends_cover_early(self):
        summary = compute_warranty(sold_day=0, warranty_days=10, outages=[], return_day=4)
        self.assertEqual(summary["end_reason"], EXPIRED_RETURN)
        self.assertEqual(summary["cover_until_day"], 4)
        self.assertEqual(summary["latest_start_day"], 3)
        self.assertTrue(eligibility(summary, 3)["covered"])
        self.assertFalse(eligibility(summary, 4)["covered"])

    def test_return_after_natural_expiry_keeps_budget_reason(self):
        summary = compute_warranty(sold_day=0, warranty_days=2, outages=[], return_day=9)
        self.assertEqual(summary["end_reason"], EXPIRED_BUDGET)
        self.assertEqual(summary["cover_until_day"], 2)

    def test_zero_budget_is_expired_at_sale(self):
        summary = compute_warranty(sold_day=0, warranty_days=0, outages=[])
        self.assertEqual(summary["expiry_day"], 0)
        self.assertIsNone(summary["latest_start_day"])
        self.assertFalse(eligibility(summary, 0)["covered"])

    def test_explain_outages_splits_effective_and_dead_days(self):
        rows = explain_outages([Outage(2, 7, "x")], sold_day=0, expiry_day=4)
        self.assertEqual(rows[0]["effective_paused_days"], 2)
        self.assertEqual(rows[0]["post_expiry_days"], 3)

    def test_invalid_interval_rejected(self):
        with self.assertRaises(ValueError):
            Outage(3, 3)
        with self.assertRaises(ValueError):
            Outage(4, 2)


if __name__ == "__main__":
    unittest.main()
