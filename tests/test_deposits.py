"""Tests for container deposits, clearance, vouchers and shipment planning."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from reagent import connect, initialize
from reagent.deposits import DepositService
from reagent.errors import Conflict, ValidationError
from reagent.inventory import InventoryService
from reagent.runs import RunService

ROOT = Path(__file__).resolve().parent.parent
MATERIALS = ROOT / "materials" / "719"


def _credential(deposit=4, latest=12, supplier="SUP", model="PK-A"):
    return {
        "supplier": supplier,
        "package_model": model,
        "deposit": deposit,
        "latest_return_day": latest,
    }


# Default scenario: A and B are the same model sold by the same supplier, each
# with a 4-unit deposit and a return-by day of 12.  C is another kind, D is an
# extra R bottle left untouched.
SCENARIO = {
    "initial_day": 10,
    "kinds": [
        {"kind": "R", "open_lifetime_days": 2},
        {"kind": "S", "open_lifetime_days": 2},
    ],
    "bottles": [
        {
            "bottle_id": "A",
            "kind": "R",
            "total_portions": 1,
            "intake_day": 10,
            "printed_expiry_day": 20,
            "opened_day": None,
            "credential": _credential(),
        },
        {
            "bottle_id": "B",
            "kind": "R",
            "total_portions": 1,
            "intake_day": 10,
            "printed_expiry_day": 12,
            "opened_day": None,
            "credential": _credential(),
        },
        {
            "bottle_id": "C",
            "kind": "S",
            "total_portions": 1,
            "intake_day": 10,
            "printed_expiry_day": 20,
            "opened_day": None,
            "credential": _credential(model="PK-S"),
        },
        {
            "bottle_id": "D",
            "kind": "R",
            "total_portions": 1,
            "intake_day": 10,
            "printed_expiry_day": 20,
            "opened_day": None,
            "credential": _credential(),
        },
    ],
    "methods": [
        {"method_id": "USE1", "revision": 1, "requirements": {"R": 1}},
    ],
}


class DepositTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "store.db")
        self.conn = connect(self.db_path)
        initialize(self.conn)
        self.inventory = InventoryService(self.conn)
        self.runs = RunService(self.conn)
        self.deposits = DepositService(self.conn)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        finally:
            self._tmp.cleanup()

    def init(self, document=None) -> None:
        self.inventory.initialize(document or SCENARIO)

    # helpers -------------------------------------------------------------
    def sell_a(self, day: int = 10) -> None:
        """Run A dry through the ordinary run path (its last consumption)."""
        self.runs.create_run(
            "use-A", "USE1", 1, day, [{"bottle_id": "A", "portions": 1}]
        )
        self.runs.start_run("use-A", day)

    def clear_b(self, day: int = 12) -> None:
        self.deposits.open_clearance("B", 1, day)
        self.deposits.sign_clearance("CLR:B", "count-1", day)
        self.deposits.sign_clearance("CLR:B", "count-2", day)

    def one_trip(self, arrival: int = 12, price: int = 5) -> None:
        self.deposits.add_supplier_kind("SUP", "R")
        self.deposits.add_supplier_kind("SUP", "S")
        self.deposits.add_trip("T", 12, arrival, 3)
        self.deposits.add_box_model("TWO", 2, ["R", "S"])
        self.deposits.add_trip_offer("T", "TWO", price)


class CredentialTests(DepositTestCase):
    def test_credential_is_read_from_document(self) -> None:
        self.init()
        cred = self.deposits.credential("A")
        self.assertEqual(cred["supplier"], "SUP")
        self.assertEqual(cred["deposit"], 4)
        self.assertEqual(cred["latest_return_day"], 12)

    def test_bottle_without_credential_has_no_voucher(self) -> None:
        doc = json.loads(json.dumps(SCENARIO))
        del doc["bottles"][0]["credential"]
        self.init(doc)
        self.sell_a()
        with self.assertRaises(Conflict):
            self.deposits.apply_voucher("A", 12)
        self.assertIn("missing supply credential", self.deposits.voucher_blockers("A", 12))

    def test_credential_validation(self) -> None:
        doc = json.loads(json.dumps(SCENARIO))
        doc["bottles"][0]["credential"]["deposit"] = -1
        with self.assertRaises(ValidationError):
            self.inventory.initialize(doc)
        doc = json.loads(json.dumps(SCENARIO))
        doc["bottles"][0]["credential"]["supplier"] = ""
        with self.assertRaises(ValidationError):
            self.inventory.initialize(doc)


class ClearanceConservationTests(DepositTestCase):
    def test_cleared_is_a_separate_destination(self) -> None:
        self.init()
        self.clear_b()
        rows = {r["bottle_id"]: r for r in self.inventory.conservation()}
        b = rows["B"]
        self.assertEqual(b["cleared"], 1)
        self.assertEqual(b["consumed"], 0)  # never disguised as consumption
        self.assertEqual(b["unallocated"], 0)
        self.assertEqual(
            b["total_portions"],
            b["unallocated"] + b["reserved"] + b["consumed"] + b["cleared"],
        )

    def test_expired_unallocated_cannot_be_faked_as_consumed(self) -> None:
        self.init()
        # B is expired on day 12 with one unallocated portion.
        self.assertEqual(self.inventory.query("B", 12)["unallocated"], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO clearance_requests(request_id, bottle_id, "
                "inventory_version, portions, status, created_day) "
                "VALUES('x', 'B', 0, 2, 'cleared', 12)"
            )

    def test_cleared_request_is_frozen(self) -> None:
        self.init()
        self.clear_b()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE clearance_requests SET portions = 2 "
                "WHERE request_id = 'CLR:B'"
            )


class ClearanceRuleTests(DepositTestCase):
    def test_clearance_requires_expired_bottle(self) -> None:
        self.init()
        # C is valid on day 12 -> cannot be cleared just because unallocated.
        with self.assertRaises(Conflict):
            self.deposits.open_clearance("C", 1, 12)

    def test_clearance_must_equal_exact_unallocated(self) -> None:
        self.init()
        with self.assertRaises(Conflict):
            self.deposits.open_clearance("B", 2, 12)

    def test_clearance_blocked_while_reserved(self) -> None:
        self.init()
        self.inventory.hold("old-holder", [("B", 1)], 10)
        with self.assertRaises(Conflict):
            self.deposits.open_clearance("B", 1, 12)

    def test_two_different_counters_same_version_required(self) -> None:
        self.init()
        self.deposits.open_clearance("B", 1, 12)
        view = self.deposits.sign_clearance("CLR:B", "count-1", 12)
        self.assertEqual(view["status"], "pending")
        with self.assertRaises(Conflict):
            self.deposits.sign_clearance("CLR:B", "count-1", 12)  # same counter
        view = self.deposits.sign_clearance("CLR:B", "count-2", 12)
        self.assertEqual(view["status"], "cleared")

    def test_counters_signing_different_versions_do_not_clear(self) -> None:
        self.init()
        self.deposits.open_clearance("B", 1, 12)
        self.deposits.sign_clearance("CLR:B", "count-1", 12, inventory_version=0)
        view = self.deposits.sign_clearance(
            "CLR:B", "count-2", 12, inventory_version=1
        )
        self.assertEqual(view["status"], "pending")
        with self.assertRaises(Conflict):
            self.deposits.apply_voucher("B", 12)

    def test_recount_then_matching_versions_clear(self) -> None:
        self.init()
        self.deposits.open_clearance("B", 1, 12)
        self.deposits.sign_clearance("CLR:B", "count-1", 12, inventory_version=0)
        self.deposits.recount("B", 12)
        # second counter restates the new version -> still pending (count-1 is
        # on the old version)
        self.deposits.sign_clearance("CLR:B", "count-2", 12, inventory_version=1)
        self.assertEqual(
            self.deposits.clearance_view("CLR:B")["status"], "pending"
        )

    def test_counters_must_agree_on_portions(self) -> None:
        self.init()
        self.deposits.open_clearance("B", 1, 12)
        self.deposits.sign_clearance("CLR:B", "count-1", 12, portions=1)
        view = self.deposits.sign_clearance("CLR:B", "count-2", 12, portions=0)
        self.assertEqual(view["status"], "pending")


class VoucherEligibilityTests(DepositTestCase):
    def test_consumed_bottle_is_eligible(self) -> None:
        self.init()
        self.sell_a()
        self.assertTrue(self.deposits.explain_voucher("A", 12)["eligible"])
        voucher = self.deposits.apply_voucher("A", 12)
        self.assertEqual(voucher["deposit"], 4)
        self.assertEqual(voucher["status"], "open")

    def test_unconsumed_valid_stock_is_not_eligible(self) -> None:
        self.init()
        info = self.deposits.explain_voucher("C", 12)
        self.assertFalse(info["eligible"])
        with self.assertRaises(Conflict):
            self.deposits.apply_voucher("C", 12)

    def test_expired_stock_needs_clearance_first(self) -> None:
        self.init()
        self.assertFalse(self.deposits.explain_voucher("B", 12)["eligible"])
        self.deposits.open_clearance("B", 1, 12)
        self.deposits.sign_clearance("CLR:B", "count-1", 12)
        self.assertFalse(self.deposits.explain_voucher("B", 12)["eligible"])
        self.deposits.sign_clearance("CLR:B", "count-2", 12)
        self.assertTrue(self.deposits.explain_voucher("B", 12)["eligible"])

    def test_active_reservation_blocks_voucher(self) -> None:
        self.init()
        self.sell_a()
        self.inventory.hold("old-holder", [("D", 1)], 10)
        # D is fully reserved -> still has an active reservation, no voucher
        self.assertFalse(self.deposits.explain_voucher("D", 11)["eligible"])

    def test_past_return_day_blocks_voucher(self) -> None:
        self.init()
        self.sell_a()
        self.assertFalse(self.deposits.explain_voucher("A", 13)["eligible"])
        with self.assertRaises(Conflict):
            self.deposits.apply_voucher("A", 13)

    def test_only_one_surviving_voucher_per_bottle(self) -> None:
        self.init()
        self.sell_a()
        self.deposits.apply_voucher("A", 12)
        with self.assertRaises(Conflict):
            self.deposits.apply_voucher("A", 12)


class ShipmentPlanTests(DepositTestCase):
    def test_single_bottle_would_lose_money_so_ships_nothing(self) -> None:
        self.init()
        self.sell_a()
        self.deposits.apply_voucher("A", 12)
        self.one_trip()
        plan = self.deposits.plan_shipment(12)
        self.assertEqual(plan["bottle_count"], 0)
        self.assertEqual(plan["profit"], 0)

    def test_two_bottles_after_clearance_net_three(self) -> None:
        self.init()
        self.sell_a()
        self.clear_b()
        self.deposits.apply_voucher("A", 12)
        self.deposits.apply_voucher("B", 12)
        self.one_trip()
        plan = self.deposits.plan_shipment(12)
        self.assertEqual(plan["bottle_count"], 2)
        self.assertEqual(plan["deposit_total"], 8)
        self.assertEqual(plan["box_cost"], 5)
        self.assertEqual(plan["profit"], 3)
        self.assertEqual(plan["trips_used"], ["T"])

    def test_late_trip_pays_nothing(self) -> None:
        self.init()
        self.sell_a()
        self.clear_b()
        self.deposits.apply_voucher("A", 12)
        self.deposits.apply_voucher("B", 12)
        # trip departs 12 but arrives 13, past the return-by day 12
        self.one_trip(arrival=13)
        plan = self.deposits.plan_shipment(12)
        self.assertEqual(plan["bottle_count"], 0)

    def test_ship_persists_and_locks_vouchers(self) -> None:
        self.init()
        self.sell_a()
        self.clear_b()
        self.deposits.apply_voucher("A", 12)
        self.deposits.apply_voucher("B", 12)
        self.one_trip()
        shipment = self.deposits.ship(12, "SHP12")
        self.assertEqual(shipment["profit"], 3)
        self.assertEqual(shipment["bottle_count"], 2)
        states = {v["bottle_id"]: v["status"] for v in self.deposits.vouchers()}
        self.assertEqual(states, {"A": "shipped", "B": "shipped"})
        # a shipped voucher can never be reused
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE vouchers SET status = 'open' WHERE bottle_id = 'A'"
            )

    def test_one_voucher_only_one_shipment(self) -> None:
        self.init()
        self.sell_a()
        self.deposits.apply_voucher("A", 12)
        self.one_trip(price=0)
        self.deposits.ship(12, "S1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO shipped_items(box_id, voucher_id, bottle_id, "
                "deposit_paid) VALUES('S1#B0', 'VOUCHER:A', 'A', 4)"
            )

    def test_mixed_kinds_in_one_box(self) -> None:
        self.init()
        self.sell_a()
        # consume C too (kind S), via a hold+consume, so both are eligible
        self.inventory.hold("h", [("C", 1)], 10)
        self.inventory.consume_held("h", 10)
        self.deposits.apply_voucher("A", 12)
        self.deposits.apply_voucher("C", 12)
        self.deposits.add_supplier_kind("SUP", "R")
        self.deposits.add_supplier_kind("SUP", "S")
        self.deposits.add_trip("T", 12, 12, 3)
        self.deposits.add_box_model("MIX", 2, ["R", "S"])
        self.deposits.add_trip_offer("T", "MIX", 5)
        plan = self.deposits.plan_shipment(12)
        self.assertEqual(plan["bottle_count"], 2)
        self.assertEqual(plan["profit"], 3)

    def test_single_kind_supplier_cannot_mix(self) -> None:
        self.init()
        self.sell_a()
        self.inventory.hold("h", [("C", 1)], 10)
        self.inventory.consume_held("h", 10)
        self.deposits.apply_voucher("A", 12)
        self.deposits.apply_voucher("C", 12)
        # supplier only accepts small S bottles, box admits only S
        self.deposits.add_supplier_kind("SUP", "S")
        self.deposits.add_trip("T", 12, 12, 3)
        self.deposits.add_box_model("SMALL", 2, ["S"])
        self.deposits.add_trip_offer("T", "SMALL", 5)
        plan = self.deposits.plan_shipment(12)
        # only C (S) can ship; one S gives 4 - 5 < 0 -> nothing
        self.assertEqual(plan["bottle_count"], 0)

    def test_max_bottles_and_trips_caps(self) -> None:
        self.init()
        self.sell_a()
        self.clear_b()
        self.deposits.apply_voucher("A", 12)
        self.deposits.apply_voucher("B", 12)
        self.one_trip(price=5)
        plan = self.deposits.plan_shipment(12, max_bottles=1)
        self.assertLessEqual(plan["bottle_count"], 1)

    def test_plan_does_not_mutate(self) -> None:
        self.init()
        self.sell_a()
        self.clear_b()
        self.deposits.apply_voucher("A", 12)
        self.deposits.apply_voucher("B", 12)
        self.one_trip()
        self.deposits.plan_shipment(12)
        self.assertEqual(len(self.deposits.vouchers("open")), 2)
        self.assertFalse(
            self.conn.execute("SELECT 1 FROM shipments").fetchone()
        )

    def test_tie_prefers_more_bottles(self) -> None:
        # Both loadings yield profit 0: one bottle in a 1-slot box priced 4, or
        # two bottles in a 2-slot box priced 8.  Same profit -> prefer two.
        self.init()
        self.sell_a()
        self.clear_b()
        self.deposits.apply_voucher("A", 12)
        self.deposits.apply_voucher("B", 12)
        self.deposits.add_supplier_kind("SUP", "R")
        self.deposits.add_trip("T", 12, 12, 3)
        self.deposits.add_box_model("ONE", 1, ["R"])
        self.deposits.add_box_model("TWO", 2, ["R"])
        self.deposits.add_trip_offer("T", "ONE", 4)
        self.deposits.add_trip_offer("T", "TWO", 8)
        plan = self.deposits.plan_shipment(12)
        self.assertEqual(plan["profit"], 0)
        self.assertEqual(plan["bottle_count"], 2)


class DepositOperationFileTests(DepositTestCase):
    def test_deposit_operation_file_end_to_end(self) -> None:
        from reagent.cli import execute_operations

        document = json.loads(
            (MATERIALS / "deposit_inventory.json").read_text(encoding="utf-8")
        )
        operations = json.loads(
            (MATERIALS / "deposit_operations.json").read_text(encoding="utf-8")
        )
        self.inventory.initialize(document)
        results = execute_operations(self.conn, operations)
        plans = [r["result"] for r in results if r["op"] == "plan"]
        self.assertEqual(len(plans), 2)
        self.assertEqual(plans[0]["bottle_count"], 0)
        self.assertEqual(plans[1]["bottle_count"], 2)
        self.assertEqual(plans[1]["profit"], 3)
        shipments = [r["result"] for r in results if r["op"] == "ship"]
        self.assertEqual(shipments[0]["profit"], 3)
        rows = {r["bottle_id"]: r for r in self.inventory.conservation()}
        for row in rows.values():
            self.assertEqual(
                row["total_portions"],
                row["unallocated"]
                + row["reserved"]
                + row["consumed"]
                + row["cleared"],
            )

    def test_demo_script_runs_and_conserves(self) -> None:
        import subprocess
        import sys

        out = os.path.join(self._tmp.name, "results.json")
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "demo_deposit.py"),
                "--out",
                out,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        data = json.loads(Path(out).read_text(encoding="utf-8"))
        self.assertTrue(data["conservation_ok"])
        self.assertEqual(data["final_shipment"]["profit"], 3)
        self.assertEqual(data["initial_plan_one_voucher"]["bottle_count"], 0)
        self.assertEqual(data["branches"]["late_trip"]["bottle_count"], 0)
        self.assertEqual(
            data["branches"]["different_versions"]["status"], "pending"
        )
        self.assertEqual(
            data["branches"]["old_holder_at_expiry"]["voucher"], "blocked"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
