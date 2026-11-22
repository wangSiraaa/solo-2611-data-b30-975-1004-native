"""Recycling tests: sealed sheets, batches, cakes, provenance and audit."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from basket_service import open_service
from basket_service.errors import BasketError

UNIT_R = {"kind": "UNIT", "sku": "R", "recipe": {"red": 2, "white": 0}}
UNIT_W = {"kind": "UNIT", "sku": "W", "recipe": {"red": 0, "white": 2}}
GROUP_PAIR = {
    "kind": "GROUP",
    "coupon_id": "PAIR",
    "members": {"MA": {"red": 1, "white": 1}, "MB": {"red": 1, "white": 1}},
    "loss": {"red": 1, "white": 1},
}


def make_workshop(path=":memory:", sheets=(UNIT_R, UNIT_W, GROUP_PAIR)):
    svc = open_service(path)
    for sheet in sheets:
        svc.register_process_sheet(sheet)
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


class SheetTests(unittest.TestCase):
    def test_register_sheet_idempotent_and_conflict(self):
        svc = open_service(":memory:")
        self.assertEqual(svc.register_process_sheet(UNIT_R), "UNIT/R")
        self.assertEqual(svc.register_process_sheet(UNIT_R), "UNIT/R")
        with self.assertRaises(BasketError) as ctx:
            svc.register_process_sheet({"kind": "UNIT", "sku": "R", "recipe": {"red": 1, "white": 1}})
        self.assertEqual(ctx.exception.code, "SHEET_CONFLICT")
        sheet = svc.get_sheet("UNIT/R")
        self.assertEqual(sheet["recipe"], {"red": 2, "white": 0})
        with self.assertRaises(BasketError) as ctx:
            svc.get_sheet("UNIT/ZZ")
        self.assertEqual(ctx.exception.code, "UNKNOWN_SHEET")

    def test_sheet_validation(self):
        svc = open_service(":memory:")
        bad = [
            {"kind": "UNIT", "sku": "R", "recipe": {"red": 0, "white": 0}},
            {"kind": "UNIT", "sku": "R", "recipe": {"red": -1, "white": 2}},
            {"kind": "UNIT", "sku": "R", "recipe": {"red": 1.5, "white": 0}},
            {"kind": "UNIT", "sku": "", "recipe": {"red": 1, "white": 0}},
            {"kind": "GROUP", "coupon_id": "P", "members": {}, "loss": {"red": 0, "white": 0}},
            {"kind": "GROUP", "coupon_id": "P", "members": {"A": {"red": 1, "white": 0}}},
            {"kind": "GROUP", "coupon_id": "P", "members": {"A": {"red": 1, "white": 0}}, "loss": {"red": -1, "white": 0}},
            {"kind": "X", "sku": "R", "recipe": {"red": 1, "white": 0}},
            {"kind": "UNIT", "sku": "R", "recipe": {"red": True, "white": 0}},
        ]
        for sheet in bad:
            with self.assertRaises(BasketError, msg=str(sheet)) as ctx:
                svc.register_process_sheet(sheet)
            self.assertEqual(ctx.exception.code, "INVALID_SHEET")


class BatchTests(unittest.TestCase):
    def test_scrap_units_become_cakes_restocks_do_not(self):
        svc = make_workshop()
        svc.return_units("ret-r", "SR", ["r1"], {"r1": "SCRAP"})
        svc.return_units("ret-w", "SW", ["w1"], {"w1": "RESTOCK"})  # RESTOCK is never furnace material
        svc.return_units("ret-m", "SM", ["m1", "m2"], {"m1": "SCRAP", "m2": "SCRAP"})
        batch = svc.run_recycling("B1")
        by_kind = {(c["kind"], c.get("sku") or c["coupon_id"]): c for c in batch["cakes"]}
        self.assertIn(("UNIT", "R"), by_kind)
        self.assertIn(("GROUP", "PAIR"), by_kind)
        self.assertNotIn(("UNIT", "W"), by_kind)  # RESTOCK unit stays out of the furnace
        group = by_kind[("GROUP", "PAIR")]
        self.assertEqual((group["red"], group["white"]), (1, 1))
        self.assertEqual(group["material"], {"red": 2, "white": 2})
        self.assertEqual(group["loss"], {"red": 1, "white": 1})
        self.assertEqual([m["unit_id"] for m in group["members"]], ["m1", "m2"])
        self.assertTrue(svc.audit_recycling()["ok"])

    def test_group_pending_until_all_members_scrap_received(self):
        svc = make_workshop()
        svc.return_units("ret-m1", "SM", ["m1"], {"m1": "SCRAP"})
        first = svc.run_recycling("B1")
        self.assertEqual(first["cakes"], [])
        self.assertEqual(
            first["pending"],
            [{"sale_id": "SM", "coupon_id": "PAIR", "received": ["m1"], "awaiting": ["m2"]}],
        )
        # Replaying the batch id changes nothing; the group completes in a new batch.
        self.assertEqual(svc.run_recycling("B1"), first)
        svc.return_units("ret-m2", "SM", ["m2"], {"m2": "SCRAP"})
        second = svc.run_recycling("B2")
        self.assertEqual(len(second["cakes"]), 1)
        self.assertEqual(second["cakes"][0]["kind"], "GROUP")
        self.assertEqual((second["cakes"][0]["red"], second["cakes"][0]["white"]), (1, 1))
        # A third sweep finds nothing unconsumed.
        self.assertEqual(svc.run_recycling("B3")["cakes"], [])
        self.assertTrue(svc.audit_recycling()["ok"])

    def test_restock_member_blocks_group_forever(self):
        svc = make_workshop()
        svc.return_units("ret-m1", "SM", ["m1"], {"m1": "SCRAP"})
        svc.return_units("ret-m2", "SM", ["m2"], {"m2": "RESTOCK"})
        batch = svc.run_recycling("B1")
        self.assertEqual(batch["cakes"], [])
        self.assertEqual(batch["pending"][0]["awaiting"], ["m2"])
        self.assertEqual(svc.cakes(), [])

    def test_manual_scrap_count_never_mints_a_cake(self):
        svc = make_workshop()
        svc.adjust_inventory("manual-scrap", "R", 25, "SCRAP")
        svc.adjust_inventory("manual-scrap-2", "MA", 25, "SCRAP")
        batch = svc.run_recycling("B1")
        self.assertEqual(batch["cakes"], [])
        self.assertEqual(batch["pending"], [])
        # The old inventory queries still work and are untouched by recycling.
        self.assertEqual(svc.inventory()["SCRAP"], {"R": 25, "MA": 25})

    def test_item_enters_exactly_one_batch_and_survives_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "shop.db")
            svc = make_workshop(path)
            scrap_all(svc)
            first = svc.run_recycling("B1")
            self.assertEqual(len(first["cakes"]), 3)
            replay = svc.run_recycling("B1")
            self.assertEqual(replay, first)
            again = svc.run_recycling("B2")
            self.assertEqual(again["cakes"], [])
            self.assertEqual(len(svc.cakes()), 3)
            del svc
            reopened = open_service(path)
            self.assertEqual(len(reopened.cakes()), 3)
            self.assertEqual(reopened.run_recycling("B3")["cakes"], [])
            self.assertTrue(reopened.audit_recycling()["ok"])
            stored = reopened.get_recycle_batch("B1")
            self.assertTrue(stored["repeated"])
            self.assertEqual(len(stored["cakes"]), 3)

    def test_no_sheet_means_skipped_not_caked(self):
        svc = open_service(":memory:")
        svc.register_process_sheet(UNIT_R)
        snap = svc.publish_snapshot([{"sku": "R", "price": 100}, {"sku": "Z", "price": 100}], [], snapshot_id="s")
        svc.register_sale(
            "S1",
            quote=svc.quote(
                [{"unit_id": "r1", "sku": "R", "price": 100}, {"unit_id": "z1", "sku": "Z", "price": 100}],
                [],
                "q1",
                snap,
            ),
        )
        svc.return_units("ret", "S1", ["r1", "z1"], {"r1": "SCRAP", "z1": "SCRAP"})
        batch = svc.run_recycling("B1")
        self.assertEqual(len(batch["cakes"]), 1)
        self.assertEqual(batch["skipped"], [{"sale_id": "S1", "unit_id": "z1", "sku": "Z", "reason": "NO_SHEET"}])
        # Registering the Z sheet later lets a later batch pick the item up.
        svc.register_process_sheet({"kind": "UNIT", "sku": "Z", "recipe": {"red": 0, "white": 3}})
        second = svc.run_recycling("B2")
        self.assertEqual(len(second["cakes"]), 1)
        self.assertEqual((second["cakes"][0]["red"], second["cakes"][0]["white"]), (0, 3))

    def test_group_sheet_only_binds_actual_coupon_groups(self):
        # Same SKU sold with and without the coupon: only the actual coupon
        # group de-films as a group; the plain sale cakes per piece.
        svc = open_service(":memory:")
        svc.register_process_sheet({"kind": "UNIT", "sku": "A", "recipe": {"red": 5, "white": 0}})
        svc.register_process_sheet(
            {
                "kind": "GROUP",
                "coupon_id": "DUO",
                "members": {"A": {"red": 5, "white": 0}},
                "loss": {"red": 1, "white": 0},
            }
        )
        snap_plain = svc.publish_snapshot([{"sku": "A", "price": 100}], [], snapshot_id="plain")
        snap_duo = svc.publish_snapshot(
            [{"sku": "A", "price": 100}],
            [{"coupon_id": "DUO", "D": 30, "requires": {"A": 2}}],
            snapshot_id="duo",
        )
        svc.register_sale("S-plain", quote=svc.quote([{"unit_id": "a0", "sku": "A", "price": 100}], [], "q-p", snap_plain))
        svc.register_sale(
            "S-duo",
            quote=svc.quote(
                [{"unit_id": "a1", "sku": "A", "price": 100}, {"unit_id": "a2", "sku": "A", "price": 100}],
                [],
                "q-d",
                snap_duo,
            ),
        )
        svc.return_units("r-plain", "S-plain", ["a0"], {"a0": "SCRAP"})
        svc.return_units("r-duo", "S-duo", ["a1", "a2"], {"a1": "SCRAP", "a2": "SCRAP"})
        batch = svc.run_recycling("B1")
        cakes = sorted(batch["cakes"], key=lambda c: c["kind"])
        self.assertEqual([c["kind"] for c in cakes], ["GROUP", "UNIT"])
        self.assertEqual((cakes[0]["red"], cakes[0]["white"]), (9, 0))  # 5+5-1 group loss
        self.assertEqual((cakes[1]["red"], cakes[1]["white"]), (5, 0))
        self.assertTrue(svc.audit_recycling()["ok"])


    def test_concurrent_batches_cannot_double_consume(self):
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "race.db")
            svc = make_workshop(path)
            scrap_all(svc)
            del svc
            results, errors = [], []
            barrier = threading.Barrier(2)

            def worker(batch_id):
                client = open_service(path)
                try:
                    barrier.wait()
                    results.append(client.run_recycling(batch_id))
                except BasketError as exc:
                    errors.append(exc.code)

            threads = [threading.Thread(target=worker, args=(f"B{i}",)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            # Both batches succeed (each sweeps what it sees), but every
            # physical item still entered exactly one batch.
            self.assertEqual(errors, [])
            final = open_service(path)
            cakes = final.cakes()
            self.assertEqual(len(cakes), 3)
            self.assertTrue(final.audit_recycling()["ok"])
            total_caked = sum(len(b["cakes"]) for b in results)
            self.assertEqual(total_caked, 3)


if __name__ == "__main__":
    unittest.main()
