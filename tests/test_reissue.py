"""Tests for re-issue (改签) consent analysis and registrar batch scheduling.

Run with:  python3 -m unittest tests.test_reissue -v
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gradaudit import (
    Package,
    Store,
    parse_catalog,
    parse_plan,
    parse_records,
    parse_requirements,
    reissue_options,
    schedule_batch,
    schedule_packages,
    verify_plan,
)
from gradaudit.errors import ConfirmationError, SchemaError
from gradaudit.resign import ReissueResult


# --------------------------------------------------------------------------- #
# fixtures: the prompt's Aqua/Beta example
# --------------------------------------------------------------------------- #
def catalog_new():
    # Old course P/Q/R each may go to G or H.
    return parse_catalog({
        "format": "course-catalog/1", "revision": 2,
        "courses": [
            {"course_id": "CG", "categories": ["G"]},
            {"course_id": "CH", "categories": ["H"]},
        ],
        "equivalences": {"P": ["CG", "CH"], "Q": ["CG", "CH"], "R": ["CG", "CH"]},
    })


def catalog_q_no_h():
    # Same as new, but Q's H mapping is withdrawn (Q -> G only).
    return parse_catalog({
        "format": "course-catalog/1", "revision": 3,
        "courses": [
            {"course_id": "CG", "categories": ["G"]},
            {"course_id": "CH", "categories": ["H"]},
        ],
        "equivalences": {"P": ["CG", "CH"], "Q": ["CG"], "R": ["CG", "CH"]},
    })


def reqs(rid="RQ"):
    return parse_requirements({
        "request_id": rid, "revision": 2,
        "groups": [
            {"group_id": "G", "allowed_categories": ["G"], "minimum_points": 3},
            {"group_id": "H", "allowed_categories": ["H"], "minimum_points": 3},
        ],
    })


def records_a():
    return parse_records({"student_id": "A", "revision": 1, "records": [
        {"record_id": "A_M", "external_course": "P", "points": 3},
        {"record_id": "A_L", "external_course": "Q", "points": 3},
    ]})


def records_b():
    return parse_records({"student_id": "B", "revision": 1, "records": [
        {"record_id": "B_L", "external_course": "Q", "points": 3},
        {"record_id": "B_Z", "external_course": "R", "points": 3},
    ]})


def old_plan_a():
    # Old confirmation: both records used, both in G (2 x 3 = 6, legal overfill).
    return parse_plan({
        "format": "course-allocation/1", "student_id": "A",
        "catalog_revision": 1, "records_revision": 1,
        "request_id": "RQ", "requirements_revision": 1,
        "assignments": [
            {"record_id": "A_M", "course_id": "CG", "group_id": "G"},
            {"record_id": "A_L", "course_id": "CG", "group_id": "G"},
        ],
    })


def old_plan_b():
    return parse_plan({
        "format": "course-allocation/1", "student_id": "B",
        "catalog_revision": 1, "records_revision": 1,
        "request_id": "RQ", "requirements_revision": 1,
        "assignments": [
            {"record_id": "B_L", "course_id": "CG", "group_id": "G"},
            {"record_id": "B_Z", "course_id": "CG", "group_id": "G"},
        ],
    })


ADV_A = {"A_M": "Mei", "A_L": "Lan"}
ADV_B = {"B_L": "Lan", "B_Z": "Zhu"}
PRICE = {"Mei": 2, "Lan": 3, "Zhu": 2}


# =========================================================================== #
# re-issue consent sets
# =========================================================================== #
class TestReissue(unittest.TestCase):
    def test_minimal_singletons_both_need_one_change(self):
        ra = reissue_options(old_plan_a(), catalog_new(), records_a(), reqs(), ADV_A)
        rb = reissue_options(old_plan_b(), catalog_new(), records_b(), reqs(), ADV_B)
        self.assertTrue(ra.full_consent_feasible)
        self.assertTrue(rb.full_consent_feasible)
        self.assertEqual({frozenset(o.advisors) for o in ra.options},
                         {frozenset({"Mei"}), frozenset({"Lan"})})
        self.assertEqual({frozenset(o.advisors) for o in rb.options},
                         {frozenset({"Lan"}), frozenset({"Zhu"})})

    def test_each_option_plan_verifies_under_new_revisions(self):
        for old, recs, adv in ((old_plan_a(), records_a(), ADV_A),
                               (old_plan_b(), records_b(), ADV_B)):
            res = reissue_options(old, catalog_new(), recs, reqs(), adv)
            for opt in res.options:
                v = verify_plan(opt.plan, catalog_new(), recs, reqs())
                self.assertTrue(v.accepted, v.reasons)

    def test_unconsented_old_destinations_preserved(self):
        # With only Mei consenting, A_L must keep its old destination (CG,G);
        # A_M moves to H.  Check the actual assignment.
        ra = reissue_options(old_plan_a(), catalog_new(), records_a(), reqs(), ADV_A)
        by_set = {frozenset(o.advisors): o for o in ra.options}
        plan_mei = by_set[frozenset({"Mei"})].plan
        dest = {a.record_id: (a.course_id, a.group_id) for a in plan_mei.assignments}
        self.assertEqual(dest["A_L"], ("CG", "G"))  # Lan did not consent: unchanged
        self.assertEqual(dest["A_M"], ("CH", "H"))  # Mei consented: remapped

    def test_removing_q_to_h_forces_zhu(self):
        # Q's H mapping withdrawn -> Beta's Lan-record can no longer move to H,
        # so Lan cannot be the single consenting advisor; Beta needs Zhu.
        rb = reissue_options(old_plan_b(), catalog_q_no_h(), records_b(), reqs(), ADV_B)
        self.assertEqual({frozenset(o.advisors) for o in rb.options}, {frozenset({"Zhu"})})
        # Alpha is unaffected (its Q record is A_L, advisor Lan; but the
        # alternative Mei still works).
        ra = reissue_options(old_plan_a(), catalog_q_no_h(), records_a(), reqs(), ADV_A)
        self.assertEqual({frozenset(o.advisors) for o in ra.options}, {frozenset({"Mei"})})

    def test_no_full_consent_reports_infeasible(self):
        # New requirements demand 6 G and 3 H but only 6 total points exist,
        # and both old records are stuck in G with no H-capable candidate.
        cat = parse_catalog({
            "format": "course-catalog/1", "revision": 9,
            "courses": [{"course_id": "CG", "categories": ["G"]}],
            "equivalences": {"P": ["CG"], "Q": ["CG"]},
        })
        hard = parse_requirements({"request_id": "RQ", "revision": 9, "groups": [
            {"group_id": "G", "allowed_categories": ["G"], "minimum_points": 6},
            {"group_id": "H", "allowed_categories": ["H"], "minimum_points": 3},
        ]})
        recs = records_a()
        res = reissue_options(
            parse_plan({
                "format": "course-allocation/1", "student_id": "A",
                "catalog_revision": 1, "records_revision": 1,
                "request_id": "RQ", "requirements_revision": 1,
                "assignments": [
                    {"record_id": "A_M", "course_id": "CG", "group_id": "G"},
                    {"record_id": "A_L", "course_id": "CG", "group_id": "G"},
                ],
            }),
            cat, recs, hard, ADV_A,
        )
        self.assertFalse(res.options)
        self.assertFalse(res.full_consent_feasible)
        self.assertIn("even with every responsible advisor consenting", res.note)

    def test_repeated_advisor_listed_once(self):
        # Both records have the same advisor; a set is set-valued.
        adv = {"A_M": "Mei", "A_L": "Mei"}
        res = reissue_options(old_plan_a(), catalog_new(), records_a(), reqs(), adv)
        self.assertEqual({frozenset(o.advisors) for o in res.options}, {frozenset({"Mei"})})
        self.assertEqual(res.all_advisors, ["Mei"])

    def test_unused_records_need_no_consent(self):
        # A third, never-used record can be activated freely with no advisor.
        recs = parse_records({"student_id": "A", "revision": 1, "records": [
            {"record_id": "A_M", "external_course": "P", "points": 3},
            {"record_id": "A_L", "external_course": "Q", "points": 3},
            {"record_id": "A_FREE", "external_course": "R", "points": 3},
        ]})
        adv = {"A_M": "Mei", "A_L": "Lan"}  # no entry for A_FREE
        res = reissue_options(old_plan_a(), catalog_new(), recs, reqs(), adv)
        # A_FREE only helps if it can be used without consent; feasibility must
        # not require an advisor for it.
        self.assertTrue(res.options)
        for opt in res.options:
            self.assertTrue(frozenset(opt.advisors).issubset({"Mei", "Lan"}))

    def test_used_record_missing_advisor_is_schema_error(self):
        with self.assertRaises(SchemaError):
            reissue_options(old_plan_a(), catalog_new(), records_a(), reqs(), {"A_M": "Mei"})

    def test_identity_must_be_preserved(self):
        other = records_b()
        with self.assertRaises(SchemaError):
            reissue_options(old_plan_a(), catalog_new(), other, reqs(), ADV_A)

    def test_options_are_inclusion_minimal(self):
        # No returned set may contain another returned set; and no proper
        # subset of a returned set is feasible.
        ra = reissue_options(old_plan_a(), catalog_new(), records_a(), reqs(), ADV_A)
        sets = [frozenset(o.advisors) for o in ra.options]
        for i, s in enumerate(sets):
            for j, t in enumerate(sets):
                if i != j:
                    self.assertFalse(s < t, f"{s} is a proper subset of {t}")
        for o in ra.options:
            for drop in o.advisors:
                subset = frozenset(o.advisors) - {drop}
                fixed = {rid: (a.course_id, a.group_id)
                         for rid, a in {x.record_id: x for x in old_plan_a().assignments}.items()
                         if ADV_A[rid] not in subset}
                from gradaudit.resign import _constrain_plan
                self.assertIsNone(_constrain_plan(catalog_new(), records_a(), reqs(), fixed))

    def test_forces_advisor_when_old_destination_illegal(self):
        # Catalog revision that keeps G but drops P's G mapping (P -> H only).
        cat = parse_catalog({
            "format": "course-catalog/1", "revision": 4,
            "courses": [
                {"course_id": "CG", "categories": ["G"]},
                {"course_id": "CH", "categories": ["H"]},
            ],
            "equivalences": {"P": ["CH"], "Q": ["CG", "CH"]},
        })
        res = reissue_options(old_plan_a(), cat, records_a(), reqs(), ADV_A)
        # A_M's old (CG,G) is now illegal, so Mei must sign in every option.
        self.assertTrue(res.options)
        for opt in res.options:
            self.assertIn("Mei", opt.advisors)
        # And no plan keeps A_M at CG.
        for opt in res.options:
            a_m = next(a for a in opt.plan.assignments if a.record_id == "A_M")
            self.assertNotEqual((a_m.course_id, a_m.group_id), ("CG", "G"))


# =========================================================================== #
# batch scheduling
# =========================================================================== #
class TestBatch(unittest.TestCase):
    def _reissues(self):
        ra = reissue_options(old_plan_a(), catalog_new(), records_a(), reqs(), ADV_A)
        rb = reissue_options(old_plan_b(), catalog_new(), records_b(), reqs(), ADV_B)
        return {"A": ra, "B": rb}

    def test_original_cheapest_is_lan_once(self):
        res = schedule_batch(self._reissues(), price=PRICE)
        self.assertTrue(res.feasible)
        self.assertEqual(res.total_cost, 3)
        self.assertEqual(res.attendees, ["Lan"])
        self.assertEqual(res.assignments["A"]["advisors"], ["Lan"])
        self.assertEqual(res.assignments["B"]["advisors"], ["Lan"])

    def test_after_withdrawing_q_to_h_cost_becomes_mei_plus_zhu(self):
        ra = reissue_options(old_plan_a(), catalog_q_no_h(), records_a(), reqs(), ADV_A)
        rb = reissue_options(old_plan_b(), catalog_q_no_h(), records_b(), reqs(), ADV_B)
        res = schedule_batch({"A": ra, "B": rb}, price=PRICE)
        self.assertTrue(res.feasible)
        self.assertEqual(res.total_cost, 4)
        self.assertEqual(res.attendees, ["Mei", "Zhu"])

    def test_companion_pair_makes_single_lan_illegal_cost_5(self):
        res = schedule_batch(self._reissues(), price=PRICE,
                             companion_groups=[["Lan", "Zhu"]])
        self.assertTrue(res.feasible)
        # Any Zhu attendance now drags Lan in (and vice versa), so the old
        # "single Lan = 3" trip becomes Lan+Zhu = 5.  Alternatives that avoid
        # Lan (Mei for A, Zhu for B) force Zhu -> Lan too, costing 3+2+2=7.
        # The unique optimum is therefore Lan+Zhu = 5, matching the prompt.
        self.assertEqual(res.total_cost, 5)
        self.assertEqual(res.attendees, ["Lan", "Zhu"])

    def test_companion_with_fixed_lan_yields_5(self):
        # "登记兰、竹必须同车到场，要来则两人均来": once Lan is a committed
        # attendee (fixed), the cheapest round is Lan+Zhu = 3+2 = 5.
        res = schedule_batch(self._reissues(), price=PRICE,
                             fixed_attendees=["Lan"],
                             companion_groups=[["Lan", "Zhu"]])
        self.assertTrue(res.feasible)
        self.assertEqual(res.total_cost, 5)
        self.assertEqual(res.attendees, ["Lan", "Zhu"])

    def test_fixed_attendee_fee_counted_even_if_unused(self):
        # 固定的已确认参加者必须连同其费用计入, even if no package needs them.
        # "Kai" is committed but appears in no consent set; the fee is still paid.
        price = dict(PRICE, Kai=1)
        res = schedule_batch(self._reissues(), price=price,
                             fixed_attendees=["Kai"])
        self.assertTrue(res.feasible)
        self.assertEqual(res.total_cost, 4)  # 3 (Lan) + 1 (Kai)
        self.assertIn("Kai", res.attendees)

    def test_fixed_attendee_covers_student(self):
        # A fixed Mei covers Alpha for free; Beta still needs the cheaper Zhu.
        res = schedule_batch(self._reissues(), price=PRICE, fixed_attendees=["Mei"])
        self.assertEqual(res.assignments["A"]["advisors"], ["Mei"])
        self.assertEqual(res.total_cost, 4)  # Mei(2) + Zhu(2)

    def test_incompatible_pair_makes_batch_infeasible(self):
        # Mei cannot attend with Lan or Zhu.  {Lan} still covers both students.
        res = schedule_batch(self._reissues(), price=PRICE,
                             incompatible_pairs=[["Mei", "Lan"], ["Mei", "Zhu"]])
        self.assertTrue(res.feasible)
        self.assertEqual(res.total_cost, 3)
        self.assertEqual(res.attendees, ["Lan"])

        res2 = schedule_batch(self._reissues(), price=PRICE,
                              incompatible_pairs=[["Lan", "Zhu"], ["Mei", "Lan"]])
        # {Lan} alone covers both students and avoids the forbidden pairs -> 3.
        self.assertTrue(res2.feasible)
        self.assertEqual(res2.total_cost, 3)

    def test_whole_batch_impossible_example(self):
        # Construct a genuine whole-batch failure: every package for B contains
        # Lan or Zhu, and both Lan and Zhu are incompatible with Mei; Mei is a
        # fixed attendee, and A needs Mei or Lan (Lan forbidden) so A needs Mei.
        # B needs Lan or Zhu, both forbidden with fixed Mei -> no way.
        res = schedule_batch(self._reissues(), price=PRICE,
                             fixed_attendees=["Mei"],
                             incompatible_pairs=[["Mei", "Lan"], ["Mei", "Zhu"]])
        self.assertFalse(res.feasible)
        self.assertIsNone(res.total_cost)
        self.assertTrue(res.conflicts)

    def test_cannot_reissue_student_blocks_round(self):
        cat = parse_catalog({
            "format": "course-catalog/1", "revision": 9,
            "courses": [{"course_id": "CG", "categories": ["G"]}],
            "equivalences": {"P": ["CG"], "Q": ["CG"], "R": ["CG"]},
        })
        hard = parse_requirements({"request_id": "RQ", "revision": 9, "groups": [
            {"group_id": "G", "allowed_categories": ["G"], "minimum_points": 100},
        ]})
        bad_a = reissue_options(old_plan_a(), cat, records_a(), hard, ADV_A)
        good_b = reissue_options(old_plan_b(), catalog_new(), records_b(), reqs(), ADV_B)
        res = schedule_batch({"A": bad_a, "B": good_b}, price=PRICE)
        self.assertFalse(res.feasible)
        self.assertIn("cannot_reissue", {c["code"] for c in res.conflicts})

    def test_equal_cost_alternatives_both_valid(self):
        # All advisors priced equally; multiple attendance sets tie at the
        # same minimum cost.  Any one reported must still cover every student.
        price = {"Mei": 1, "Lan": 1, "Zhu": 1}
        res = schedule_batch(self._reissues(), price=price)
        self.assertTrue(res.feasible)
        self.assertEqual(res.total_cost, 1)
        for sid in ("A", "B"):
            advs = set(res.assignments[sid]["advisors"])
            self.assertTrue(advs <= set(res.attendees))

    def test_schedule_packages_per_student_plan_present(self):
        reissues = self._reissues()
        students = {sid: [Package(sid, frozenset(o.advisors), o.plan.as_document())
                          for o in r.options]
                    for sid, r in reissues.items()}
        res = schedule_packages(students, price=PRICE)
        self.assertTrue(res.feasible)
        self.assertEqual(res.total_cost, 3)
        self.assertIsNotNone(res.assignments["A"]["plan"])
        self.assertIsNotNone(res.assignments["B"]["plan"])

    def test_schedule_packages_rejects_empty_student(self):
        with self.assertRaises(SchemaError):
            schedule_packages({"A": []}, price=PRICE)

    def test_missing_price_is_rejected_not_billed_zero(self):
        with self.assertRaises(SchemaError):
            schedule_batch(self._reissues(), price={"Mei": 2})  # no Lan/Zhu


# =========================================================================== #
# Store integration: confirmed destinations only
# =========================================================================== #
class TestStoreReissue(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.import_catalog({
            "format": "course-catalog/1", "revision": 1,
            "courses": [
                {"course_id": "CG", "categories": ["G"]},
                {"course_id": "CH", "categories": ["H"]},
            ],
            "equivalences": {"P": ["CG", "CH"], "Q": ["CG", "CH"]},
        })
        self.store.import_records({"student_id": "A", "revision": 1, "records": [
            {"record_id": "A_M", "external_course": "P", "points": 3},
            {"record_id": "A_L", "external_course": "Q", "points": 3},
        ]})
        self.store.import_requirements({"request_id": "RQ", "revision": 1, "groups": [
            {"group_id": "G", "allowed_categories": ["G"], "minimum_points": 3},
        ]})
        self.old = {
            "format": "course-allocation/1", "student_id": "A",
            "catalog_revision": 1, "records_revision": 1,
            "request_id": "RQ", "requirements_revision": 1,
            "assignments": [
                {"record_id": "A_M", "course_id": "CG", "group_id": "G"},
                {"record_id": "A_L", "course_id": "CG", "group_id": "G"},
            ],
        }

    def tearDown(self):
        self.store.close()

    def _confirm(self):
        self.store.prepare_audit(self.old, request_id="RQ", catalog_revision=1,
                                 records_revision=1, requirements_revision=1)
        self.store.confirm_audit(request_id="RQ")

    def _new_revisions(self):
        self.store.import_catalog({
            "format": "course-catalog/1", "revision": 2,
            "courses": [
                {"course_id": "CG", "categories": ["G"]},
                {"course_id": "CH", "categories": ["H"]},
            ],
            "equivalences": {"P": ["CG", "CH"], "Q": ["CG", "CH"]},
        })
        self.store.import_requirements({"request_id": "RQ", "revision": 2, "groups": [
            {"group_id": "G", "allowed_categories": ["G"], "minimum_points": 3},
            {"group_id": "H", "allowed_categories": ["H"], "minimum_points": 3},
        ]})

    def test_reissue_requires_live_confirmation(self):
        # Prepared but never confirmed -> refused.
        self.store.prepare_audit(self.old, request_id="RQ", catalog_revision=1,
                                 records_revision=1, requirements_revision=1)
        self.store.set_advisor_assignments(request_id="RQ", advisor_of=ADV_A)
        self._new_revisions()
        with self.assertRaises(ConfirmationError):
            self.store.reissue(request_id="RQ", catalog_revision=2,
                               records_revision=1, requirements_revision=2)

    def test_reissue_refuses_stale_confirmation(self):
        self._confirm()
        self.store.set_advisor_assignments(request_id="RQ", advisor_of=ADV_A)
        self._new_revisions()  # makes the old confirmation stale
        with self.assertRaises(ConfirmationError):
            self.store.reissue(request_id="RQ", catalog_revision=2,
                               records_revision=1, requirements_revision=2)

    def test_reissue_after_reconfirm_uses_new_destinations(self):
        self._confirm()
        self.store.set_advisor_assignments(request_id="RQ", advisor_of=ADV_A)
        self._new_revisions()
        # Re-confirm against revision 2 with a valid new plan (A_M->H, A_L->G).
        new_plan = {
            "format": "course-allocation/1", "student_id": "A",
            "catalog_revision": 2, "records_revision": 1,
            "request_id": "RQ", "requirements_revision": 2,
            "assignments": [
                {"record_id": "A_M", "course_id": "CH", "group_id": "H"},
                {"record_id": "A_L", "course_id": "CG", "group_id": "G"},
            ],
        }
        self.store.prepare_audit(new_plan, request_id="RQ", catalog_revision=2,
                                 records_revision=1, requirements_revision=2)
        self.store.confirm_audit(request_id="RQ")
        # Now re-issue against the SAME revisions: the confirmed plan already
        # satisfies everything, so no advisor must sign (empty set is minimal).
        res = self.store.reissue(request_id="RQ", catalog_revision=2,
                                 records_revision=1, requirements_revision=2)
        self.assertTrue(res.options)
        self.assertIn(frozenset(), {o.advisors for o in res.options})
        self.assertIn("reissue_analysed", [h["kind"] for h in self.store.history("RQ")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
