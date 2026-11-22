"""Tests for whole-bottle seals (封单) and offline co-signing (会签)."""

from __future__ import annotations

import os
import tempfile
import unittest

from reagent import connect, initialize
from reagent import cosign
from reagent.errors import Conflict, NotFoundError, ValidationError
from reagent.inventory import InventoryService
from reagent.runs import RunService
from reagent.seals import SealService


# ---------------------------------------------------------------------------
# pure planner
# ---------------------------------------------------------------------------
class PlannerExampleTests(unittest.TestCase):
    def test_three_shifts_force_all_three_groups_fee_three(self) -> None:
        groups = cosign.parse_groups(
            [
                {"name": "AB", "seats": ["A", "B"], "fee": 1},
                {"name": "AC", "seats": ["A", "C"], "fee": 1},
                {"name": "BC", "seats": ["B", "C"], "fee": 1},
            ]
        )
        shifts = cosign.parse_shifts(
            [
                {"shift_id": "s1", "on_duty": ["A", "B"]},
                {"shift_id": "s2", "on_duty": ["A", "C"]},
                {"shift_id": "s3", "on_duty": ["B", "C"]},
            ]
        )
        plan = cosign.plan_scheme(
            groups, shifts, ["JIA"], {"JIA": ["A", "B"]}, ["A", "B", "C"]
        )
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.activated, ["AB", "AC", "BC"])
        self.assertEqual(plan.total_fee, 3)
        self.assertEqual(plan.per_shift["s1"], ["AB"])
        self.assertEqual(plan.per_shift["s2"], ["AC"])
        self.assertEqual(plan.per_shift["s3"], ["BC"])

    def test_second_run_only_c_witness_makes_ab_ineligible(self) -> None:
        groups = cosign.parse_groups(
            [
                {"name": "AB", "seats": ["A", "B"], "fee": 1},
                {"name": "AC", "seats": ["A", "C"], "fee": 1},
                {"name": "BC", "seats": ["B", "C"], "fee": 1},
            ]
        )
        shifts = cosign.parse_shifts(
            [
                {"shift_id": "s2", "on_duty": ["A", "C"]},
                {"shift_id": "s3", "on_duty": ["B", "C"]},
            ]
        )
        witnesses = {"JIA": ["A", "B"], "YI": ["C"]}
        plan = cosign.plan_scheme(
            groups, shifts, ["JIA", "YI"], witnesses, ["A", "B", "C"]
        )
        self.assertTrue(plan.feasible)
        self.assertIn("AB", plan.ineligible)
        self.assertEqual(plan.ineligible["AB"], ["YI"])
        self.assertEqual(plan.activated, ["AC", "BC"])
        self.assertEqual(plan.total_fee, 2)

    def test_effective_historical_group_with_only_a_blocks_bc(self) -> None:
        groups = cosign.parse_groups(
            [
                {"name": "AC", "seats": ["A", "C"], "fee": 1},
                {"name": "BC", "seats": ["B", "C"], "fee": 1},
            ]
        )
        shifts = cosign.parse_shifts(
            [
                {"shift_id": "s2", "on_duty": ["A", "C"]},
                {"shift_id": "s3", "on_duty": ["B", "C"]},
            ]
        )
        witnesses = {"JIA": ["A", "B"], "YI": ["C"]}
        historical = [cosign.HistoricalGroup("oldA", frozenset({"A"}))]
        blocked = cosign.plan_scheme(
            groups, shifts, ["JIA", "YI"], witnesses, ["A", "B", "C"], historical
        )
        self.assertFalse(blocked.feasible)
        self.assertIn("BC", blocked.blocked)
        self.assertIn("oldA", blocked.blocked["BC"])

        cleared = cosign.plan_scheme(
            groups, shifts, ["JIA", "YI"], witnesses, ["A", "B", "C"], []
        )
        self.assertTrue(cleared.feasible)
        self.assertEqual(cleared.activated, ["AC", "BC"])

    def test_deputy_only_intersection_is_infeasible(self) -> None:
        groups = cosign.parse_groups(
            [
                {"name": "G1", "seats": ["A", "D"], "fee": 1},
                {"name": "G2", "seats": ["B", "D"], "fee": 1},
            ]
        )
        shifts = cosign.parse_shifts(
            [
                {"shift_id": "w1", "on_duty": ["A", "D"]},
                {"shift_id": "w2", "on_duty": ["B", "D"]},
            ]
        )
        plan = cosign.plan_scheme(
            groups, shifts, ["JIA"], {"JIA": ["A", "B"]}, ["A", "B"]
        )
        self.assertFalse(plan.feasible)
        self.assertTrue(plan.conflicts)
        self.assertIn("deputy", plan.reason)

    def test_group_without_ordinary_seat_is_self_blocked(self) -> None:
        groups = cosign.parse_groups([{"name": "DD", "seats": ["D", "E"], "fee": 1}])
        shifts = cosign.parse_shifts([{"shift_id": "w", "on_duty": ["D", "E"]}])
        plan = cosign.plan_scheme(
            groups, shifts, ["JIA"], {"JIA": ["D"]}, ["A", "B"]
        )
        self.assertFalse(plan.feasible)
        self.assertIn("DD", plan.blocked)

    def test_minimum_fee_prefers_cheaper_cover(self) -> None:
        groups = cosign.parse_groups(
            [
                {"name": "AB", "seats": ["A", "B"], "fee": 5},
                {"name": "AC", "seats": ["A", "C"], "fee": 1},
                {"name": "BC", "seats": ["B", "C"], "fee": 1},
            ]
        )
        shifts = cosign.parse_shifts(
            [
                {"shift_id": "s1", "on_duty": ["A", "B"]},
                {"shift_id": "s2", "on_duty": ["A", "C"]},
            ]
        )
        # s2 needs AC; s1 needs AB (only group inside s1) -> total 6
        plan = cosign.plan_scheme(
            groups, shifts, ["JIA"], {"JIA": ["A"]}, ["A", "B", "C"]
        )
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.activated, ["AB", "AC"])
        self.assertEqual(plan.total_fee, 6)

    def test_recognition_violations_reports_deputy_pairs(self) -> None:
        groups = cosign.parse_groups(
            [
                {"name": "G1", "seats": ["A", "D"], "fee": 1},
                {"name": "G2", "seats": ["B", "D"], "fee": 1},
            ]
        )
        violations = cosign.recognition_violations(
            groups, frozenset({"A", "B"}),
            [cosign.HistoricalGroup("old", frozenset({"C"}))],
        )
        # G1/G2 deputy pair, plus each lacks an ordinary seat with old{C}
        self.assertEqual(len(violations), 3)

    def test_peak_season_scale_dozens_of_groups_hundreds_of_shifts(self) -> None:
        import random
        import time

        random.seed(7)
        deputies = [f"d{i}" for i in range(10)]
        groups = []
        for i in range(40):
            extra = random.sample(deputies, random.choice([1, 2, 3]))
            groups.append(
                {"name": f"g{i}", "seats": ["A"] + extra, "fee": 1 + i % 4}
            )
        raw_shifts = []
        for i in range(250):
            chosen = random.sample(groups, random.choice([1, 2]))
            on = set()
            for g in chosen:
                on |= set(g["seats"])
            raw_shifts.append({"shift_id": f"t{i}", "on_duty": sorted(on)})
        parsed_groups = cosign.parse_groups(groups)
        parsed_shifts = cosign.parse_shifts(raw_shifts)
        start = time.time()
        plan = cosign.plan_scheme(
            parsed_groups, parsed_shifts, ["r"], {"r": ["A"]}, ["A"]
        )
        elapsed = time.time() - start
        self.assertTrue(plan.feasible)
        # every shift is covered by at least one activated group
        for names in plan.per_shift.values():
            self.assertTrue(names)
        self.assertLess(elapsed, 5.0)


