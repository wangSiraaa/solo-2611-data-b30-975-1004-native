"""Furnace tests: order validation, exact optimisation, idempotent runs."""

from __future__ import annotations

import random
import unittest

from basket_service import open_service
from basket_service.errors import BasketError
from basket_service.furnace import solve
from basket_service.reference import furnace_reference

def make_workshop():
    svc = open_service(":memory:")
    svc.register_process_sheet({"kind": "UNIT", "sku": "R", "recipe": {"red": 2, "white": 0}})
    svc.register_process_sheet({"kind": "UNIT", "sku": "W", "recipe": {"red": 0, "white": 2}})
    svc.register_process_sheet(
        {
            "kind": "GROUP",
            "coupon_id": "PAIR",
            "members": {"MA": {"red": 1, "white": 1}, "MB": {"red": 1, "white": 1}},
            "loss": {"red": 1, "white": 1},
        }
    )
    snap_r = svc.publish_snapshot([{"sku": "R", "price": 100}], [], snapshot_id="snap-r")
    snap_w = svc.publish_snapshot([{"sku": "W", "price": 100}], [], snapshot_id="snap-w")
    snap_m = svc.publish_snapshot(
        [{"sku": "MA", "price": 100}, {"sku": "MB", "price": 100}],
        [{"coupon_id": "PAIR", "D": 50, "requires": {"MA": 1, "MB": 1}}],
        snapshot_id="snap-m",
    )
    svc.register_sale("SR", quote=svc.quote([{"unit_id": "r1", "sku": "R", "price": 100}], [], "q-r", snap_r))
    svc.register_sale("SW", quote=svc.quote([{"unit_id": "w1", "sku": "W", "price": 100}], [], "q-w", snap_w))
    svc.register_sale(
        "SM",
        quote=svc.quote(
            [{"unit_id": "m1", "sku": "MA", "price": 100}, {"unit_id": "m2", "sku": "MB", "price": 100}],
            [],
            "q-m",
            snap_m,
        ),
    )
    return svc


def scrap_all(svc):
    svc.return_units("ret-r", "SR", ["r1"], {"r1": "SCRAP"})
    svc.return_units("ret-w", "SW", ["w1"], {"w1": "SCRAP"})
    svc.return_units("ret-m", "SM", ["m1", "m2"], {"m1": "SCRAP", "m2": "SCRAP"})

PINK = {"order_id": "pink", "red_fraction": {"min": [1, 2], "max": [1, 2]}, "min_total": 2, "max_total": 4, "value": 3}
RED = {"order_id": "red", "red_fraction": {"min": [1, 1], "max": [1, 1]}, "min_total": 2, "max_total": 2, "value": 2}


def workshop_with_cakes():
    svc = make_workshop()
    scrap_all(svc)
    svc.run_recycling("B1")
    return svc


def plate(service_result, order_id):
    return next(p for p in service_result["plates"] if p["order_id"] == order_id)


