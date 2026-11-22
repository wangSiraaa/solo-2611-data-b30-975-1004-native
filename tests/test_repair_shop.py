"""Repair shop tests: feasibility, value maximisation, tie-breaks, version pinning."""

from __future__ import annotations

import unittest

from basket_service import open_service
from basket_service.errors import BasketError


def build(svc, n, sku="R", base_days=30, sale_id="S"):
    units = [{"unit_id": f"u{i}", "sku": sku, "price": 1000} for i in range(n)]
    snap = svc.publish_snapshot([{"sku": sku, "price": 1000, "quantity": n}], [], snapshot_id=f"snap-{sale_id}")
    quote = svc.quote(units, [], f"q-{sale_id}", snap)
    svc.register_sale(sale_id, quote=quote)
    svc.publish_warranty_term(f"term-{sale_id}", sku, base_days)
    svc.enroll_warranties(sale_id, 0)
    return sale_id


def build_brief_units(svc):
    """Two same-price R units; uA receives the EXT coupon (+2 days), uB does not.

    Returns the sale id.  Base term is 3 days, addendum 2 days.
    """

    basket = [
        {"unit_id": "uA", "sku": "R", "price": 1000},
        {"unit_id": "uB", "sku": "R", "price": 1000},
    ]
    coupons = [{"coupon_id": "EXT", "D": 100, "requires": {"R": 1}}]
    snap = svc.publish_snapshot([{"sku": "R", "price": 1000, "quantity": 2}], coupons, snapshot_id="snap-brief")
    quote = svc.quote(basket, [], "q-brief", snap)
    svc.register_sale("SB", quote=quote)
    svc.publish_warranty_term("tb-base", "R", 3)
    svc.publish_warranty_term("tb-ext", "R", 3, coupon_id="EXT", bonus_days=2)
    svc.enroll_warranties("SB", 0)
    return "SB"


class FeasibilityTests(unittest.TestCase):
    def setUp(self):
        self.svc = open_service(":memory:")
        build(self.svc, 2)
        self.svc.register_repair_method("m1", "R", 1, "L1")
        self.svc.register_technician("tech", "L1", unavailable=[(0, 7), (8, 60)])

    def test_brief_one_day_slot_accepts_covered_unit_only(self):
        # Dedicated brief setup: uA holds the coupon (5-day budget), uB base only.
        svc = open_service(":memory:")
        sale_id = build_brief_units(svc)
        svc.register_repair_method("m1", "R", 1, "L1")
        svc.register_technician("tech", "L1", unavailable=[(0, 7), (8, 60)])  # free only day 7
        # Outages registered before work orders so each order pins the new version.
        svc.register_outage("a1", sale_id, "uA", 1, 3)
        svc.register_outage("a2", sale_id, "uA", 2, 4)  # union 3 paused -> expiry day 8
        svc.register_outage("b1", sale_id, "uB", 1, 3)
        svc.register_outage("b2", sale_id, "uB", 2, 4)  # expiry day 6
        self.assertEqual(svc.get_warranty(sale_id, "uA")["latest_start_day"], 7)
        self.assertEqual(svc.get_warranty(sale_id, "uB")["latest_start_day"], 5)
        svc.open_work_order("WO-A", sale_id, "uA", 6, 500, ["m1"])
        svc.open_work_order("WO-B", sale_id, "uB", 6, 500, ["m1"])
        result = svc.plan_repairs("B1", ["WO-A", "WO-B"], 30)
        decisions = {d["work_order_id"]: d for d in result["decisions"]}
        self.assertTrue(decisions["WO-A"]["accepted"])
        self.assertEqual(decisions["WO-A"]["start_day"], 7)
        self.assertEqual(decisions["WO-A"]["technician_id"], "tech")
        # uB's cover ends on day 5 but delivery is day 6: no covered start day
        # exists on/after delivery, so it is a hard (non-optimisation) reject.
        self.assertFalse(decisions["WO-B"]["accepted"])
        self.assertEqual(decisions["WO-B"]["reason_code"], "NO_COVERED_START_DAY")
        self.assertEqual(result["total_value"], 500)

    def test_unit_expired_before_delivery_is_hard_rejected_even_with_free_tech(self):
        # A fully available technician cannot rescue an order whose warranty
        # already ended before the delivery day.
        svc = open_service(":memory:")
        build(svc, 1, base_days=3)
        svc.register_repair_method("m1", "R", 1, "L1")
        svc.register_technician("free", "L1")
        svc.open_work_order("WO", "S", "u0", 9, 10, ["m1"])  # warranty ends day 3
        result = svc.plan_repairs("B", ["WO"], 20)
        self.assertEqual(result["decisions"][0]["reason_code"], "NO_COVERED_START_DAY")
        self.assertEqual(result["total_value"], 0)

    def test_two_day_method_with_one_free_day_rejected(self):
        svc = open_service(":memory:")
        sale_id = build_brief_units(svc)
        svc.register_repair_method("m2", "R", 2, "L1")
        svc.register_technician("tech", "L1", unavailable=[(0, 7), (8, 60)])  # free only day 7
        svc.register_outage("a1", sale_id, "uA", 1, 3)
        svc.register_outage("a2", sale_id, "uA", 2, 4)  # cover reaches day 7
        svc.open_work_order("WO", sale_id, "uA", 6, 500, ["m2"])
        result = svc.plan_repairs("B2", ["WO"], 30)
        decision = result["decisions"][0]
        self.assertFalse(decision["accepted"])
        self.assertEqual(decision["reason_code"], "NO_SLOT_WITHIN_HORIZON")

    def test_delivery_after_warranty_rejected(self):
        self.svc.open_work_order("WO", "S", "u1", 100, 10, ["m1"])
        result = self.svc.plan_repairs("B", ["WO"], 120)
        self.assertEqual(result["decisions"][0]["reason_code"], "NO_COVERED_START_DAY")

    def test_wrong_specialty_and_unknown_method(self):
        # Only a non-matching specialty exists -> no qualified technician.
        svc = open_service(":memory:")
        build(svc, 1)
        svc.register_repair_method("m1", "R", 1, "L1")
        svc.register_technician("mismatch", "OTHER")
        svc.open_work_order("WO", "S", "u0", 0, 10, ["m1"])
        result = svc.plan_repairs("B", ["WO"], 10)
        self.assertEqual(result["decisions"][0]["reason_code"], "NO_QUALIFIED_TECHNICIAN")
        # An unregistered method cannot be attached to a work order.
        with self.assertRaises(BasketError) as ctx:
            self.svc.open_work_order("WO-X", "S", "u1", 0, 10, ["nope"])
        self.assertEqual(ctx.exception.code, "UNKNOWN_METHOD")


