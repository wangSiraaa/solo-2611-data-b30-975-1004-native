"""Tests for the paper-archive reconstruction + custody retention layer.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import json
import os
import random
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gradaudit.archive import parse_archive
from gradaudit.custody import (
    custody_report,
    optimize_custody,
    parse_custody,
    review_retention,
)
from gradaudit.errors import BatchAtomicityError, SchemaError
from gradaudit.model import parse_catalog, parse_records, parse_requirements
from gradaudit.verify import verify_plan_document

EX = os.path.join(ROOT, "examples", "archive")


def load(name):
    with open(os.path.join(EX, name), encoding="utf-8") as fh:
        return json.load(fh)


def archive_doc(**over):
    doc = {
        "format": "paper-archive/1",
        "revision": 1,
        "boxes": [{"box_id": "A", "cost": 1}, {"box_id": "B", "cost": 1}],
        "copies": [
            {"copy_id": "a1", "page_id": "a", "box_id": "A"},
            {"copy_id": "a2", "page_id": "a", "box_id": "B"},
        ],
        "seal_rules": [{"student_id": "S", "record_id": "R1", "sets": [["a"]]}],
    }
    doc.update(over)
    return doc


# =========================================================================== #
# archive reconstruction
# =========================================================================== #
class TestReconstruction(unittest.TestCase):
    def test_any_readable_subset_yields_records_and_evidence(self):
        arc = parse_archive(archive_doc())
        self.assertEqual(arc.reconstructable_records("S", ["A"]), ["R1"])
        self.assertEqual(arc.reconstructable_records("S", ["B"]), ["R1"])
        self.assertEqual(arc.reconstructable_records("S", []), [])
        self.assertEqual(arc.reconstructable_records("S", ["A", "B"]), ["R1"])

    def test_two_copies_restore_record_once(self):
        arc = parse_archive(archive_doc())
        ev = arc.reconstruct_for_student("S", ["A", "B"])
        self.assertEqual(len(ev), 1)  # one record, not two
        self.assertEqual(ev[0].record_id, "R1")
        self.assertEqual(len(ev[0].pages), 1)  # one witness copy per named page
        self.assertIn(ev[0].pages[0].box_id, {"A", "B"})

    def test_every_page_of_a_set_is_indispensable(self):
        doc = archive_doc(seal_rules=[
            {"student_id": "S", "record_id": "R1", "sets": [["a", "b"]]}
        ])
        doc["copies"].append({"copy_id": "b1", "page_id": "b", "box_id": "A"})
        # b is unboxed -> not usable
        doc["copies"][-1]["box_id"] = None
        arc = parse_archive(doc)
        self.assertEqual(arc.reconstructable_records("S", ["A", "B"]), [])
        doc["copies"][-1]["box_id"] = "A"
        arc = parse_archive(doc)
        self.assertEqual(arc.reconstructable_records("S", ["A"]), ["R1"])

    def test_alternative_seal_sets(self):
        doc = archive_doc(seal_rules=[
            {"student_id": "S", "record_id": "R1", "sets": [["a"], ["b"]]}
        ])
        doc["copies"].append({"copy_id": "b1", "page_id": "b", "box_id": "B"})
        arc = parse_archive(doc)
        self.assertEqual(arc.reconstructable_records("S", ["A"]), ["R1"])
        self.assertEqual(arc.reconstructable_records("S", ["B"]), ["R1"])

    def test_voided_and_unaccepted_copies_unusable(self):
        doc = archive_doc()
        doc["copies"][1]["voided"] = True
        arc = parse_archive(doc)
        self.assertEqual(arc.reconstructable_records("S", ["B"]), [])
        doc["copies"][0]["accepted"] = False
        arc = parse_archive(doc)
        self.assertEqual(arc.reconstructable_records("S", ["A"]), [])

    def test_shared_page_across_students(self):
        doc = archive_doc(seal_rules=[
            {"student_id": "S", "record_id": "R1", "sets": [["a"]]},
            {"student_id": "T", "record_id": "R9", "sets": [["a"]]},
        ])
        arc = parse_archive(doc)
        self.assertEqual(arc.reconstructable_records("S", ["A"]), ["R1"])
        self.assertEqual(arc.reconstructable_records("T", ["A"]), ["R9"])

    def test_report_shape(self):
        arc = parse_archive(archive_doc())
        rep = arc.report(["A"])
        self.assertEqual(rep["format"], "archive-report/1")
        self.assertEqual(rep["readable_boxes"], ["A"])


# =========================================================================== #
# archive-station operations
# =========================================================================== #
class TestOperations(unittest.TestCase):
    def test_transfer_moves_copy_between_boxes(self):
        doc = archive_doc()
        doc["copies"] = [{"copy_id": "a1", "page_id": "a", "box_id": "A"}]
        doc["operations"] = [{"op": "transfer", "copy_id": "a1", "to_box": "B"}]
        arc = parse_archive(doc)
        self.assertEqual(arc.reconstructable_records("S", ["A"]), [])
        self.assertEqual(arc.reconstructable_records("S", ["B"]), ["R1"])

    def test_void_operation_disables_copy(self):
        doc = archive_doc(operations=[{"op": "void", "copy_id": "a2"}])
        arc = parse_archive(doc)
        self.assertEqual(arc.reconstructable_records("S", ["B"]), [])
        self.assertEqual(arc.reconstructable_records("S", ["A"]), ["R1"])

    def test_restore_seal_makes_copy_usable_again(self):
        doc = archive_doc(operations=[
            {"op": "void", "copy_id": "a2"},
            {"op": "restore", "copy_id": "a2"},
        ])
        arc = parse_archive(doc)
        self.assertEqual(arc.reconstructable_records("S", ["B"]), ["R1"])

    def test_variant_conflict_blocks_page_until_resolved(self):
        doc = archive_doc()
        doc["copies"][1]["variant"] = "v2"  # same page number, different variant
        arc = parse_archive(doc)
        # two live variants => ambiguous, page unusable
        self.assertEqual(arc.reconstructable_records("S", ["A"]), [])
        self.assertEqual(arc.reconstructable_records("S", ["B"]), [])

        # resolving to the v2 variant makes only the v2 copy usable
        doc["conflict_resolutions"] = {"a": "v2"}
        arc = parse_archive(doc)
        self.assertEqual(arc.reconstructable_records("S", ["A"]), [])
        self.assertEqual(arc.reconstructable_records("S", ["B"]), ["R1"])

    def test_voiding_one_variant_removes_conflict(self):
        doc = archive_doc(operations=[{"op": "void", "copy_id": "a2"}])
        doc["copies"][1]["variant"] = "v2"
        # a2 is voided so only the original variant is live -> no conflict
        arc = parse_archive(doc)
        self.assertEqual(arc.reconstructable_records("S", ["A"]), ["R1"])

    def test_unaccepted_variant_does_not_conflict(self):
        doc = archive_doc()
        doc["copies"][1]["variant"] = "v2"
        doc["copies"][1]["accepted"] = False  # not "经核对", cannot conflict
        arc = parse_archive(doc)
        # a1 (original variant, accepted) is still usable; a2 is simply unusable.
        self.assertEqual(arc.reconstructable_records("S", ["A"]), ["R1"])
        self.assertEqual(arc.reconstructable_records("S", ["B"]), [])

    def test_batch_seal_refuses_conflicted_box_atomically(self):
        doc = archive_doc()
        doc["copies"][0]["variant"] = "v1"
        doc["copies"][1]["variant"] = "v2"
        doc["operations"] = [{"op": "batch_seal", "box_ids": ["A", "B"]}]
        with self.assertRaises(BatchAtomicityError):
            parse_archive(doc)

    def test_batch_seal_accepts_resolved_boxes(self):
        doc = archive_doc()
        doc["copies"][0]["variant"] = "v1"
        doc["copies"][1]["variant"] = "v2"
        doc["conflict_resolutions"] = {"a": "v1"}
        doc["operations"] = [{"op": "batch_seal", "box_ids": ["A", "B"]}]
        arc = parse_archive(doc)
        self.assertTrue(all(c.sealed for c in arc.copies.values()))


# =========================================================================== #
# schemas / guard rails
# =========================================================================== #
class TestSchema(unittest.TestCase):
    def test_too_many_boxes(self):
        doc = archive_doc(boxes=[{"box_id": f"B{i}", "cost": 1} for i in range(17)])
        with self.assertRaises(SchemaError):
            parse_archive(doc)

    def test_unknown_box_reference(self):
        doc = archive_doc()
        doc["copies"][0]["box_id"] = "GHOST"
        with self.assertRaises(SchemaError):
            parse_archive(doc)

    def test_duplicate_seal_rule(self):
        doc = archive_doc(seal_rules=[
            {"student_id": "S", "record_id": "R1", "sets": [["a"]]},
            {"student_id": "S", "record_id": "R1", "sets": [["a"]]},
        ])
        with self.assertRaises(SchemaError):
            parse_archive(doc)

    def test_seal_rule_for_unknown_student_at_custody(self):
        doc = {
            "format": "custody/1",
            "archive": archive_doc(),
            "catalog": {"format": "course-catalog/1", "revision": 1,
                        "courses": [{"course_id": "C", "categories": ["G"]}],
                        "equivalences": {"E": ["C"]}},
            "students": [{
                "records": {"student_id": "OTHER", "revision": 1,
                            "records": [{"record_id": "R1", "external_course": "E", "points": 3}]},
                "requirements": {"request_id": "Q", "revision": 1,
                                 "groups": [{"group_id": "G", "allowed_categories": ["G"],
                                             "minimum_points": 3}]},
            }],
        }
        with self.assertRaises(SchemaError):
            parse_custody(doc)


# =========================================================================== #
# custody optimiser: the three requested demonstrations
# =========================================================================== #
class TestCustodyExamples(unittest.TestCase):
    def test_base_keeps_two_copies_cost_2(self):
        rep = custody_report(load("base.json"))
        self.assertTrue(rep["feasible"])
        self.assertEqual({b["box_id"] for b in rep["retained_boxes"]}, {"A", "B"})
        self.assertEqual(rep["total_cost"], 2)
        self.assertEqual({c["missing_box"] for c in rep["loss_certificates"]}, {"A", "B"})

    def test_voided_copy_forces_A_and_C_cost_4(self):
        rep = custody_report(load("voided_b.json"))
        self.assertTrue(rep["feasible"])
        self.assertEqual({b["box_id"] for b in rep["retained_boxes"]}, {"A", "C"})
        self.assertEqual(rep["total_cost"], 4)

    def test_restored_seal_but_B_costs_5_still_A_and_C(self):
        rep = custody_report(load("raised_b.json"))
        self.assertTrue(rep["feasible"])
        self.assertEqual({b["box_id"] for b in rep["retained_boxes"]}, {"A", "C"})
        self.assertEqual(rep["total_cost"], 4)

    def test_two_group_single_record_is_infeasible(self):
        rep = custody_report(load("two_group_infeasible.json"))
        self.assertFalse(rep["feasible"])
        fail = rep["normal_failure"]
        self.assertEqual(fail["scenario"]["kind"], "all_normal")
        stu = fail["students"][0]
        self.assertEqual(stu["group_maxima_ignoring_other_groups"], {"GA": 3, "GB": 3})
        self.assertEqual(stu["group_minima"], {"GA": 3, "GB": 3})

    def test_every_emitted_plan_is_independently_verified(self):
        for name in ("base.json", "voided_b.json", "raised_b.json"):
            doc = load(name)
            cat = parse_catalog(doc["catalog"])
            rep = custody_report(doc)
            for scenario in [rep["normal"]] + rep["loss_certificates"]:
                for p in scenario["students"]:
                    stu = next(s for s in doc["students"]
                               if s["records"]["student_id"] == p["student_id"])
                    recs = parse_records(stu["records"])
                    reqs = parse_requirements(stu["requirements"])
                    res = verify_plan_document(p["plan"], cat, recs, reqs)
                    with self.subTest(file=name, student=p["student_id"]):
                        self.assertTrue(res.accepted, res.reasons)
                        self.assertTrue(p["independent_verification"]["accepted"])

    def test_loss_evidence_never_cites_lost_box(self):
        rep = custody_report(load("base.json"))
        for cert in rep["loss_certificates"]:
            lost = cert["missing_box"]
            self.assertNotIn(lost, cert["readable_boxes"])
            for p in cert["students"]:
                for e in p["evidence"]:
                    for page in e["pages"]:
                        self.assertNotEqual(page["box_id"], lost)


# =========================================================================== #
# custodian properties: empty boxes, alternative allocations, shared certs
# =========================================================================== #
def _custody(archive, catalog, students):
    return {
        "format": "custody/1",
        "archive": archive,
        "catalog": catalog,
        "students": students,
    }


def _catalog(courses, equivalences):
    return {"format": "course-catalog/1", "revision": 1,
            "courses": courses, "equivalences": equivalences}


def _student(sid, records, groups, req_rev=1):
    return {
        "records": {"student_id": sid, "revision": 1, "records": records},
        "requirements": {"request_id": f"{sid}_REQ", "revision": req_rev, "groups": groups},
    }


class TestCustodyProperties(unittest.TestCase):
    def test_empty_kept_box_is_checked_in_loss_scenario(self):
        # An explicitly retained empty box is not skipped: the all-normal case
        # and its own loss are both evaluated (via review_retention).
        arch = {
            "format": "paper-archive/1", "revision": 1,
            "boxes": [{"box_id": "A", "cost": 1}, {"box_id": "B", "cost": 1},
                      {"box_id": "EMPTY", "cost": 0}],
            "copies": [
                {"copy_id": "a1", "page_id": "a", "box_id": "A"},
                {"copy_id": "a2", "page_id": "a", "box_id": "B"},
            ],
            "seal_rules": [{"student_id": "S", "record_id": "R1", "sets": [["a"]]}],
        }
        doc = _custody(
            arch,
            _catalog([{"course_id": "C", "categories": ["G"]}], {"E": ["C"]}),
            [_student("S", [{"record_id": "R1", "external_course": "E", "points": 3}],
                      [{"group_id": "G", "allowed_categories": ["G"], "minimum_points": 3}])],
        )
        archive, catalog, students = parse_custody(doc)
        rep = review_retention(archive, catalog, students, ["A", "B", "EMPTY"])
        self.assertTrue(rep["feasible"])
        retained = {b["box_id"] for b in rep["retained_boxes"]}
        self.assertEqual(retained, {"A", "B", "EMPTY"})
        # EMPTY still gets its own single-loss certificate.
        self.assertIn("EMPTY", {c["missing_box"] for c in rep["loss_certificates"]})
        # Cheapest optimisation drops the (free but unnecessary) empty box.
        opt = custody_report(doc)
        self.assertEqual({b["box_id"] for b in opt["retained_boxes"]}, {"A", "B"})

    def test_review_rejects_unsurvivable_empty_list(self):
        # Dropping to a single copy box cannot survive its own loss.
        arch = {
            "format": "paper-archive/1", "revision": 1,
            "boxes": [{"box_id": "A", "cost": 1}, {"box_id": "B", "cost": 1}],
            "copies": [{"copy_id": "a1", "page_id": "a", "box_id": "A"},
                       {"copy_id": "a2", "page_id": "a", "box_id": "B"}],
            "seal_rules": [{"student_id": "S", "record_id": "R1", "sets": [["a"]]}],
        }
        doc = _custody(
            arch,
            _catalog([{"course_id": "C", "categories": ["G"]}], {"E": ["C"]}),
            [_student("S", [{"record_id": "R1", "external_course": "E", "points": 3}],
                      [{"group_id": "G", "allowed_categories": ["G"], "minimum_points": 3}])],
        )
        archive, catalog, students = parse_custody(doc)
        rep = review_retention(archive, catalog, students, ["A"])
        self.assertFalse(rep["feasible"])
        self.assertEqual(rep["loss_failures"][0]["scenario"]["lost_box"], "A")

    def test_fault_scenarios_may_use_different_records(self):
        # Two independent single-page records; losing one box forces the other
        # record. Each record alone suffices for the group.
        arch = {
            "format": "paper-archive/1", "revision": 1,
            "boxes": [{"box_id": "A", "cost": 1}, {"box_id": "B", "cost": 1}],
            "copies": [
                {"copy_id": "a1", "page_id": "a", "box_id": "A"},
                {"copy_id": "b1", "page_id": "b", "box_id": "B"},
            ],
            "seal_rules": [
                {"student_id": "S", "record_id": "R1", "sets": [["a"]]},
                {"student_id": "S", "record_id": "R2", "sets": [["b"]]},
            ],
        }
        doc = _custody(
            arch,
            _catalog([{"course_id": "C", "categories": ["G"]}], {"E1": ["C"], "E2": ["C"]}),
            [_student("S", [
                {"record_id": "R1", "external_course": "E1", "points": 3},
                {"record_id": "R2", "external_course": "E2", "points": 3},
            ], [{"group_id": "G", "allowed_categories": ["G"], "minimum_points": 3}])],
        )
        rep = custody_report(doc)
        self.assertTrue(rep["feasible"])
        used = {}
        for cert in rep["loss_certificates"]:
            used[cert["missing_box"]] = {p["used_records"][0] for p in cert["students"]}
            # R2 uses page b in B, R1 uses page a in A; losing one still works.
        self.assertEqual(used["A"], {"R2"})
        self.assertEqual(used["B"], {"R1"})

    def test_shared_school_certificate_serves_multiple_students(self):
        arch = {
            "format": "paper-archive/1", "revision": 1,
            "boxes": [{"box_id": "SCHOOL", "cost": 2}],
            "copies": [{"copy_id": "cert1", "page_id": "school_cert", "box_id": "SCHOOL"}],
            "seal_rules": [
                {"student_id": "S", "record_id": "R1", "sets": [["school_cert"]]},
                {"student_id": "T", "record_id": "R1", "sets": [["school_cert"]]},
            ],
        }
        doc = _custody(
            arch,
            _catalog([{"course_id": "C", "categories": ["G"]}], {"E": ["C"]}),
            [
                _student("S", [{"record_id": "R1", "external_course": "E", "points": 3}],
                         [{"group_id": "G", "allowed_categories": ["G"], "minimum_points": 3}]),
                _student("T", [{"record_id": "R1", "external_course": "E", "points": 3}],
                         [{"group_id": "G", "allowed_categories": ["G"], "minimum_points": 3}]),
            ],
        )
        rep = custody_report(doc)
        # A single shared certificate box cannot survive its own loss, but there
        # is no other copy -> infeasible: must report it, not crash.
        self.assertFalse(rep["feasible"])
        # With the box present the normal case is fine; the failure is loss-only.
        self.assertIsNone(rep["normal_failure"])
        self.assertEqual(rep["loss_failures"][0]["scenario"]["lost_box"], "SCHOOL")
        fail_students = {s["student_id"] for s in rep["loss_failures"][0]["students"]}
        self.assertEqual(fail_students, {"S", "T"})

    def test_two_independent_shared_cert_copies_make_it_feasible(self):
        arch = {
            "format": "paper-archive/1", "revision": 1,
            "boxes": [{"box_id": "X", "cost": 1}, {"box_id": "Y", "cost": 1}],
            "copies": [
                {"copy_id": "c1", "page_id": "school_cert", "box_id": "X"},
                {"copy_id": "c2", "page_id": "school_cert", "box_id": "Y"},
            ],
            "seal_rules": [
                {"student_id": "S", "record_id": "R1", "sets": [["school_cert"]]},
                {"student_id": "T", "record_id": "R1", "sets": [["school_cert"]]},
            ],
        }
        doc = _custody(
            arch,
            _catalog([{"course_id": "C", "categories": ["G"]}], {"E": ["C"]}),
            [
                _student("S", [{"record_id": "R1", "external_course": "E", "points": 3}],
                         [{"group_id": "G", "allowed_categories": ["G"], "minimum_points": 3}]),
                _student("T", [{"record_id": "R1", "external_course": "E", "points": 3}],
                         [{"group_id": "G", "allowed_categories": ["G"], "minimum_points": 3}]),
            ],
        )
        rep = custody_report(doc)
        self.assertTrue(rep["feasible"])
        self.assertEqual(rep["total_cost"], 2)

    def test_no_shared_credit_between_students(self):
        # Same external course code, but each student's record is their own; the
        # solver never lets one student's record satisfy another.
        cat = _catalog([{"course_id": "C", "categories": ["G"]}], {"E": ["C"]})
        reqs = [{"group_id": "G", "allowed_categories": ["G"], "minimum_points": 3}]
        arch = {
            "format": "paper-archive/1", "revision": 1,
            "boxes": [{"box_id": "A", "cost": 1}],
            "copies": [{"copy_id": "a1", "page_id": "a", "box_id": "A"}],
            "seal_rules": [
                {"student_id": "S", "record_id": "R1", "sets": [["a"]]},
                # T's record has NO page evidence -> T cannot reconstruct
                {"student_id": "T", "record_id": "R1", "sets": [["t_page"]]},
            ],
        }
        doc = _custody(arch, cat, [
            _student("S", [{"record_id": "R1", "external_course": "E", "points": 3}], reqs),
            _student("T", [{"record_id": "R1", "external_course": "E", "points": 3}], reqs),
        ])
        rep = custody_report(doc)
        self.assertFalse(rep["feasible"])

    def test_multi_page_set_across_boxes(self):
        # One record needs pages a AND b; each page lives in its own box, and
        # each page has exactly one usable copy. Keeping both is necessary; the
        # loss of either box is fatal, so there is no feasible retention.
        arch = {
            "format": "paper-archive/1", "revision": 1,
            "boxes": [{"box_id": "A", "cost": 1}, {"box_id": "B", "cost": 1}],
            "copies": [
                {"copy_id": "a1", "page_id": "a", "box_id": "A"},
                {"copy_id": "b1", "page_id": "b", "box_id": "B"},
            ],
            "seal_rules": [{"student_id": "S", "record_id": "R1", "sets": [["a", "b"]]}],
        }
        doc = _custody(
            arch,
            _catalog([{"course_id": "C", "categories": ["G"]}], {"E": ["C"]}),
            [_student("S", [{"record_id": "R1", "external_course": "E", "points": 3}],
                      [{"group_id": "G", "allowed_categories": ["G"], "minimum_points": 3}])],
        )
        rep = custody_report(doc)
        self.assertFalse(rep["feasible"])
        lost = {f["scenario"]["lost_box"] for f in rep["loss_failures"]}
        self.assertEqual(lost, {"A", "B"})

    def test_multi_page_set_with_duplicates_is_feasible(self):
        arch = {
            "format": "paper-archive/1", "revision": 1,
            "boxes": [{"box_id": "A", "cost": 1}, {"box_id": "B", "cost": 1},
                      {"box_id": "C", "cost": 1}, {"box_id": "D", "cost": 1}],
            "copies": [
                {"copy_id": "a1", "page_id": "a", "box_id": "A"},
                {"copy_id": "a2", "page_id": "a", "box_id": "B"},
                {"copy_id": "b1", "page_id": "b", "box_id": "C"},
                {"copy_id": "b2", "page_id": "b", "box_id": "D"},
            ],
            "seal_rules": [{"student_id": "S", "record_id": "R1", "sets": [["a", "b"]]}],
        }
        doc = _custody(
            arch,
            _catalog([{"course_id": "C", "categories": ["G"]}], {"E": ["C"]}),
            [_student("S", [{"record_id": "R1", "external_course": "E", "points": 3}],
                      [{"group_id": "G", "allowed_categories": ["G"], "minimum_points": 3}])],
        )
        rep = custody_report(doc)
        self.assertTrue(rep["feasible"])
        self.assertEqual(rep["total_cost"], 4)

    def test_essential_box_makes_retention_infeasible(self):
        # Box A is the *only* home of page `a`; its loss is fatal, so no
        # retention list can survive every single-box loss.
        arch = {
            "format": "paper-archive/1", "revision": 1,
            "boxes": [{"box_id": "A", "cost": 1}, {"box_id": "B", "cost": 1}],
            "copies": [
                {"copy_id": "a1", "page_id": "a", "box_id": "A"},
                {"copy_id": "b1", "page_id": "b", "box_id": "B"},
                {"copy_id": "b2", "page_id": "b", "box_id": "A"},
            ],
            "seal_rules": [{"student_id": "S", "record_id": "R1", "sets": [["a"]]}],
        }
        doc = _custody(
            arch,
            _catalog([{"course_id": "C", "categories": ["G"]}], {"E": ["C"]}),
            [_student("S", [{"record_id": "R1", "external_course": "E", "points": 3}],
                      [{"group_id": "G", "allowed_categories": ["G"], "minimum_points": 3}])],
        )
        rep = custody_report(doc)
        self.assertFalse(rep["feasible"])
        self.assertEqual(rep["loss_failures"][0]["scenario"]["lost_box"], "A")
        self.assertEqual(
            [s["student_id"] for s in rep["loss_failures"][0]["students"]], ["S"]
        )

    def test_cheapest_is_chosen_over_alternative_cover(self):
        # Two alternative covers: cheap (A+B) and expensive (C+D).
        arch = {
            "format": "paper-archive/1", "revision": 1,
            "boxes": [{"box_id": "A", "cost": 2}, {"box_id": "B", "cost": 2},
                      {"box_id": "C", "cost": 5}, {"box_id": "D", "cost": 5}],
            "copies": [
                {"copy_id": "a1", "page_id": "a", "box_id": "A"},
                {"copy_id": "a2", "page_id": "a", "box_id": "B"},
                {"copy_id": "a3", "page_id": "a", "box_id": "C"},
                {"copy_id": "a4", "page_id": "a", "box_id": "D"},
            ],
            "seal_rules": [{"student_id": "S", "record_id": "R1", "sets": [["a"]]}],
        }
        doc = _custody(
            arch,
            _catalog([{"course_id": "C", "categories": ["G"]}], {"E": ["C"]}),
            [_student("S", [{"record_id": "R1", "external_course": "E", "points": 3}],
                      [{"group_id": "G", "allowed_categories": ["G"], "minimum_points": 3}])],
        )
        rep = custody_report(doc)
        self.assertTrue(rep["feasible"])
        self.assertEqual({b["box_id"] for b in rep["retained_boxes"]}, {"A", "B"})
        self.assertEqual(rep["total_cost"], 4)


# =========================================================================== #
# scale: up to 16 boxes and 12 records per student
# =========================================================================== #
class TestScale(unittest.TestCase):
    def test_16_boxes_12_records(self):
        boxes = [{"box_id": f"B{i:02d}", "cost": 1} for i in range(16)]
        copies = []
        seal_rules = []
        records = []
        # 12 records; each needs one page. Give each page two copies in
        # disjoint boxes so any single-box loss is survivable.
        for i in range(12):
            page = f"p{i}"
            b1 = f"B{(2 * i) % 16:02d}"
            b2 = f"B{(2 * i + 1) % 16:02d}"
            copies.append({"copy_id": f"{page}c1", "page_id": page, "box_id": b1})
            copies.append({"copy_id": f"{page}c2", "page_id": page, "box_id": b2})
            seal_rules.append({"student_id": "S", "record_id": f"R{i}", "sets": [[page]]})
            records.append({"record_id": f"R{i}", "external_course": "E", "points": 1})
        arch = {"format": "paper-archive/1", "revision": 1, "boxes": boxes,
                "copies": copies, "seal_rules": seal_rules}
        doc = _custody(
            arch,
            _catalog([{"course_id": "C", "categories": ["G"]}], {"E": ["C"]}),
            [_student("S", records,
                      [{"group_id": "G", "allowed_categories": ["G"], "minimum_points": 12}])],
        )
        rep = custody_report(doc)
        self.assertTrue(rep["feasible"])
        # All 12 records (1 point each) are needed; each page has two copies in
        # two different boxes, so every one of the 16 boxes is load-bearing.
        self.assertEqual(len(rep["retained_boxes"]), 16)
        self.assertEqual(rep["total_cost"], 16)

    def test_16_box_search_is_bounded(self):
        # 16 boxes, one record reachable via a duplicated page; the optimizer
        # must drop the 14 irrelevant boxes down to the two load-bearing ones.
        boxes = [{"box_id": f"B{i:02d}", "cost": 1} for i in range(16)]
        arch = {
            "format": "paper-archive/1", "revision": 1, "boxes": boxes,
            "copies": [
                {"copy_id": "x1", "page_id": "x", "box_id": "B00"},
                {"copy_id": "x2", "page_id": "x", "box_id": "B01"},
            ],
            "seal_rules": [{"student_id": "S", "record_id": "R1", "sets": [["x"]]}],
        }
        doc = _custody(
            arch,
            _catalog([{"course_id": "C", "categories": ["G"]}], {"E": ["C"]}),
            [_student("S", [{"record_id": "R1", "external_course": "E", "points": 3}],
                      [{"group_id": "G", "allowed_categories": ["G"], "minimum_points": 3}])],
        )
        rep = custody_report(doc)
        self.assertTrue(rep["feasible"])
        self.assertEqual({b["box_id"] for b in rep["retained_boxes"]}, {"B00", "B01"})
        self.assertEqual(rep["total_cost"], 2)


# =========================================================================== #
# randomised consistency: report is internally coherent and certificates hold
# =========================================================================== #
class TestRandomised(unittest.TestCase):
    def _random_case(self, rng):
        n_boxes = rng.randint(1, 5)
        box_ids = [f"B{i}" for i in range(n_boxes)]
        boxes = [{"box_id": b, "cost": rng.randint(0, 3)} for b in box_ids]
        pages = ["p0", "p1", "p2"]
        copies = []
        cid = 0
        for p in pages:
            for b in box_ids:
                if rng.random() < 0.4:
                    copies.append({"copy_id": f"c{cid}", "page_id": p, "box_id": b})
                    cid += 1
        if not copies:
            copies.append({"copy_id": "c0", "page_id": "p0", "box_id": box_ids[0]})
        seal_rules = []
        records = []
        for i, p in enumerate(pages):
            sets = [[p]]
            if rng.random() < 0.3:
                sets.append([rng.choice(pages), rng.choice(pages)])
            seal_rules.append({"student_id": "S", "record_id": f"R{i}", "sets": sets})
            records.append({"record_id": f"R{i}", "external_course": "E", "points": 1})
        arch = {"format": "paper-archive/1", "revision": 1, "boxes": boxes,
                "copies": copies, "seal_rules": seal_rules}
        doc = _custody(
            arch,
            _catalog([{"course_id": "C", "categories": ["G"]}], {"E": ["C"]}),
            [_student("S", records,
                      [{"group_id": "G", "allowed_categories": ["G"], "minimum_points": 1}])],
        )
        return doc

    def test_certificates_are_internally_consistent(self):
        rng = random.Random(1234)
        for trial in range(100):
            doc = self._random_case(rng)
            rep = custody_report(doc)
            with self.subTest(trial=trial):
                if not rep["feasible"]:
                    continue
                retained = {b["box_id"] for b in rep["retained_boxes"]}
                self.assertEqual(rep["normal"]["readable_boxes"], sorted(retained))
                missing = {c["missing_box"] for c in rep["loss_certificates"]}
                self.assertEqual(missing, retained)  # one certificate per kept box
                for p in rep["normal"]["students"]:
                    self.assertTrue(p["independent_verification"]["accepted"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