class ValidationTests(unittest.TestCase):
    def test_order_validation(self):
        svc = open_service(":memory:")
        bad = [
            [],
            [{"order_id": "a"}],
            [{"order_id": "a", "red_fraction": {"min": [2, 1], "max": [1, 1]}, "min_total": 1, "max_total": 1, "value": 1}],
            [{"order_id": "a", "red_fraction": {"min": [1, 2], "max": [1, 3]}, "min_total": 1, "max_total": 1, "value": 1}],
            [{"order_id": "a", "red_fraction": {"min": [1, 2], "max": [1, 2]}, "min_total": 0, "max_total": 1, "value": 1}],
            [{"order_id": "a", "red_fraction": {"min": [1, 2], "max": [1, 2]}, "min_total": 3, "max_total": 2, "value": 1}],
            [{"order_id": "a", "red_fraction": {"min": [1, 2], "max": [1, 2]}, "min_total": 1, "max_total": 2, "value": 0}],
            [{"order_id": "a", "red_fraction": {"min": [1, 2], "max": [1, 2]}, "min_total": 1, "max_total": 2, "value": True}],
            [{"order_id": "a", "red_fraction": {"min": [1, 0], "max": [1, 1]}, "min_total": 1, "max_total": 2, "value": 1}],
            [{"order_id": "a", "red_fraction": {"min": [1, 2], "max": [1, 2]}, "min_total": 1, "max_total": 2, "value": 1}] * 2,
        ]
        for orders in bad:
            with self.assertRaises(BasketError, msg=str(orders)) as ctx:
                svc.run_furnace("F-bad", orders)
            self.assertEqual(ctx.exception.code, "INVALID_ORDER")
        too_many = [
            {"order_id": f"o{i}", "red_fraction": {"min": [0, 1], "max": [1, 1]}, "min_total": 1, "max_total": 99, "value": 1}
            for i in range(9)
        ]
        with self.assertRaises(BasketError):
            svc.run_furnace("F-many", too_many)

    def test_fraction_normalisation(self):
        svc = make_workshop()
        scrap_all(svc)
        svc.run_recycling("B1")
        # [2/4, 2/4] is the same order as [1/2, 1/2]: same run id must replay.
        order = {"order_id": "pink", "red_fraction": {"min": [2, 4], "max": [2, 4]}, "min_total": 2, "max_total": 4, "value": 3}
        first = svc.run_furnace("F-norm", [order])
        second = svc.run_furnace("F-norm", [PINK])
        self.assertEqual(first, second)
        self.assertEqual(first["orders"][0]["red_fraction"]["min"], [1, 2])


class FurnaceExamplesTests(unittest.TestCase):
    def test_main_example_scores_five(self):
        svc = workshop_with_cakes()
        plan = svc.run_furnace("F1", [PINK, RED])
        self.assertEqual(plan["total_value"], 5)
        pink, red = plate(plan, "pink"), plate(plan, "red")
        self.assertEqual((pink["red"], pink["white"], pink["total"]), (1, 1, 2))
        self.assertEqual((red["red"], red["white"]), (2, 0))
        # The pink plate burns the group cake M: its requisition record names
        # both returned chips m1/m2 of sale SM.
        self.assertEqual(
            [(i["sale_id"], i["unit_id"]) for i in pink["items"]],
            [("SM", "m1"), ("SM", "m2")],
        )
        self.assertEqual([(i["sale_id"], i["unit_id"]) for i in red["items"]], [("SR", "r1")])
        # W stays in stock, unfilled orders listed, run replayable.
        self.assertEqual(len(plan["stock_remaining"]), 1)
        self.assertEqual(plan["unfilled_orders"], [])
        self.assertEqual(svc.run_furnace("F1", [PINK, RED]), plan)
        self.assertTrue(svc.get_furnace_run("F1")["repeated"])

    def test_restocker_variant_scores_three(self):
        svc = make_workshop()
        svc.return_units("ret-r", "SR", ["r1"], {"r1": "SCRAP"})
        svc.return_units("ret-w", "SW", ["w1"], {"w1": "SCRAP"})
        svc.return_units("ret-m1", "SM", ["m1"], {"m1": "SCRAP"})
        svc.return_units("ret-m2", "SM", ["m2"], {"m2": "RESTOCK"})  # group never completes
        svc.run_recycling("B1")
        plan = svc.run_furnace("F1", [PINK, RED])
        self.assertEqual(plan["total_value"], 3)
        pink = plate(plan, "pink")
        self.assertEqual((pink["red"], pink["white"]), (2, 2))
        self.assertEqual(plan["unfilled_orders"], ["red"])

    def test_min_total_four_variant_scores_three(self):
        svc = workshop_with_cakes()
        pink4 = {"order_id": "pink", "red_fraction": {"min": [1, 2], "max": [1, 2]}, "min_total": 4, "max_total": 4, "value": 3}
        plan = svc.run_furnace("F1", [pink4, RED])
        self.assertEqual(plan["total_value"], 3)
        pink = plate(plan, "pink")
        self.assertEqual((pink["red"], pink["white"], pink["total"]), (2, 2, 4))
        self.assertEqual(plan["unfilled_orders"], ["red"])
        # M cannot join any plate and stays in stock.
        self.assertEqual(len(plan["stock_remaining"]), 1)

    def test_aggregate_cannot_be_scooped(self):
        # Pooled, the stock holds 3 red + 3 white; an order for 2/3 red out of
        # 3 portions looks fillable from the pool but no whole-cake set works.
        svc = workshop_with_cakes()
        trap = {"order_id": "trap", "red_fraction": {"min": [2, 3], "max": [2, 3]}, "min_total": 3, "max_total": 3, "value": 100}
        plan = svc.run_furnace("F1", [trap])
        self.assertEqual(plan["total_value"], 0)
        self.assertEqual(plan["unfilled_orders"], ["trap"])

    def test_run_id_conflict_and_leftover_stock_for_later_run(self):
        svc = workshop_with_cakes()
        svc.run_furnace("F1", [PINK, RED])
        with self.assertRaises(BasketError) as ctx:
            svc.run_furnace("F1", [RED])
        self.assertEqual(ctx.exception.code, "FURNACE_CONFLICT")
        white_only = {"order_id": "white", "red_fraction": {"min": [0, 1], "max": [0, 1]}, "min_total": 2, "max_total": 2, "value": 1}
        second = svc.run_furnace("F2", [white_only])
        self.assertEqual(second["total_value"], 1)
        self.assertEqual(svc.cakes("IN_STOCK"), [])
        self.assertEqual(len(svc.cakes("REQUISITIONED")), 3)