class OptimisationTests(unittest.TestCase):
    def test_maximises_total_value_under_capacity(self):
        svc = open_service(":memory:")
        build(svc, 4)
        svc.register_repair_method("m1", "R", 1, "L")
        svc.register_technician("t1", "L")
        for i, value in enumerate([100, 300, 200, 50]):
            svc.open_work_order(f"WO{i}", "S", f"u{i}", 0, value, ["m1"])
        result = svc.plan_repairs("B", ["WO0", "WO1", "WO2", "WO3"], 2)
        accepted = {d["work_order_id"] for d in result["decisions"] if d["accepted"]}
        self.assertEqual(accepted, {"WO1", "WO2"})
        self.assertEqual(result["total_value"], 500)

    def test_rush_many_short_jobs_contend_for_few_days(self):
        svc = open_service(":memory:")
        n = 14
        build(svc, n, sale_id="SR")
        svc.register_repair_method("mm", "R", 1, "L")
        svc.register_technician("g1", "L", unavailable=[(3, 40)])
        for i in range(n):
            svc.open_work_order(f"G{i}", "SR", f"u{i}", 0, 10 * (i + 1), ["mm"])
        result = svc.plan_repairs("GB", [f"G{i}" for i in range(n)], 40)
        self.assertEqual(result["accepted"], 3)
        self.assertEqual(result["rejected"], 11)
        # The three highest-value orders win the three available days.
        self.assertEqual(result["total_value"], 10 * (14 + 13 + 12))
        days = sorted(c["day"] for c in svc.technician_calendar("g1"))
        self.assertEqual(days, [0, 1, 2])

    def test_long_sparse_calendar_few_complex_machines(self):
        svc = open_service(":memory:")
        build(svc, 3, base_days=200, sale_id="SL")
        svc.register_repair_method("long", "R", 2, "L")
        svc.register_technician("sparse", "L", unavailable=[(0, 50), (52, 300)])
        for i in range(3):
            svc.open_work_order(f"L{i}", "SL", f"u{i}", 0, 100, ["long"])
        result = svc.plan_repairs("LB", ["L0", "L1", "L2"], 300)
        self.assertEqual(result["accepted"], 1)
        accepted = next(d for d in result["decisions"] if d["accepted"])
        self.assertEqual(accepted["start_day"], 50)
        self.assertEqual(accepted["end_day"], 52)
        self.assertEqual(accepted["technician_id"], "sparse")

    def test_deterministic_tie_break_same_value(self):
        svc = open_service(":memory:")
        build(svc, 3)
        svc.register_repair_method("m1", "R", 1, "L")
        svc.register_technician("t1", "L")
        for i in range(3):
            svc.open_work_order(f"WO{i}", "S", f"u{i}", 0, 100, ["m1"])
        first = svc.plan_repairs("B", ["WO0", "WO1", "WO2"], 1)
        # Only day 0 exists in the horizon; one order wins deterministically.
        accepted = [d["work_order_id"] for d in first["decisions"] if d["accepted"]]
        self.assertEqual(accepted, ["WO0"])
        replay = svc.plan_repairs("B", ["WO0", "WO1", "WO2"], 1)
        self.assertEqual(replay["decisions"], first["decisions"])

    def test_two_technicians_same_specialty_never_double_booked(self):
        svc = open_service(":memory:")
        build(svc, 4)
        svc.register_repair_method("m1", "R", 1, "L")
        svc.register_technician("t1", "L")
        svc.register_technician("t2", "L")
        for i in range(4):
            svc.open_work_order(f"WO{i}", "S", f"u{i}", 0, 100, ["m1"])
        result = svc.plan_repairs("B", [f"WO{i}" for i in range(4)], 2)
        self.assertEqual(result["accepted"], 4)
        # Each technician holds at most one unit per day.
        slots = [(d["technician_id"], d["start_day"]) for d in result["decisions"] if d["accepted"]]
        self.assertEqual(len(slots), len(set(slots)))


