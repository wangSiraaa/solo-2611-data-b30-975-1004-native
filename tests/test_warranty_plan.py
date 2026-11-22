"""End-to-end JSON plan coverage for the warranty and repair-shop ops."""

from __future__ import annotations

import unittest

from basket_service import open_service


class WarrantyPlanTests(unittest.TestCase):
    def test_full_warranty_and_repair_plan_replays(self):
        svc = open_service(":memory:")
        plan = {
            "steps": [
                {
                    "op": "publish_snapshot",
                    "products": [{"sku": "R", "price": 1000, "quantity": 1}],
                    "coupons": [],
                    "snapshot_id": "s",
                },
                {
                    "name": "q",
                    "op": "quote",
                    "basket": [{"unit_id": "u1", "sku": "R", "price": 1000}],
                    "coupons": [],
                    "request_id": "q",
                    "snapshot_id": "s",
                },
                {"op": "register_sale", "sale_id": "S", "quote": "$q"},
                {"op": "publish_warranty_term", "term_id": "tb", "sku": "R", "base_days": 3},
                {"op": "enroll_warranties", "sale_id": "S", "sold_day": 0},
                {"op": "register_outage", "record_id": "o1", "sale_id": "S", "unit_id": "u1", "start_day": 1, "end_day": 2},
                {"op": "query_warranty", "query_id": "Q1", "sale_id": "S", "unit_id": "u1", "query_day": 3},
                {"op": "warranty_versions", "sale_id": "S", "unit_id": "u1"},
                {"op": "register_repair_method", "method_id": "m", "sku": "R", "duration_days": 1, "specialty": "L"},
                {"op": "register_technician", "technician_id": "t", "specialty": "L"},
                {
                    "op": "open_work_order",
                    "work_order_id": "W",
                    "sale_id": "S",
                    "unit_id": "u1",
                    "delivery_day": 2,
                    "repair_value": 50,
                    "method_ids": ["m"],
                },
                {"op": "plan_repairs", "batch_id": "B", "work_order_ids": ["W"], "horizon_end_day": 10},
                {"op": "get_repair_batch", "batch_id": "B"},
                {"op": "technician_calendar", "technician_id": "t"},
            ]
        }
        result = svc.run(plan)
        self.assertTrue(all(step["ok"] for step in result["steps"]))
        query = next(s["result"] for s in result["steps"] if s["op"] == "query_warranty")
        # Budget 3, one paused day (day 1) -> expiry day 4; day 3 still covered.
        self.assertTrue(query["covered"])
        self.assertEqual(query["paused_days"], 1)
        batch = next(s["result"] for s in result["steps"] if s["op"] == "plan_repairs")
        decision = batch["decisions"][0]
        self.assertTrue(decision["accepted"])
        self.assertEqual(decision["start_day"], 2)
        self.assertEqual(decision["technician_id"], "t")

    def test_plan_expect_error_routes_warranty_codes(self):
        svc = open_service(":memory:")
        plan = {
            "steps": [
                {
                    "op": "register_outage",
                    "record_id": "x",
                    "sale_id": "NOPE",
                    "unit_id": "u1",
                    "start_day": 1,
                    "end_day": 2,
                    "expect_error": "UNKNOWN_WARRANTY",
                }
            ]
        }
        result = svc.run(plan)
        step = result["steps"][0]
        # An expected-error step is recorded with the observed code rather than ok=True.
        self.assertFalse(step["ok"])
        self.assertEqual(step["expected_error_observed"], "UNKNOWN_WARRANTY")


if __name__ == "__main__":
    unittest.main()