# ---------------------------------------------------------------------------
# DB-backed seals
# ---------------------------------------------------------------------------
def _document() -> dict:
    return {
        "initial_day": 1,
        "kinds": [{"kind": "R", "open_lifetime_days": 100}],
        "bottles": [
            {
                "bottle_id": "X",
                "kind": "R",
                "total_portions": 2,
                "intake_day": 1,
                "printed_expiry_day": 500,
                "opened_day": None,
            },
            {
                "bottle_id": "Y",
                "kind": "R",
                "total_portions": 1,
                "intake_day": 1,
                "printed_expiry_day": 500,
                "opened_day": None,
            },
        ],
        "methods": [
            {"method_id": "M", "revision": 1, "requirements": {"R": 1}},
            {"method_id": "M2", "revision": 1, "requirements": {"R": 2}},
        ],
    }


class SealTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "store.db")
        self.conn = connect(self.db_path)
        initialize(self.conn)
        self.inventory = InventoryService(self.conn)
        self.runs = RunService(self.conn)
        self.seals = SealService(self.conn)
        self.inventory.initialize(_document())
        self.seals.register_seats(
            [
                {"seat_id": "A", "seat_type": "ordinary"},
                {"seat_id": "B", "seat_type": "ordinary"},
                {"seat_id": "C", "seat_type": "ordinary"},
                {"seat_id": "D", "seat_type": "deputy"},
            ],
            day=1,
        )

    def tearDown(self) -> None:
        try:
            self.conn.close()
        finally:
            self._tmp.cleanup()

    def make_shared_runs(self) -> None:
        # r1 spans X and Y; r2 shares X -> closure {r1, r2}, bottles X, Y
        self.runs.create_run(
            "r1",
            "M2",
            1,
            1,
            [
                {"bottle_id": "X", "portions": 1},
                {"bottle_id": "Y", "portions": 1},
            ],
        )
        self.runs.create_run(
            "r2", "M", 1, 1, [{"bottle_id": "X", "portions": 1}]
        )