class VersionPinningAndPersistenceTests(unittest.TestCase):
    def test_historical_schedule_uses_pinned_warranty_version(self):
        svc = open_service(":memory:")
        build(svc, 1, base_days=5)
        svc.register_repair_method("m", "R", 1, "L")
        svc.register_technician("t", "L")
        svc.open_work_order("OLD", "S", "u0", 4, 100, ["m"])  # pinned to v1
        self.assertEqual(svc.get_work_order("OLD")["warranty_version"], 1)
        svc.register_outage("b1", "S", "u0", 0, 3, registered_day=9)  # now v2
        old = svc.plan_repairs("BATCH-OLD", ["OLD"], 20)["decisions"][0]
        self.assertTrue(old["accepted"])
        self.assertEqual(old["start_day"], 4)  # legal under v1's latest start day 4
        self.assertEqual(old["warranty_version"], 1)
        self.assertEqual(svc.get_booking_decision("OLD")["warranty_version"], 1)

    def test_booked_order_cannot_be_replanned_and_batch_replays(self):
        svc = open_service(":memory:")
        build(svc, 1)
        svc.register_repair_method("m", "R", 1, "L")
        svc.register_technician("t", "L")
        svc.open_work_order("WO", "S", "u0", 0, 10, ["m"])
        first = svc.plan_repairs("B", ["WO"], 5)
        again = svc.plan_repairs("B", ["WO"], 5)
        self.assertEqual(again["decisions"], first["decisions"])
        with self.assertRaises(BasketError) as ctx:
            svc.plan_repairs("B2", ["WO"], 5)
        self.assertEqual(ctx.exception.code, "WORK_ORDER_CONFLICT")

    def test_persistence_across_reopen(self):
        import tempfile
        from pathlib import Path
        from basket_service import open_service as reopen

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "shop.db")
            svc = open_service(path)
            build(svc, 1)
            svc.register_repair_method("m", "R", 1, "L")
            svc.register_technician("t", "L")
            svc.open_work_order("WO", "S", "u0", 0, 10, ["m"])
            svc.plan_repairs("B", ["WO"], 5)
            del svc
            svc2 = reopen(path)
            batch = svc2.get_repair_batch("B")
            self.assertEqual(batch["total_value"], 10)
            self.assertEqual(svc2.technician_calendar("t")[0]["work_order_id"], "WO")


if __name__ == "__main__":
    unittest.main()