class SolverTests(unittest.TestCase):
    def test_solver_matches_bruteforce_on_random_small_cases(self):
        rng = random.Random(20261122)
        for trial in range(200):
            n_cakes = rng.randint(1, 10)
            cakes = [
                {"batch_no": f"c{i}", "red": rng.randint(0, 3), "white": rng.randint(0, 3)}
                for i in range(n_cakes)
            ]
            for cake in cakes:
                if cake["red"] + cake["white"] == 0:
                    cake["red"] = 1
            n_orders = rng.randint(1, 4)
            orders = []
            for j in range(n_orders):
                lo = rng.randint(0, 4)
                hi = rng.randint(lo, 4)
                min_total = rng.randint(1, 4)
                orders.append(
                    {
                        "order_id": f"o{j}",
                        "red_fraction": {"min": [lo, 4], "max": [hi, 4]},
                        "min_total": min_total,
                        "max_total": min_total + rng.randint(0, 4),
                        "value": rng.randint(1, 9),
                    }
                )
            expected = furnace_reference(cakes, orders)
            got = solve(cakes, orders)["total_value"]
            self.assertEqual(got, expected, f"case {trial}: {cakes} {orders}")

    def test_solver_handles_batched_identical_cakes(self):
        # 24 identical small cakes + a big one, 8 orders: the grouped DP stays
        # tiny and exact.
        cakes = [{"batch_no": f"s{i:02d}", "red": 1, "white": 1} for i in range(23)]
        cakes.append({"batch_no": "big", "red": 30, "white": 0})
        orders = [
            {"order_id": "big-only", "red_fraction": {"min": [1, 1], "max": [1, 1]}, "min_total": 20, "max_total": 40, "value": 50}
        ] + [
            {"order_id": f"half-{i}", "red_fraction": {"min": [1, 2], "max": [1, 2]}, "min_total": 2, "max_total": 4, "value": 3}
            for i in range(7)
        ]
        plan = solve(cakes, orders)
        self.assertEqual(plan["total_value"], 50 + 7 * 3)
        big_plate = next(p for p in plan["plates"] if p["order_id"] == "big-only")
        self.assertEqual(big_plate["batch_nos"], ["big"])

    def test_complexity_guard(self):
        # 24 distinct recipes -> state space 2^24, beyond the guard.
        cakes = [{"batch_no": f"c{i}", "red": i, "white": 0} for i in range(24)]
        with self.assertRaises(BasketError) as ctx:
            solve(cakes, [{"order_id": "o", "red_fraction": {"min": [0, 1], "max": [1, 1]}, "min_total": 1, "max_total": 100, "value": 1}])
        self.assertEqual(ctx.exception.code, "FURNACE_COMPLEXITY")


if __name__ == "__main__":
    unittest.main()