class SealManifestTests(SealTestCase):
    def test_closure_follows_shared_bottle_and_spanning_run(self) -> None:
        self.make_shared_runs()
        seal = self.seals.create_seal(
            "S1", ["r1"], {"r1": ["A", "B"], "r2": ["C"]}, day=1
        )
        self.assertEqual(seal["closure_runs"], ["r1", "r2"])
        self.assertEqual(sorted(seal["bottles"]), ["X", "Y"])
        self.assertEqual(
            seal["report"]["runs"]["r2"]["reason"],
            "shares bottle X with run r1",
        )
        self.assertEqual(
            seal["report"]["runs"]["r1"]["reason"],
            "designated by the waybill",
        )
        # bottle X holds r1's and r2's active portions
        xruns = {r["run_id"]: r["portions"] for r in seal["bottles"]["X"]["runs"]}
        self.assertEqual(xruns, {"r1": 1, "r2": 1})

    def test_sealing_does_not_deduct_inventory(self) -> None:
        self.make_shared_runs()
        before = {b: self.inventory.query(b, 1) for b in ("X", "Y")}
        self.seals.create_seal(
            "S1", ["r1"], {"r1": ["A", "B"], "r2": ["C"]}, day=1
        )
        after = {b: self.inventory.query(b, 1) for b in ("X", "Y")}
        self.assertEqual(before, after)

    def test_imperfect_manifest_reports_issues(self) -> None:
        # add an outside ordinary holder and leave a portion unallocated on Y
        self.runs.create_run(
            "r1", "M2", 1, 1,
            [
                {"bottle_id": "X", "portions": 1},
                {"bottle_id": "Y", "portions": 1},
            ],
        )
        # Y total 1 fully reserved by r1; give the manifest a problem by
        # reserving the spare X portion for an ordinary holder
        self.inventory.hold("loose", [("X", 1)], 1)
        seal = self.seals.create_seal(
            "S1", ["r1"], {"r1": ["A", "B"]}, day=1
        )
        issues = seal["report"]["issues"]
        self.assertFalse(seal["clean"])
        self.assertTrue(any(i["bottle_id"] == "X" for i in issues["ordinary_holders"]))
        # X now holds r1(1) + loose(1) = total 2, so no unallocated, but the
        # ordinary holder is called out.
        self.assertEqual(
            [i["bottle_id"] for i in issues["ordinary_holders"]], ["X"]
        )

    def test_unallocated_and_invalid_bottles_reported(self) -> None:
        # reserve only one of X's two portions -> closure is just this run,
        # X has 1 unallocated and Y is not in the closure.
        self.runs.create_run(
            "r1", "M", 1, 1, [{"bottle_id": "X", "portions": 1}]
        )
        seal = self.seals.create_seal("S1", ["r1"], {"r1": ["A"]}, day=1)
        issues = seal["report"]["issues"]
        self.assertEqual(issues["unallocated"], [{"bottle_id": "X", "unallocated": 1}])

    def test_expired_bottle_in_closure_is_reported(self) -> None:
        # P opened day 2 with L=2 -> effective expiry day 4; sealed on day 4.
        doc = {
            "initial_day": 2,
            "kinds": [{"kind": "R", "open_lifetime_days": 2}],
            "bottles": [
                {
                    "bottle_id": "P",
                    "kind": "R",
                    "total_portions": 1,
                    "intake_day": 1,
                    "printed_expiry_day": 50,
                    "opened_day": 2,
                }
            ],
            "methods": [
                {"method_id": "M", "revision": 1, "requirements": {"R": 1}}
            ],
        }
        self._tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp2.cleanup)
        path = os.path.join(self._tmp2.name, "exp.db")
        conn = connect(path)
        self.addCleanup(conn.close)
        initialize(conn)
        inv = InventoryService(conn)
        runs = RunService(conn)
        seals = SealService(conn)
        inv.initialize(doc)
        runs.create_run("r1", "M", 1, 2, [{"bottle_id": "P", "portions": 1}])
        seals.register_seats([{"seat_id": "A", "seat_type": "ordinary"}], day=2)
        seal = seals.create_seal("S1", ["r1"], {"r1": ["A"]}, day=4)
        invalid = seal["report"]["issues"]["invalid_bottles"]
        self.assertEqual(
            invalid,
            [{"bottle_id": "P", "validity": "expired",
              "effective_expiry_day": 4}],
        )
        self.assertFalse(seal["clean"])

    def test_missing_witnesses_rejected(self) -> None:
        self.make_shared_runs()
        with self.assertRaises(ValidationError):
            self.seals.create_seal("S1", ["r1"], {"r1": ["A", "B"]}, day=1)

    def test_run_without_active_reservations_cannot_be_sealed(self) -> None:
        self.runs.create_run("r1", "M", 1, 1, [{"bottle_id": "X", "portions": 1}])
        self.runs.release_run("r1", 1, "cancel")
        with self.assertRaises(ValidationError):
            self.seals.create_seal("S1", ["r1"], {"r1": ["A"]}, day=1)

    def test_duplicate_seal_id_rejected(self) -> None:
        self.runs.create_run("r1", "M", 1, 1, [{"bottle_id": "X", "portions": 1}])
        self.seals.create_seal("S1", ["r1"], {"r1": ["A"]}, day=1)
        with self.assertRaises(Conflict):
            self.seals.create_seal("S1", ["r1"], {"r1": ["A"]}, day=1)

    def test_unknown_run_rejected(self) -> None:
        with self.assertRaises(NotFoundError):
            self.seals.create_seal("S1", ["ghost"], {"ghost": ["A"]}, day=1)

    def test_closure_is_transitive_across_bottles(self) -> None:
        # extra bottles Z(2), W(1): r1 -> X -> r2b -> Z -> r3 chain
        for bid, total in (("Z", 2), ("W", 1)):
            self.conn.execute(
                "INSERT INTO bottles(bottle_id, kind, total_portions, intake_day, "
                "printed_expiry_day, opened_day) VALUES(?, 'R', ?, 1, 500, NULL)",
                (bid, total),
            )
        self.runs.create_run(
            "r1", "M2", 1, 1,
            [{"bottle_id": "X", "portions": 1}, {"bottle_id": "Y", "portions": 1}],
        )
        self.runs.create_run(
            "r2b", "M2", 1, 1,
            [{"bottle_id": "X", "portions": 1}, {"bottle_id": "Z", "portions": 1}],
        )
        self.runs.create_run(
            "r3", "M2", 1, 1,
            [{"bottle_id": "Z", "portions": 1}, {"bottle_id": "W", "portions": 1}],
        )
        seal = self.seals.create_seal(
            "S1",
            ["r1"],
            {"r1": ["A"], "r2b": ["B"], "r3": ["C"]},
            day=1,
        )
        self.assertEqual(seal["closure_runs"], ["r1", "r2b", "r3"])
        self.assertEqual(set(seal["bottles"]), {"X", "Y", "Z", "W"})
        self.assertEqual(
            seal["report"]["runs"]["r2b"]["reason"], "shares bottle X with run r1"
        )
        self.assertEqual(
            seal["report"]["runs"]["r3"]["reason"], "shares bottle Z with run r2b"
        )


