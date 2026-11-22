"""Test suite for the gradaudit library.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gradaudit import (
    CharterDraft,
    RevisionConflictError,
    Store,
    StudentEntry,
    assess_scholarship,
    evaluate_honor,
    parse_catalog,
    parse_plan,
    parse_records,
    parse_requirements,
    review_bundle,
    solve,
    verify_honor_result,
    verify_plan,
    verify_plan_document,
)
from gradaudit.errors import ConfirmationStaleError, NotPreparedError

MAT = os.path.join(ROOT, "materials", "725")


def load(name):
    with open(os.path.join(MAT, name), encoding="utf-8") as fh:
        return json.load(fh)


def cat1():
    return parse_catalog(load("catalog.json"))


def cat2():
    return parse_catalog(load("catalog_revision2.json"))


def recs():
    return parse_records(load("records.json"))


def reqs():
    return parse_requirements(load("requirements.json"))


# =========================================================================== #
# parsing / catalog
# =========================================================================== #
class TestCatalog(unittest.TestCase):
    def test_candidates_are_explicit_not_guessed(self):
        c = cat1()
        self.assertEqual(c.candidates("X"), ("LC_CORE", "LC_OPT"))
        self.assertEqual(c.candidates("Z"), ("LC_BOTH",))
        # an external course absent from equivalences has no candidates
        self.assertEqual(c.candidates("NOT_IN_CATALOG"), ())
        # a bare suffix in the name does not conjure a candidate
        self.assertEqual(c.candidates("LC_CORE"), ())

    def test_candidate_categories_listed(self):
        c = cat1()
        out = c.candidates_with_categories("Z")
        self.assertEqual(out, [{"course_id": "LC_BOTH", "categories": ["CORE", "OPT"]}])

    def test_records_parse_rejects_bad_points(self):
        from gradaudit.errors import SchemaError

        bad = copy.deepcopy(load("records.json"))
        bad["records"][0]["points"] = 0
        with self.assertRaises(SchemaError):
            parse_records(bad)
        bad["records"][0]["points"] = True
        with self.assertRaises(SchemaError):
            parse_records(bad)

    def test_requirements_reject_too_many_groups(self):
        from gradaudit.errors import SchemaError

        bad = copy.deepcopy(load("requirements.json"))
        bad["groups"] = bad["groups"] * 2  # 4 groups
        with self.assertRaises(SchemaError):
            parse_requirements(bad)


# =========================================================================== #
# independent verification (the public contract)
# =========================================================================== #
class TestVerify(unittest.TestCase):
    def test_plan_a_and_b_both_accepted(self):
        for name in ("plan_a.json", "plan_b.json"):
            with self.subTest(plan=name):
                result = verify_plan_document(load(name), cat1(), recs(), reqs())
                self.assertTrue(result.accepted, result.reasons)
                self.assertEqual(result.group_totals, {"G_CORE": 3, "G_OPT": 3})
                self.assertEqual(set(result.used_records), {"R1", "R2"})

    def test_invalid_reuse_rejected_and_points_at_r1(self):
        result = verify_plan_document(load("plan_invalid_reuse.json"), cat1(), recs(), reqs())
        self.assertFalse(result.accepted)
        self.assertTrue(any(r.get("location") == "R1" for r in result.reasons), result.reasons)

    def test_unknown_record(self):
        plan = {
            "format": "course-allocation/1", "student_id": "ST1",
            "catalog_revision": 1, "records_revision": 1,
            "request_id": "REQ1", "requirements_revision": 1,
            "assignments": [
                {"record_id": "R1", "course_id": "LC_CORE", "group_id": "G_CORE"},
                {"record_id": "GHOST", "course_id": "LC_OPT", "group_id": "G_OPT"},
            ],
        }
        res = verify_plan_document(plan, cat1(), recs(), reqs())
        self.assertFalse(res.accepted)
        self.assertIn("unknown_record", {r["code"] for r in res.reasons})

    def test_non_candidate_course_rejected(self):
        # R1 (external X) cannot map to LC_BOTH (only Z can).
        plan = {
            "format": "course-allocation/1", "student_id": "ST1",
            "catalog_revision": 1, "records_revision": 1,
            "request_id": "REQ1", "requirements_revision": 1,
            "assignments": [
                {"record_id": "R1", "course_id": "LC_BOTH", "group_id": "G_CORE"},
                {"record_id": "R2", "course_id": "LC_OPT", "group_id": "G_OPT"},
            ],
        }
        res = verify_plan_document(plan, cat1(), recs(), reqs())
        self.assertFalse(res.accepted)
        self.assertIn("course_not_candidate", {r["code"] for r in res.reasons})

    def test_category_mismatch_rejected(self):
        # R1 -> LC_CORE assigned to the OPT group: course is a candidate but
        # its categories do not hit G_OPT.
        plan = {
            "format": "course-allocation/1", "student_id": "ST1",
            "catalog_revision": 1, "records_revision": 1,
            "request_id": "REQ1", "requirements_revision": 1,
            "assignments": [
                {"record_id": "R1", "course_id": "LC_CORE", "group_id": "G_OPT"},
                {"record_id": "R2", "course_id": "LC_CORE", "group_id": "G_CORE"},
                {"record_id": "R3", "course_id": "LC_BOTH", "group_id": "G_OPT"},
            ],
        }
        # G_CORE=3 ok; G_OPT gets only R3 (2) because R1 miscategorised
        res = verify_plan_document(plan, cat1(), recs(), reqs())
        self.assertFalse(res.accepted)
        codes = {r["code"] for r in res.reasons}
        self.assertIn("category_mismatch", codes)
        self.assertIn("group_underfilled", codes)

    def test_points_read_from_records_not_plan(self):
        plan = {
            "format": "course-allocation/1", "student_id": "ST1",
            "catalog_revision": 1, "records_revision": 1,
            "request_id": "REQ1", "requirements_revision": 1,
            # A roguish extra points field must be ignored.
            "assignments": [
                {"record_id": "R3", "course_id": "LC_BOTH", "group_id": "G_OPT", "points": 99},
            ],
        }
        res = verify_plan_document(plan, cat1(), recs(), reqs())
        self.assertFalse(res.accepted)  # R3 is only 2 points -> underfilled
        self.assertEqual(res.group_totals["G_OPT"], 2)
        self.assertTrue(any(r.get("location") == "G_OPT" for r in res.reasons))

    def test_unused_records_are_fine(self):
        # plan_a leaves R3 unused and is accepted.
        res = verify_plan_document(load("plan_a.json"), cat1(), recs(), reqs())
        self.assertNotIn("R3", res.used_records)

    def test_extra_plan_fields_ignored(self):
        plan = load("plan_a.json")
        plan["comment"] = "extra fields are allowed"
        plan["assignments"][0]["note"] = "still fine"
        res = verify_plan_document(plan, cat1(), recs(), reqs())
        self.assertTrue(res.accepted, res.reasons)

    def test_revision_mismatch(self):
        res = verify_plan_document(load("plan_a.json"), cat2(), recs(), reqs())
        self.assertFalse(res.accepted)
        self.assertIn("revision_mismatch", {r["code"] for r in res.reasons})

    def test_any_contract_satisfying_plan_accepted_not_just_solver_output(self):
        # plan_b differs from the solver's choice but must still be accepted.
        solver_plan = solve(cat1(), recs(), reqs()).as_plan_document()
        plan_b = load("plan_b.json")
        self.assertNotEqual(solver_plan["assignments"], plan_b["assignments"])
        self.assertTrue(verify_plan_document(plan_b, cat1(), recs(), reqs()).accepted)


# =========================================================================== #
# solver
# =========================================================================== #
class TestSolver(unittest.TestCase):
    def test_rev1_feasible(self):
        result = solve(cat1(), recs(), reqs())
        self.assertTrue(result.feasible)
        self.assertEqual(result.group_totals, {"G_CORE": 3, "G_OPT": 3})
        # the emitted plan must pass the independent verifier
        self.assertTrue(verify_plan_document(result.as_plan_document(), cat1(), recs(), reqs()).accepted)

    def test_rev2_infeasible_with_group_maxima(self):
        result = solve(cat2(), recs(), reqs())
        self.assertFalse(result.feasible)
        maxima = {g.group_id: g.max_ignoring_other_groups for g in result.groups}
        self.assertEqual(maxima["G_OPT"], 2)
        self.assertEqual(maxima["G_CORE"], 8)

    def test_single_group_reachable_but_overall_not(self):
        # One record, 3 points, usable in either group; both groups need 3.
        catalog = {
            "format": "course-catalog/1", "revision": 1,
            "courses": [
                {"course_id": "C1", "categories": ["A"]},
                {"course_id": "C2", "categories": ["B"]},
                {"course_id": "CB", "categories": ["A", "B"]},
            ],
            "equivalences": {"E": ["CB"]},
        }
        records = {"student_id": "S", "revision": 1,
                   "records": [{"record_id": "R1", "external_course": "E", "points": 3}]}
        requirements = {"request_id": "Q", "revision": 1, "groups": [
            {"group_id": "GA", "allowed_categories": ["A"], "minimum_points": 3},
            {"group_id": "GB", "allowed_categories": ["B"], "minimum_points": 3},
        ]}
        res = solve(parse_catalog(catalog), parse_records(records), parse_requirements(requirements))
        self.assertFalse(res.feasible)
        for g in res.groups:
            self.assertEqual(g.max_ignoring_other_groups, 3)
            self.assertTrue(g.satisfied)  # each alone is enough
        self.assertIn("does not make the request feasible", res.note)

    def test_multi_candidate_and_unused(self):
        # Two groups, several candidates; solver must pick an exact allocation
        # and may leave records unused.
        catalog = {
            "format": "course-catalog/1", "revision": 1,
            "courses": [
                {"course_id": "A1", "categories": ["A"]},
                {"course_id": "A2", "categories": ["A"]},
                {"course_id": "B1", "categories": ["B"]},
                {"course_id": "AB", "categories": ["A", "B"]},
            ],
            "equivalences": {"EA": ["A1", "A2", "AB"], "EB": ["B1", "AB"], "EX": ["A1"]},
        }
        records = {"student_id": "S", "revision": 1, "records": [
            {"record_id": "R1", "external_course": "EA", "points": 2},
            {"record_id": "R2", "external_course": "EB", "points": 2},
            {"record_id": "R3", "external_course": "EA", "points": 2},
            {"record_id": "R4", "external_course": "EX", "points": 2},
        ]}
        requirements = {"request_id": "Q", "revision": 1, "groups": [
            {"group_id": "GA", "allowed_categories": ["A"], "minimum_points": 4},
            {"group_id": "GB", "allowed_categories": ["B"], "minimum_points": 2},
        ]}
        c, r, q = parse_catalog(catalog), parse_records(records), parse_requirements(requirements)
        res = solve(c, r, q)
        self.assertTrue(res.feasible)
        self.assertTrue(verify_plan_document(res.as_plan_document(), c, r, q).accepted)
        self.assertEqual(res.group_totals["GA"], 4)
        self.assertEqual(res.group_totals["GB"], 2)
        # R4 can only ever feed GA; it is used here. But no record may repeat.
        used = [a.record_id for a in res.assignments]
        self.assertEqual(len(used), len(set(used)))

    def test_exact_infeasibility_due_to_shared_record(self):
        # Same as above but GB minimum cannot coexist: only CB is shared and
        # each group needs a *separate* record.
        catalog = {
            "format": "course-catalog/1", "revision": 1,
            "courses": [
                {"course_id": "CA", "categories": ["A"]},
                {"course_id": "CB_", "categories": ["B"]},
                {"course_id": "CAB", "categories": ["A", "B"]},
            ],
            "equivalences": {"SHARED": ["CAB"]},
        }
        records = {"student_id": "S", "revision": 1,
                   "records": [{"record_id": "R1", "external_course": "SHARED", "points": 3}]}
        requirements = {"request_id": "Q", "revision": 1, "groups": [
            {"group_id": "GA", "allowed_categories": ["A"], "minimum_points": 3},
            {"group_id": "GB", "allowed_categories": ["B"], "minimum_points": 3},
        ]}
        res = solve(parse_catalog(catalog), parse_records(records), parse_requirements(requirements))
        self.assertFalse(res.feasible)


# =========================================================================== #
# SQLite library: revisions, conflict, prepare/confirm/reopen
# =========================================================================== #
class TestLibrary(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def _import_all(self):
        self.store.import_catalog(load("catalog.json"))
        self.store.import_records(load("records.json"))
        self.store.import_requirements(load("requirements.json"))

    def test_same_revision_different_content_rejected(self):
        self.store.import_catalog(load("catalog.json"))
        # revision 1 again but changed content
        changed = copy.deepcopy(load("catalog.json"))
        changed["equivalences"]["X"] = ["LC_CORE"]
        with self.assertRaises(RevisionConflictError):
            self.store.import_catalog(changed)
        # identical re-import is idempotent
        self.store.import_catalog(load("catalog.json"))

    def test_old_revision_remains_queryable(self):
        self.store.import_catalog(load("catalog.json"))
        self.store.import_catalog(load("catalog_revision2.json"))
        self.assertEqual(self.store.list_candidates(1, "X"), [
            {"course_id": "LC_CORE", "categories": ["CORE"]},
            {"course_id": "LC_OPT", "categories": ["OPT"]},
        ])
        self.assertEqual(self.store.list_candidates(2, "X"), [
            {"course_id": "LC_CORE", "categories": ["CORE"]},
        ])

    def test_prepare_confirm_status_flow(self):
        self._import_all()
        prep = self.store.prepare_audit(
            load("plan_a.json"), request_id="REQ1",
            catalog_revision=1, records_revision=1, requirements_revision=1,
        )
        self.assertTrue(prep["accepted"])
        pts = self.store.preparation_points(prep["preparation_id"])
        self.assertEqual(
            {(p["record_id"], p["course_id"], p["group_id"], p["points"]) for p in pts},
            {("R1", "LC_CORE", "G_CORE", 3), ("R2", "LC_OPT", "G_OPT", 3)},
        )
        conf = self.store.confirm_audit(request_id="REQ1")
        self.assertIn("confirmation_id", conf)
        status = self.store.confirmation_status("REQ1")
        self.assertTrue(status["confirmed"])
        self.assertIsNone(status["stale"])

    def test_rejected_preparation_cannot_be_confirmed(self):
        self._import_all()
        prep = self.store.prepare_audit(
            load("plan_invalid_reuse.json"), request_id="REQ1",
            catalog_revision=1, records_revision=1, requirements_revision=1,
        )
        self.assertFalse(prep["accepted"])
        from gradaudit.errors import ConfirmationError
        with self.assertRaises(ConfirmationError):
            self.store.confirm_audit(request_id="REQ1")

    def test_catalog_change_makes_confirmation_stale_and_blocks_reconfirm(self):
        self._import_all()
        prep = self.store.prepare_audit(
            load("plan_a.json"), request_id="REQ1",
            catalog_revision=1, records_revision=1, requirements_revision=1,
        )
        conf = self.store.confirm_audit(request_id="REQ1")
        # Mapping revision to rev2, re-registered against REQ1.
        self.store.import_catalog(load("catalog_revision2.json"))
        self.store.register_request(
            request_id="REQ1", catalog_revision=2,
            records_revision=1, requirements_revision=1,
        )
        status = self.store.confirmation_status("REQ1")
        self.assertFalse(status["confirmed"])
        self.assertIsNotNone(status["stale"])
        # old confirmation must not be re-confirmable against new revisions
        with self.assertRaises(ConfirmationStaleError):
            self.store.confirm_audit(request_id="REQ1", preparation_id=prep["preparation_id"])
        # but history keeps the original confirmation intact
        confs = self.store.list_confirmations("REQ1")
        self.assertEqual(len(confs), 1)
        self.assertEqual(confs[0]["confirmation_id"], conf["confirmation_id"])

    def test_mapping_revision_really_changes_next_feasibility(self):
        self._import_all()
        self.store.import_catalog(load("catalog_revision2.json"))
        self.store.register_request(
            request_id="REQ1", catalog_revision=1,
            records_revision=1, requirements_revision=1,
        )
        # rev1 can satisfy both groups
        from gradaudit.solver import solve as _solve
        r1 = _solve(self.store.get_catalog(1), self.store.get_records(1), self.store.get_requirements(1))
        self.assertTrue(r1.feasible)
        # rev2 cannot
        r2 = _solve(self.store.get_catalog(2), self.store.get_records(1), self.store.get_requirements(1))
        self.assertFalse(r2.feasible)

    def test_reopen_keeps_history(self):
        self._import_all()
        self.store.prepare_audit(
            load("plan_a.json"), request_id="REQ1",
            catalog_revision=1, records_revision=1, requirements_revision=1,
        )
        self.store.confirm_audit(request_id="REQ1")
        res = self.store.reopen_audit(request_id="REQ1", reason="student appealed")
        self.assertTrue(res["reopened"])
        self.assertFalse(self.store.confirmation_status("REQ1")["confirmed"])
        # history retains the confirmation and events
        hist = self.store.history("REQ1")
        kinds = [h["kind"] for h in hist]
        self.assertIn("confirmed", kinds)
        self.assertIn("reopened", kinds)
        self.assertEqual(len(self.store.list_confirmations("REQ1")), 1)

    def test_external_plan_uses_same_identity_and_confirmation_boundary(self):
        # A plan with a wrong records_revision for the *current* preparation
        # is rejected at prepare time by the independent verifier.
        self._import_all()
        plan = load("plan_b.json")
        plan["records_revision"] = 2  # not the supplied records rev
        prep = self.store.prepare_audit(
            plan, request_id="REQ1",
            catalog_revision=1, records_revision=1, requirements_revision=1,
        )
        self.assertFalse(prep["accepted"])

    def test_prepare_rejects_mismatched_request_id(self):
        self._import_all()
        with self.assertRaises(Exception):
            self.store.prepare_audit(
                load("plan_a.json"), request_id="OTHER",
                catalog_revision=1, records_revision=1, requirements_revision=1,
            )

    def test_importing_newer_revision_alone_stales_confirmation(self):
        # No explicit register_request: merely importing a newer catalog
        # revision must invalidate the previously confirmed audit.
        self._import_all()
        self.store.prepare_audit(
            load("plan_a.json"), request_id="REQ1",
            catalog_revision=1, records_revision=1, requirements_revision=1,
        )
        self.store.confirm_audit(request_id="REQ1")
        self.assertTrue(self.store.confirmation_status("REQ1")["confirmed"])
        self.store.import_catalog(load("catalog_revision2.json"))
        status = self.store.confirmation_status("REQ1")
        self.assertFalse(status["confirmed"])
        self.assertIn("catalog_revision", status["stale"])

    def test_importing_newer_records_alone_stales_confirmation(self):
        self._import_all()
        self.store.prepare_audit(
            load("plan_a.json"), request_id="REQ1",
            catalog_revision=1, records_revision=1, requirements_revision=1,
        )
        self.store.confirm_audit(request_id="REQ1")
        newer = copy.deepcopy(load("records.json"))
        newer["revision"] = 2
        newer["records"][2]["points"] = 4  # R3 changed
        self.store.import_records(newer)
        status = self.store.confirmation_status("REQ1")
        self.assertFalse(status["confirmed"])
        self.assertIn("records_revision", status["stale"])


class TestSolverExactness(unittest.TestCase):
    """Brute-force cross-check that the DP's feasible/infeasible answers agree
    with exhaustive enumeration on many small random instances."""

    def _brute_force(self, catalog, records, requirements):
        import itertools

        rec_list = sorted(records.records.values(), key=lambda r: r.record_id)
        group_list = list(requirements.groups)
        # per record: list of (None) skip or (course, group_index) options
        per_record_options = []
        for rec in rec_list:
            opts = [None]
            cands = catalog.candidates(rec.external_course)
            for g_idx, g in enumerate(group_list):
                for cid in cands:
                    if set(catalog.courses[cid].categories) & set(g.allowed_categories):
                        opts.append((cid, g_idx))
            per_record_options.append(opts)
        for combo in itertools.product(*per_record_options):
            totals = [0] * len(group_list)
            for rec, choice in zip(rec_list, combo):
                if choice is None:
                    continue
                _, g_idx = choice
                totals[g_idx] += rec.points
            if all(totals[g] >= group_list[g].minimum_points for g in range(len(group_list))):
                return True
        return False

    def test_random_small_instances(self):
        import random

        rng = random.Random(20240101)
        for trial in range(200):
            n_courses = rng.randint(2, 4)
            cats = ["A", "B", "C"]
            courses = []
            for i in range(n_courses):
                chosen = rng.sample(cats, rng.randint(1, 2))
                courses.append({"course_id": f"C{i}", "categories": chosen})
            course_ids = [c["course_id"] for c in courses]
            ext_pool = ["E0", "E1", "E2", "E3"]
            equivalences = {}
            for ext in ext_pool:
                k = rng.randint(1, min(3, n_courses))
                equivalences[ext] = rng.sample(course_ids, k)
            catalog_doc = {"format": "course-catalog/1", "revision": 1,
                           "courses": courses, "equivalences": equivalences}
            n_records = rng.randint(0, 5)
            recs_doc = {"student_id": "S", "revision": 1, "records": [
                {"record_id": f"R{i}", "external_course": rng.choice(ext_pool),
                 "points": rng.randint(1, 6)}
                for i in range(n_records)
            ]}
            n_groups = rng.randint(1, 3)
            groups = []
            for gi in range(n_groups):
                groups.append({
                    "group_id": f"G{gi}",
                    "allowed_categories": rng.sample(cats, rng.randint(1, 2)),
                    "minimum_points": rng.randint(1, 6),
                })
            req_doc = {"request_id": "Q", "revision": 1, "groups": groups}

            c = parse_catalog(catalog_doc)
            r = parse_records(recs_doc)
            q = parse_requirements(req_doc)
            got = solve(c, r, q)
            expected = self._brute_force(c, r, q)
            with self.subTest(trial=trial):
                self.assertEqual(got.feasible, expected)
                if got.feasible:
                    # every solver answer must itself verify
                    self.assertTrue(
                        verify_plan_document(got.as_plan_document(), c, r, q).accepted
                    )


# =========================================================================== #
# honor evaluation (graduation first, then honor on the leftovers)
# =========================================================================== #
def _scenario_catalog():
    """Exclusive G courses, exclusive honor courses, E and F two-way codes."""
    return {
        "format": "course-catalog/1", "revision": 1,
        "courses": [
            {"course_id": "GC_A", "categories": ["G"]},
            {"course_id": "GC_B", "categories": ["G"]},
            {"course_id": "GC_C", "categories": ["G"]},
            {"course_id": "HON_A", "categories": ["HONOR"]},
            {"course_id": "HON_B", "categories": ["HONOR"]},
            {"course_id": "HON_C", "categories": ["HONOR"]},
            {"course_id": "E_HON", "categories": ["HONOR"]},
            {"course_id": "E_ORD", "categories": ["OPT"]},
            {"course_id": "F_HON", "categories": ["HONOR"]},
            {"course_id": "F_ORD", "categories": ["OPT"]},
        ],
        "equivalences": {
            "XA_G": ["GC_A"], "XB_G": ["GC_B"], "XC_G": ["GC_C"],
            "XA_H": ["HON_A"], "XB_H": ["HON_B"], "XC_H": ["HON_C"],
            "E": ["E_HON", "E_ORD"], "F": ["F_HON", "F_ORD"],
        },
    }


def _scenario_req():
    return {"request_id": "Q", "revision": 1, "groups": [
        {"group_id": "GRAD", "allowed_categories": ["G"], "minimum_points": 3},
    ]}


def _student(sid, recs):
    return {"student_id": sid, "revision": 1, "records": recs}


def _scenario_students(b_uses="E"):
    A = _student("A", [
        {"record_id": "A_G", "external_course": "XA_G", "points": 3},
        {"record_id": "A_H", "external_course": "XA_H", "points": 3},
        {"record_id": "A_E", "external_course": "E", "points": 2},
    ])
    bcode = "E" if b_uses == "E" else "F"
    B = _student("B", [
        {"record_id": "B_G", "external_course": "XB_G", "points": 3},
        {"record_id": "B_H", "external_course": "XB_H", "points": 2},
        {"record_id": "B_" + bcode, "external_course": bcode, "points": 2},
    ])
    C = _student("C", [
        {"record_id": "C_G", "external_course": "XC_G", "points": 3},
        {"record_id": "C_H", "external_course": "XC_H", "points": 4},
    ])
    return {
        "A": parse_records(A), "B": parse_records(B), "C": parse_records(C),
    }


class TestHonor(unittest.TestCase):
    def setUp(self):
        self.cat = parse_catalog(_scenario_catalog())
        self.req = parse_requirements(_scenario_req())
        self.students = _scenario_students("E")

    def test_graduation_must_be_complete_before_honor(self):
        # A: 3 G points used for graduation, 3+2 honor leftover -> 5.
        res = evaluate_honor(self.cat, self.students["A"], self.req, ["HONOR"])
        self.assertTrue(res.eligible)
        self.assertEqual(res.honor_points, 5)
        self.assertEqual(res.graduation_totals, {"GRAD": 3})
        # the graduate record is not also selected for honors
        self.assertNotIn("A_G", {s.record_id for s in res.honor_selections})

    def test_ineligible_when_cannot_graduate(self):
        recs = parse_records(_student("Z", [
            {"record_id": "Z_H", "external_course": "XA_H", "points": 3},
        ]))
        res = evaluate_honor(self.cat, recs, self.req, ["HONOR"])
        self.assertFalse(res.eligible)
        self.assertIsNone(res.honor_points)
        self.assertIn("cannot be met", res.reason)

    def test_honor_max_is_optimal_against_brute_force(self):
        from gradaudit.honor import apply_choices
        import itertools

        def brute(cat, recs, req, hcats, choices=None):
            if choices:
                cat = apply_choices(cat, choices)
            groups = list(req.groups)
            rl = sorted(recs.records.values(), key=lambda r: r.record_id)
            opts = []
            for rec in rl:
                cands = cat.candidates(rec.external_course)
                o = [None]
                for gi, g in enumerate(groups):
                    for cid in cands:
                        if set(cat.courses[cid].categories) & set(g.allowed_categories):
                            o.append(("g", gi))
                if any(set(cat.courses[cid].categories) & set(hcats) for cid in cands):
                    o.append(("h", None))
                opts.append(o)
            best = None
            for combo in itertools.product(*opts):
                totals = [0] * len(groups); h = 0
                for rec, ch in zip(rl, combo):
                    if ch is None:
                        continue
                    if ch[0] == "g":
                        totals[ch[1]] += rec.points
                    else:
                        h += rec.points
                if all(totals[i] >= groups[i].minimum_points for i in range(len(groups))):
                    best = h if best is None else max(best, h)
            return best

        import random
        rng = random.Random(99)
        for trial in range(120):
            nc = rng.randint(2, 4)
            courses = [{"course_id": f"C{i}", "categories": rng.sample(["A", "B", "H"], rng.randint(1, 2))}
                       for i in range(nc)]
            cids = [c["course_id"] for c in courses]
            eq = {f"E{j}": rng.sample(cids, rng.randint(1, min(3, nc))) for j in range(3)}
            cd = {"format": "course-catalog/1", "revision": 1, "courses": courses, "equivalences": eq}
            recd = {"student_id": "S", "revision": 1, "records": [
                {"record_id": f"R{i}", "external_course": rng.choice(list(eq)), "points": rng.randint(1, 6)}
                for i in range(rng.randint(0, 4))]}
            grpd = {"request_id": "Q", "revision": 1, "groups": [
                {"group_id": "G0", "allowed_categories": ["A"], "minimum_points": rng.randint(1, 3)}]}
            c = parse_catalog(cd); r = parse_records(recd); q = parse_requirements(grpd)
            res = evaluate_honor(c, r, q, ["H"])
            got = res.honor_points if res.eligible else None
            with self.subTest(trial=trial):
                self.assertEqual(got, brute(c, r, q, ["H"]))
                self.assertTrue(
                    verify_honor_result(res, c, r, q, ["H"]).accepted,
                    verify_honor_result(res, c, r, q, ["H"]).reasons,
                )

    def test_charter_choice_must_be_catalog_candidate(self):
        from gradaudit.honor import apply_choices
        from gradaudit.errors import SchemaError
        # E has candidates E_HON/E_ORD; GC_A is not one of them.
        with self.assertRaises(SchemaError):
            apply_choices(self.cat, {"E": "GC_A"})
        # a code absent from the catalog cannot be pinned
        with self.assertRaises(SchemaError):
            apply_choices(self.cat, {"NOPE": "GC_A"})

    def test_charter_cannot_introduce_candidate(self):
        from gradaudit.honor import CharterDraft
        from gradaudit.errors import SchemaError
        with self.assertRaises(SchemaError):
            CharterDraft.from_catalog(self.cat, ["UNKNOWN"])

    def test_charter_choice_changes_honor_outcome(self):
        from gradaudit.honor import apply_choices
        res_f = evaluate_honor(self.cat, self.students["A"], self.req, ["HONOR"],
                               fixed_choices={"E": "E_ORD"})
        self.assertEqual(res_f.honor_points, 3)  # E ordinary -> only A_H counts
        res_h = evaluate_honor(self.cat, self.students["A"], self.req, ["HONOR"],
                               fixed_choices={"E": "E_HON"})
        self.assertEqual(res_h.honor_points, 5)

    def test_verify_rejects_overclaimed_and_shared(self):
        res = evaluate_honor(self.cat, self.students["A"], self.req, ["HONOR"])
        # Overclaim the total.
        res.honor_points = 99
        vr = verify_honor_result(res, self.cat, self.students["A"], self.req, ["HONOR"])
        self.assertFalse(vr.accepted)
        self.assertIn("honor_total_mismatch", {r["code"] for r in vr.reasons})
        # Re-run to get a fresh good result, then share a record.
        res2 = evaluate_honor(self.cat, self.students["A"], self.req, ["HONOR"])
        from gradaudit.honor import HonorSelection
        res2.honor_selections.append(HonorSelection("A_G", "GC_A", 3))
        res2.honor_points = res2.honor_points + 3
        vr2 = verify_honor_result(res2, self.cat, self.students["A"], self.req, ["HONOR"])
        self.assertIn("record_shared", {r["code"] for r in vr2.reasons})

    def test_verify_rejects_false_ineligibility(self):
        good = evaluate_honor(self.cat, self.students["A"], self.req, ["HONOR"])
        from gradaudit.honor import HonorResult
        fake = HonorResult(
            eligible=False, student_id=good.student_id,
            catalog_revision=good.catalog_revision, records_revision=good.records_revision,
            requirements_revision=good.requirements_revision, honor_points=None,
        )
        vr = verify_honor_result(fake, self.cat, self.students["A"], self.req, ["HONOR"])
        self.assertIn("should_be_eligible", {r["code"] for r in vr.reasons})

    def test_review_bundle_pins_identity_and_verifies(self):
        bundle = review_bundle(self.cat, self.students["A"], self.req, ["HONOR"],
                               choices={"E": "E_HON"})
        self.assertTrue(bundle["graduation"]["verified"])
        self.assertTrue(bundle["honor"]["verified"])
        self.assertEqual(bundle["honor_points"], 5)
        self.assertEqual(bundle["identity"]["records_hash"], self.students["A"].content_hash())
        self.assertEqual(bundle["identity"]["student_id"], "A")
        self.assertTrue(bundle["fingerprint"])
        # graduation plan alone passes the ordinary independent verifier
        from gradaudit.honor import apply_choices
        eff = apply_choices(self.cat, {"E": "E_HON"})
        self.assertTrue(
            verify_plan_document(bundle["graduation"]["plan"], eff, self.students["A"], self.req).accepted
        )


# =========================================================================== #
# scholarship: always / some / never across legal finalizations
# =========================================================================== #
class TestScholarship(unittest.TestCase):
    def setUp(self):
        self.cat = parse_catalog(_scenario_catalog())
        self.req = parse_requirements(_scenario_req())

    def _entries(self, b_uses="E"):
        s = _scenario_students(b_uses)
        # tie order C, B, A
        return [StudentEntry("C", s["C"], self.req),
                StudentEntry("B", s["B"], self.req),
                StudentEntry("A", s["A"], self.req)]

    def test_a_always_wins_two_seats_regardless_of_e(self):
        draft = CharterDraft.from_catalog(self.cat, ["E"])
        rep = assess_scholarship(self.cat, self._entries("E"), ["HONOR"], draft,
                                 quota=2, tie_order=["C", "B", "A"])
        statuses = {s["student_id"]: s["status"] for s in rep["students"]}
        self.assertEqual(statuses["A"], "always")
        self.assertEqual(statuses["C"], "always")
        self.assertEqual(statuses["B"], "never")
        self.assertEqual({a["student_id"] for a in rep["canonical_award_list"]}, {"A", "C"})

    def test_corrected_record_makes_a_conditional(self):
        # B's E record re-keyed to independent code F; E ordinary, F honor
        # drops A to third under that finalization.
        draft = CharterDraft.from_catalog(self.cat, ["E", "F"])
        rep = assess_scholarship(self.cat, self._entries("F"), ["HONOR"], draft,
                                 quota=2, tie_order=["C", "B", "A"])
        statuses = {s["student_id"]: s["status"] for s in rep["students"]}
        self.assertEqual(statuses["A"], "some")
        self.assertEqual(statuses["C"], "always")
        self.assertEqual(statuses["B"], "some")

    def test_single_seat_makes_a_conditional(self):
        draft = CharterDraft.from_catalog(self.cat, ["E"])
        rep = assess_scholarship(self.cat, self._entries("E"), ["HONOR"], draft,
                                 quota=1, tie_order=["C", "B", "A"])
        statuses = {s["student_id"]: s["status"] for s in rep["students"]}
        self.assertEqual(statuses["A"], "some")
        self.assertEqual(statuses["C"], "some")
        self.assertEqual(statuses["B"], "never")

    def test_witnesses_are_cohort_consistent_and_actual(self):
        draft = CharterDraft.from_catalog(self.cat, ["E"])
        rep = assess_scholarship(self.cat, self._entries("E"), ["HONOR"], draft,
                                 quota=1, tie_order=["C", "B", "A"])
        a = next(s for s in rep["students"] if s["student_id"] == "A")
        w = a["witness"]
        win, lose = w["available_under"], w["not_available_under"]
        self.assertIn("A", win["award_students"])
        self.assertNotIn("A", lose["award_students"])
        # each witness award list is coherent: points match a single
        # finalization's per-student picture
        for snap in (win, lose):
            choices = snap["finalization"]
            self.assertEqual(set(choices), {"E"})
            res = evaluate_honor(self.cat, _scenario_students("E")["A"], self.req,
                                 ["HONOR"], fixed_choices=choices)
            self.assertEqual(snap["per_student"]["A"]["honor_points"],
                             res.honor_points if res.eligible else None)

    def test_verifying_never_mixes_finalizations(self):
        # A "mixed" ranking (A's best from one finalization, B's best from
        # another) must never be produced: every witness award list recomputes
        # all students under ONE finalization.
        draft = CharterDraft.from_catalog(self.cat, ["E", "F"])
        rep = assess_scholarship(self.cat, self._entries("F"), ["HONOR"], draft,
                                 quota=2, tie_order=["C", "B", "A"])
        for s in rep["students"]:
            if s["status"] != "some":
                continue
            for key in ("available_under", "not_available_under"):
                snap = s["witness"][key]
                choices = snap["finalization"]
                # recompute the whole cohort under this one finalization
                recomputed = []
                for entry in self._entries("F"):
                    res = evaluate_honor(self.cat, entry.records, entry.requirements,
                                         ["HONOR"], fixed_choices=choices)
                    recomputed.append((entry.student_id, res.honor_points if res.eligible else None))
                # award points in the snapshot agree with the recomputation
                by_id = dict(recomputed)
                for aw in snap["award_list"]:
                    self.assertEqual(aw["honor_points"], by_id[aw["student_id"]])

    def test_never_student_has_no_witness(self):
        draft = CharterDraft.from_catalog(self.cat, ["E"])
        rep = assess_scholarship(self.cat, self._entries("E"), ["HONOR"], draft,
                                 quota=2, tie_order=["C", "B", "A"])
        b = next(s for s in rep["students"] if s["student_id"] == "B")
        self.assertEqual(b["status"], "never")
        self.assertNotIn("witness", b)

    def test_tie_order_unknown_student_rejected(self):
        from gradaudit.errors import SchemaError
        draft = CharterDraft.from_catalog(self.cat, ["E"])
        with self.assertRaises(SchemaError):
            assess_scholarship(self.cat, self._entries("E"), ["HONOR"], draft,
                               quota=2, tie_order=["C", "B", "A", "GHOST"])

    def test_scale_guards(self):
        from gradaudit.errors import SchemaError
        draft = CharterDraft.from_catalog(self.cat, ["E"])
        with self.assertRaises(SchemaError):  # quota must be positive
            assess_scholarship(self.cat, self._entries("E"), ["HONOR"], draft,
                               quota=0, tie_order=["C", "B", "A"])
        # 4 candidates for a covered code exceeds the <=3 contract
        big_cat = {
            "format": "course-catalog/1", "revision": 1,
            "courses": [{"course_id": f"X{i}", "categories": ["HONOR"]} for i in range(4)],
            "equivalences": {"BIG": ["X0", "X1", "X2", "X3"]},
        }
        c = parse_catalog(big_cat)
        d = CharterDraft.from_catalog(c, ["BIG"])
        r = parse_records(_student("S", [{"record_id": "r", "external_course": "BIG", "points": 1}]))
        q = parse_requirements(_scenario_req())
        with self.assertRaises(SchemaError):
            assess_scholarship(c, [StudentEntry("S", r, q)], ["HONOR"], d, quota=1, tie_order=["S"])

    def test_award_list_plus_alternate_graduation_for_honor(self):
        # The honor-optimal graduation allocation may differ from the solver's
        # default and must still verify.
        from gradaudit.honor import graduation_plan
        cat = parse_catalog({
            "format": "course-catalog/1", "revision": 1,
            "courses": [
                {"course_id": "CORE_HON", "categories": ["CORE", "HONOR"]},
                {"course_id": "CORE_ONLY", "categories": ["CORE"]},
                {"course_id": "HON", "categories": ["HONOR"]},
            ],
            "equivalences": {"A": ["CORE_HON"], "B": ["CORE_ONLY"], "C": ["HON"]},
        })
        req = parse_requirements({"request_id": "Q", "revision": 1, "groups": [
            {"group_id": "CORE", "allowed_categories": ["CORE"], "minimum_points": 3}]})
        recs = parse_records(_student("S", [
            {"record_id": "R1", "external_course": "A", "points": 3},
            {"record_id": "R2", "external_course": "B", "points": 3},
            {"record_id": "R3", "external_course": "C", "points": 2},
        ]))
        res = evaluate_honor(cat, recs, req, ["HONOR"])
        self.assertEqual(res.honor_points, 5)  # R1+R3 honor, R2 graduates
        self.assertTrue(
            verify_plan_document(graduation_plan(res, recs, req).as_document(), cat, recs, req).accepted
        )


class TestScholarshipStore(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.import_catalog(_scenario_catalog())
        self.req = _scenario_req()
        self.store.import_requirements(self.req)
        self.students = _scenario_students("E")
        # distinct records revisions (one per student)
        self.revs = {}
        for i, sid in enumerate(["A", "B", "C"], start=1):
            doc = json.loads(json.dumps({
                "student_id": sid, "revision": i,
                "records": [
                    {"record_id": r.record_id, "external_course": r.external_course, "points": r.points}
                    for r in self.students[sid].records.values()
                ],
            }))
            self.store.import_records(doc)
            self.revs[sid] = i

    def tearDown(self):
        self.store.close()

    def _entries(self):
        return [{"student_id": sid, "records_revision": self.revs[sid], "requirements_revision": 1}
                for sid in ("C", "B", "A")]

    def test_assess_is_read_only_and_pins_identity(self):
        # confirm a real graduation audit first
        self.store.register_request(request_id="Q", catalog_revision=1,
                                    records_revision=self.revs["A"], requirements_revision=1)
        plan = {"format": "course-allocation/1", "student_id": "A",
                "catalog_revision": 1, "records_revision": self.revs["A"],
                "request_id": "Q", "requirements_revision": 1,
                "assignments": [{"record_id": "A_G", "course_id": "GC_A", "group_id": "GRAD"}]}
        prep = self.store.prepare_audit(plan, request_id="Q", catalog_revision=1,
                                        records_revision=self.revs["A"], requirements_revision=1)
        self.assertTrue(prep["accepted"])
        self.store.confirm_audit(request_id="Q")
        before = self.store.confirmation_status("Q")
        self.assertTrue(before["confirmed"])

        rep = self.store.assess_scholarship(
            entries=self._entries(), covered_codes=["E"], honor_categories=["HONOR"],
            quota=2, tie_order=["C", "B", "A"],
        )
        after = self.store.confirmation_status("Q")
        self.assertTrue(after["confirmed"])
        self.assertEqual(before["confirmation_id"], after["confirmation_id"])
        self.assertTrue(rep["read_only"])
        self.assertEqual({p["student_id"] for p in rep["pinned_revisions"]}, {"A", "B", "C"})
        for p in rep["pinned_revisions"]:
            self.assertTrue(p["records_hash"])

    def test_award_list_issued_under_canonical_finalization(self):
        rep = self.store.assess_scholarship(
            entries=self._entries(), covered_codes=["E"], honor_categories=["HONOR"],
            quota=2, tie_order=["C", "B", "A"],
        )
        award = self.store.issue_award_list(rep)
        self.assertEqual(award["finalization"], rep["canonical_finalization"])
        self.assertEqual({a["student_id"] for a in award["awards"]}, {"A", "C"})

    def test_issue_refuses_hand_edited_report(self):
        from gradaudit.errors import SchemaError
        rep = self.store.assess_scholarship(
            entries=self._entries(), covered_codes=["E"], honor_categories=["HONOR"],
            quota=2, tie_order=["C", "B", "A"],
        )
        rep2 = dict(rep); rep2.pop("read_only")
        with self.assertRaises(SchemaError):
            self.store.issue_award_list(rep2)

    def test_entry_revision_must_belong_to_student(self):
        from gradaudit.errors import SchemaError
        bad = [{"student_id": "B", "records_revision": self.revs["A"], "requirements_revision": 1}]
        with self.assertRaises(SchemaError):
            self.store.assess_scholarship(
                entries=bad, covered_codes=["E"], honor_categories=["HONOR"],
                quota=1, tie_order=["B"],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)