class SealSchemeTests(SealTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_shared_runs()
        # designated r1; closure adds r2. r2 is witnessed only by C, so any
        # group needing B cannot serve.
        self.seal = self.seals.create_seal(
            "S1", ["r1"], {"r1": ["A", "B"], "r2": ["C"]}, day=1
        )
        self.groups = [
            {"name": "AB", "seats": ["A", "B"], "fee": 1},
            {"name": "AC", "seats": ["A", "C"], "fee": 1},
            {"name": "BC", "seats": ["B", "C"], "fee": 1},
        ]
        self.shifts = [
            {"shift_id": "s2", "on_duty": ["A", "C"]},
            {"shift_id": "s3", "on_duty": ["B", "C"]},
        ]

    def test_scheme_reports_ineligible_ab_and_fee(self) -> None:
        scheme = self.seals.create_scheme(
            "SCH1", "S1", self.groups, self.shifts, [], day=1
        )
        self.assertTrue(scheme["feasible"])
        self.assertEqual(scheme["total_fee"], 2)
        self.assertEqual(scheme["plan"]["activated_groups"], ["AC", "BC"])
        self.assertEqual(scheme["plan"]["ineligible_groups"], {"AB": ["r2"]})
        self.assertEqual(scheme["plan"]["per_shift"]["s2"], ["AC"])
        self.assertEqual(scheme["plan"]["per_shift"]["s3"], ["BC"])

    def test_historical_block_then_recall_allows_scheme(self) -> None:
        self.seals.register_historical_group("oldA", ["A"], 1)
        blocked = self.seals.create_scheme(
            "SCH1", "S1", self.groups, self.shifts, [], day=1
        )
        self.assertFalse(blocked["feasible"])
        self.assertIn("BC", blocked["plan"]["blocked_groups"])

        self.seals.recall_historical_group("oldA", 2)
        allowed = self.seals.create_scheme(
            "SCH2", "S1", self.groups, self.shifts, [], day=2
        )
        self.assertTrue(allowed["feasible"])
        self.assertEqual(allowed["plan"]["activated_groups"], ["AC", "BC"])

    def test_deputy_counterexample_infeasible(self) -> None:
        self.seals.register_seats(
            [{"seat_id": "P", "seat_type": "ordinary"},
             {"seat_id": "Q", "seat_type": "ordinary"}],
            day=1,
        )
        # both runs are witnessed by the deputy D, so G1 and G2 both cover
        # them; each also has an ordinary seat for the self-rule, but the
        # pair shares only the deputy -> infeasible.
        seal = self.seals.create_seal(
            "S2", ["r1"], {"r1": ["P", "Q", "D"], "r2": ["D"]}, day=1
        )
        groups = [
            {"name": "G1", "seats": ["P", "D"], "fee": 1},
            {"name": "G2", "seats": ["Q", "D"], "fee": 1},
        ]
        shifts = [
            {"shift_id": "w1", "on_duty": ["P", "D"]},
            {"shift_id": "w2", "on_duty": ["Q", "D"]},
        ]
        scheme = self.seals.create_scheme(
            "SCHX", "S2", groups, shifts, [], day=1
        )
        self.assertFalse(scheme["feasible"])
        self.assertTrue(scheme["plan"]["conflicts"])

    def test_scheme_unknown_seal(self) -> None:
        with self.assertRaises(NotFoundError):
            self.seals.create_scheme(
                "SCHX", "NOPE", self.groups, self.shifts, [], day=1
            )

    def test_duplicate_scheme_id_rejected(self) -> None:
        self.seals.create_scheme("SCH1", "S1", self.groups, self.shifts, [], day=1)
        with self.assertRaises(Conflict):
            self.seals.create_scheme(
                "SCH1", "S1", self.groups, self.shifts, [], day=1
            )


class VoucherAndPostingTests(SealTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_shared_runs()
        self.seals.create_seal(
            "S1", ["r1"], {"r1": ["A", "B"], "r2": ["C"]}, day=1
        )
        self.groups = [
            {"name": "AC", "seats": ["A", "C"], "fee": 1},
            {"name": "BC", "seats": ["B", "C"], "fee": 1},
        ]
        self.shifts = [
            {"shift_id": "s2", "on_duty": ["A", "C"]},
            {"shift_id": "s3", "on_duty": ["B", "C"]},
        ]
        self.seals.create_scheme("SCH1", "S1", self.groups, self.shifts, [], day=1)

    def test_verify_accepts_full_group_and_rejects_partial(self) -> None:
        ok = self.seals.verify_voucher("S1", "SCH1", "s2", "T1", ["A", "C"])
        self.assertTrue(ok["accepted"])
        self.assertEqual(ok["qualifying_groups"], ["AC"])
        bad = self.seals.verify_voucher("S1", "SCH1", "s2", "T1", ["A"])
        self.assertFalse(bad["accepted"])

    def test_verify_rejects_group_not_on_duty(self) -> None:
        # AC is not on duty in shift s3, though its signers exist
        receipt = self.seals.verify_voucher("S1", "SCH1", "s3", "T1", ["A", "C"])
        self.assertFalse(receipt["accepted"])

    def test_posting_dedupes_same_settlement_number(self) -> None:
        first = self.seals.post_settlement(
            "S1", "SCH1", "s2", "T1", ["A", "C"], day=1
        )
        self.assertTrue(first["posted"])
        self.assertFalse(first["replayed"])
        again = self.seals.post_settlement(
            "S1", "SCH1", "s2", "T1", ["A", "C"], day=2
        )
        self.assertTrue(again["posted"])
        self.assertTrue(again["replayed"])
        self.assertEqual(self.seals.get_posting("S1")["settlement_no"], "T1")

    def test_posting_rejects_second_distinct_settlement(self) -> None:
        self.seals.post_settlement("S1", "SCH1", "s2", "T1", ["A", "C"], day=1)
        with self.assertRaises(Conflict):
            self.seals.post_settlement(
                "S1", "SCH1", "s3", "T2", ["B", "C"], day=1
            )

    def test_posting_refused_after_run_started(self) -> None:
        self.runs.start_run("r1", 2)
        result = self.seals.post_settlement(
            "S1", "SCH1", "s2", "T1", ["A", "C"], day=2
        )
        self.assertFalse(result["posted"])
        self.assertIn("no longer 'reserved'", result["reason"])
        self.assertIn("started", result["reason"])

    def test_posting_refused_after_reservation_released_and_reheld(self) -> None:
        # release r2 and re-hold the same amount -> fresh reservation ids
        self.runs.release_run("r2", 2, "abort")
        self.runs.create_run("r2b", "M", 1, 2, [{"bottle_id": "X", "portions": 1}])
        self.assertTrue(self.seals.seal_staleness("S1")["stale"])
        result = self.seals.post_settlement(
            "S1", "SCH1", "s2", "T1", ["A", "C"], day=2
        )
        self.assertFalse(result["posted"])

    def test_staleness_false_for_untouched_seal(self) -> None:
        self.assertFalse(self.seals.seal_staleness("S1")["stale"])

    def test_unknown_shift_rejected(self) -> None:
        receipt = self.seals.verify_voucher("S1", "SCH1", "nope", "T1", ["A", "C"])
        self.assertFalse(receipt["accepted"])
        self.assertIn("unknown shift", receipt["reason"])

    def test_replay_survives_later_run_start(self) -> None:
        first = self.seals.post_settlement(
            "S1", "SCH1", "s2", "T1", ["A", "C"], day=1
        )
        self.assertTrue(first["posted"])
        # once posted, changing later state must not lose the idempotent replay
        self.runs.start_run("r2", 2)
        again = self.seals.post_settlement(
            "S1", "SCH1", "s2", "T1", ["A", "C"], day=2
        )
        self.assertTrue(again["replayed"])
        self.assertEqual(again["posting_id"], first["posting_id"])

    def test_scheme_from_other_seal_rejected(self) -> None:
        receipt = self.seals.verify_voucher("S1", "SCH1", "s2", "T1", ["A", "C"])
        self.assertTrue(receipt["accepted"])
        # a different seal id must not accept a foreign scheme
        self.seals.create_seal(
            "S9", ["r1"], {"r1": ["A", "B"], "r2": ["C"]}, day=1
        )
        mismatch = self.seals.verify_voucher("S9", "SCH1", "s2", "T1", ["A", "C"])
        self.assertFalse(mismatch["accepted"])
        self.assertIn("does not belong", mismatch["reason"])


class SeatAndHistoricalTests(SealTestCase):
    def test_seat_type_is_immutable(self) -> None:
        with self.assertRaises(Conflict):
            self.seals.register_seats(
                [{"seat_id": "A", "seat_type": "deputy"}], day=1
            )

    def test_historical_group_seats_frozen(self) -> None:
        self.seals.register_historical_group("old", ["A", "B"], 1)
        # re-registering with the same seats is idempotent
        self.assertEqual(
            self.seals.register_historical_group("old", ["B", "A"], 1)["seats"],
            ["A", "B"],
        )
        with self.assertRaises(Conflict):
            self.seals.register_historical_group("old", ["A"], 1)

    def test_recall_unknown_group(self) -> None:
        with self.assertRaises(NotFoundError):
            self.seals.recall_historical_group("ghost", 1)

    def test_missing_witness_seat_registration_rejected_at_scheme(self) -> None:
        self.make_shared_runs()
        self.seals.create_seal(
            "S1", ["r1"], {"r1": ["A", "Z"], "r2": ["C"]}, day=1
        )
        groups = [{"name": "AC", "seats": ["A", "C"], "fee": 1}]
        shifts = [{"shift_id": "s2", "on_duty": ["A", "C"]}]
        with self.assertRaises(ValidationError):
            self.seals.create_scheme(
                "SCH1", "S1", groups, shifts, [], day=1
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
