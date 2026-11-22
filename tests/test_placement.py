"""Test suite for the gradaudit.placement internship layer.

Covers the whole prompt:

* joint post/graduation eligibility with strictly disjoint records;
* independently re-verifiable reviews, including the old course-allocation
  verifier entry point on the embedded graduation plan;
* the qing/bai three-act scenario (first-choice cost 0, mapping revocation
  cost 2, station closure with one unplaced student);
* the proof that one dual-category record cannot serve both a post and
  graduation;
* exact stable matching against brute-force enumeration on random instances,
  including the "first stable roster is not the cheapest" trap;
* scale guards (24 students, 10 stations, 5 picks).
"""

from __future__ import annotations

import copy
import itertools
import json
import os
import random
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gradaudit.model import (
    Records,
    parse_catalog,
    parse_plan,
    parse_records,
    parse_requirements,
)
from gradaudit.verify import verify_plan
from gradaudit.placement import (
    MAX_STATION_PICKS,
    MAX_STATIONS,
    ReviewPool,
    Station,
    StudentLibrary,
    build_matching_input,
    evaluate_eligibility,
    find_blocking_pairs,
    parse_posts,
    parse_preferences,
    parse_stations,
    parse_subsidies,
    solve_matching,
    verify_review,
    verify_roster,
)
from gradaudit.placement.documents import (
    GRADUATION_BLOCKED_BY_RESERVATION,
    GRADUATION_NOT_FEASIBLE,
    POST_REQUIREMENTS_UNMET,
)
from gradaudit import Store

MAT = os.path.join(ROOT, "materials", "internship")


def load(name):
    with open(os.path.join(MAT, name), encoding="utf-8") as fh:
        return json.load(fh)


def posts_doc():
    return parse_posts(load("posts.json"))


def qing(rev_file="qing_catalog_rev1.json"):
    return (
        parse_catalog(load(rev_file)),
        parse_records(load("qing_records.json")),
        parse_requirements(load("qing_requirements.json")),
    )


def bai():
    return (
        parse_catalog(load("bai_catalog_rev1.json")),
        parse_records(load("bai_records.json")),
        parse_requirements(load("bai_requirements.json")),
    )


def make_pool(*pairs):
    """pairs: (student_id, catalog_doc, records_doc, requirements_doc)."""
    pool = ReviewPool()
    for student_id, cat_doc, rec_doc, req_doc in pairs:
        store = Store(":memory:")
        store.import_catalog(cat_doc)
        store.import_records(rec_doc)
        store.import_requirements(req_doc)
        pool.register_student(StudentLibrary(
            student_id=student_id, store=store,
            catalog_revision=cat_doc["revision"],
            records_revision=rec_doc["revision"],
            requirements_revision=req_doc["revision"],
        ))
    return pool


def all_reviews(pool, posts):
    docs = []
    for student in pool.student_ids:
        for post in posts.values():
            result = pool.review(student, post)
            docs.append(result.as_document())
    return docs


# =========================================================================== #
# joint eligibility and its independent review verifier
# =========================================================================== #
class TestEligibility(unittest.TestCase):
    def test_each_qing_bai_pair_is_eligible_under_rev1(self):
        posts = posts_doc()
        for cat, rec, req, name in [(*qing(), "QING"), (*bai(), "BAI")]:
            for pid in ("P", "Q"):
                with self.subTest(student=name, post=pid):
                    result = evaluate_eligibility(cat, rec, req, posts[pid])
                    self.assertEqual(result.decision, "eligible")
                    # the whole dual record RD is reserved; RG stays for G
                    self.assertEqual([r["record_id"] for r in result.reservation], ["RD"])
                    self.assertEqual(result.reservation_total_points, 3)
                    reserved = {"RD"}
                    remaining = Records(rec.student_id, rec.revision,
                                        {k: v for k, v in rec.records.items() if k not in reserved})
                    verdict = verify_plan(parse_plan(result.graduation_plan), cat, remaining, req)
                    self.assertTrue(verdict.accepted, verdict.reasons)
                    self.assertEqual(verdict.used_records, ["RG"])
                    # disjointness: no record appears on both sides
                    self.assertFalse(set(verdict.used_records) & reserved)

    def test_review_verifier_independently_accepts_and_rejects_tampering(self):
        posts = posts_doc()
        cat, rec, req = qing()
        good = evaluate_eligibility(cat, rec, req, posts["P"]).as_document()
        self.assertTrue(verify_review(good, cat, rec, req, posts).accepted)

        # tamper the embedded post: must be located
        tampered = copy.deepcopy(good)
        tampered["post"]["required_points"] = 99
        self.assertFalse(verify_review(tampered, cat, rec, req, posts).accepted)

        # forge a graduation plan that reuses the reserved record RD.
        # The course must stay category-valid for group G (hence the dual
        # P/G catalog), so the failure is the reuse itself, not a category rule.
        dual_cat = parse_catalog({
            "format": "course-catalog/1", "revision": 1,
            "courses": [
                {"course_id": "LC_G", "categories": ["G"]},
                {"course_id": "LC_PG", "categories": ["P", "G"]},
            ],
            "equivalences": {"EXT_G": ["LC_G"], "EXT_DUAL": ["LC_PG"]},
        })
        dual_result = evaluate_eligibility(dual_cat, rec, req, posts["P"])
        self.assertEqual(dual_result.decision, "eligible")
        forged = dual_result.as_document()
        forged["graduation_plan"]["assignments"].append(
            {"record_id": "RD", "course_id": "LC_PG", "group_id": "GG"}
        )
        verdict = verify_review(forged, dual_cat, rec, req, posts)
        self.assertFalse(verdict.accepted)
        codes = {r["code"] for r in verdict.reasons}
        self.assertIn("review_record_split_between_post_and_graduation", codes)

    def test_revoked_p_mapping_makes_qing_ineligible_for_p_only(self):
        posts = posts_doc()
        cat2, rec, req = qing("qing_catalog_rev2_no_p.json")
        p = evaluate_eligibility(cat2, rec, req, posts["P"])
        q = evaluate_eligibility(cat2, rec, req, posts["Q"])
        self.assertEqual(p.decision, "ineligible")
        self.assertEqual(p.reasons[0]["code"], POST_REQUIREMENTS_UNMET)
        self.assertEqual(q.decision, "eligible")
        self.assertEqual(q.reservation[0]["course_id"], "LC_Q")
        # the negative decision itself must survive independent verification
        verdict = verify_review(p.as_document(), cat2, rec, req, posts)
        self.assertTrue(verdict.accepted, verdict.reasons)

    def test_wrongly_issued_refusal_is_caught_independently(self):
        posts = posts_doc()
        cat, rec, req = qing()
        good = evaluate_eligibility(cat, rec, req, posts["P"]).as_document()
        fake_refusal = copy.deepcopy(good)
        fake_refusal["decision"] = "ineligible"
        fake_refusal["reasons"] = [{
            "code": GRADUATION_BLOCKED_BY_RESERVATION,
            "message": "fabricated refusal",
        }]
        verdict = verify_review(fake_refusal, cat, rec, req, posts)
        self.assertFalse(verdict.accepted)
        self.assertIn("review_ineligible_decision_unsupported",
                      {r["code"] for r in verdict.reasons})

    def test_one_dual_category_record_cannot_serve_post_and_graduation(self):
        # One record only, 3 points, mapping to a P/G dual course; both the
        # graduation group G and post P need 3 of it.  No second record exists.
        catalog = parse_catalog({
            "format": "course-catalog/1", "revision": 1,
            "courses": [{"course_id": "C_PG", "categories": ["P", "G"]}],
            "equivalences": {"EXT_DUAL": ["C_PG"]},
        })
        records = parse_records({"student_id": "S", "revision": 1, "records": [
            {"record_id": "R1", "external_course": "EXT_DUAL", "points": 3},
        ]})
        requirements = parse_requirements({"request_id": "REQ", "revision": 1, "groups": [
            {"group_id": "GG", "allowed_categories": ["G"], "minimum_points": 3},
        ]})
        post = posts_doc()["P"]
        result = evaluate_eligibility(catalog, records, requirements, post)
        self.assertEqual(result.decision, "ineligible")
        self.assertEqual(result.reasons[0]["code"], GRADUATION_BLOCKED_BY_RESERVATION)
        # independent verifier agrees the refusal is genuine
        self.assertTrue(
            verify_review(result.as_document(), catalog, records, requirements, posts_doc()).accepted
        )

    def test_graduation_impossible_even_without_reservation(self):
        catalog = parse_catalog({
            "format": "course-catalog/1", "revision": 1,
            "courses": [{"course_id": "C_P", "categories": ["P"]}],
            "equivalences": {"EXT_P": ["C_P"]},
        })
        records = parse_records({"student_id": "S", "revision": 1, "records": [
            {"record_id": "R1", "external_course": "EXT_P", "points": 3},
        ]})
        requirements = parse_requirements({"request_id": "REQ", "revision": 1, "groups": [
            {"group_id": "GG", "allowed_categories": ["G"], "minimum_points": 3},
        ]})
        result = evaluate_eligibility(catalog, records, requirements, posts_doc()["P"])
        self.assertEqual(result.decision, "ineligible")
        self.assertEqual(result.reasons[0]["code"], GRADUATION_NOT_FEASIBLE)

    def test_reviews_are_pair_specific_no_cross_post_retention(self):
        # A review for P and a review for Q each reserve their own RD choice;
        # nothing requires one student to keep both reservations at once.
        posts = posts_doc()
        pool = make_pool(("QING", load("qing_catalog_rev1.json"),
                          load("qing_records.json"), load("qing_requirements.json")))
        p = pool.review("QING", posts["P"])
        q = pool.review("QING", posts["Q"])
        self.assertEqual(p.reservation[0]["course_id"], "LC_P")
        self.assertEqual(q.reservation[0]["course_id"], "LC_Q")
        self.assertTrue(pool.verify_review_document(p.as_document(), posts).accepted)
        self.assertTrue(pool.verify_review_document(q.as_document(), posts).accepted)
        pool.close()


# =========================================================================== #
# the qing/bai three-act scenario
# =========================================================================== #
class TestQingBaiScenario(unittest.TestCase):
    def setUp(self):
        self.posts = posts_doc()
        self.prefs = parse_preferences(load("preferences.json"))
        self.subs = parse_subsidies(load("subsidies.json"))

    def _match(self, stations_doc, review_docs):
        stations = parse_stations(stations_doc)
        prefs = parse_preferences(load("preferences.json"),
                                  known_stations=frozenset(stations))
        subs = parse_subsidies(load("subsidies.json"),
                               known_stations=frozenset(stations))
        inp = build_matching_input(prefs, stations, review_docs, subs)
        return stations, prefs, subs, inp, solve_matching(inp)

    def test_act1_first_choices_cost_zero(self):
        pool = make_pool(
            ("QING", load("qing_catalog_rev1.json"), load("qing_records.json"),
             load("qing_requirements.json")),
            ("BAI", load("bai_catalog_rev1.json"), load("bai_records.json"),
             load("bai_requirements.json")),
        )
        docs = all_reviews(pool, self.posts)
        stations, prefs, subs, inp, roster = self._match(load("stations_open.json"), docs)
        self.assertEqual(roster.assignment, {"BAI": "Q", "QING": "P"})
        self.assertEqual(roster.placements, 2)
        self.assertEqual(roster.total_subsidy, 0)
        self.assertEqual(roster.unassigned, [])
        self.assertTrue(roster.stable)
        verdict = verify_roster(roster.as_document(inp), prefs, stations, subs, docs)
        self.assertTrue(verdict.accepted, verdict.reasons)
        pool.close()

    def test_act1_two_stable_rosters_cheapest_is_chosen_not_first(self):
        # Independent brute force enumerates *every* feasible roster: both
        # first-choice (cost 0) and crossed (cost 2) rosters are stable, and
        # the optimizer must return the cost-0 one.
        pool = make_pool(
            ("QING", load("qing_catalog_rev1.json"), load("qing_records.json"),
             load("qing_requirements.json")),
            ("BAI", load("bai_catalog_rev1.json"), load("bai_records.json"),
             load("bai_requirements.json")),
        )
        docs = all_reviews(pool, self.posts)
        stations = parse_stations(load("stations_open.json"))
        prefs = parse_preferences(load("preferences.json"),
                                  known_stations=frozenset(stations))
        subs = parse_subsidies(load("subsidies.json"),
                               known_stations=frozenset(stations))
        stable_rosters = list(_brute_force_stable(prefs, stations, subs, docs))
        costs = sorted((sum(subs[sid][s] for s, sid in a.items() if sid), a)
                       for a in stable_rosters)
        self.assertEqual(len(costs), 2)
        self.assertEqual([c for c, _ in costs], [0, 2])
        inp = build_matching_input(prefs, stations, docs, subs)
        roster = solve_matching(inp)
        self.assertEqual(roster.total_subsidy, 0)
        pool.close()

    def test_act2_revoked_mapping_cost_two(self):
        pool = make_pool(
            ("QING", load("qing_catalog_rev2_no_p.json"), load("qing_records.json"),
             load("qing_requirements.json")),
            ("BAI", load("bai_catalog_rev1.json"), load("bai_records.json"),
             load("bai_requirements.json")),
        )
        docs = all_reviews(pool, self.posts)
        stations, prefs, subs, inp, roster = self._match(load("stations_open.json"), docs)
        self.assertEqual(roster.assignment, {"BAI": "P", "QING": "Q"})
        self.assertEqual(roster.total_subsidy, 2)
        self.assertTrue(roster.stable)
        verdict = verify_roster(roster.as_document(inp), prefs, stations, subs, docs)
        self.assertTrue(verdict.accepted)
        pool.close()

    def test_act3_p_closed_qing_kept_at_q_bai_unassigned(self):
        pool = make_pool(
            ("QING", load("qing_catalog_rev1.json"), load("qing_records.json"),
             load("qing_requirements.json")),
            ("BAI", load("bai_catalog_rev1.json"), load("bai_records.json"),
             load("bai_requirements.json")),
        )
        docs = all_reviews(pool, self.posts)
        stations, prefs, subs, inp, roster = self._match(load("stations_p_closed.json"), docs)
        self.assertEqual(roster.assignment, {"BAI": None, "QING": "Q"})
        self.assertEqual(roster.unassigned, ["BAI"])
        self.assertEqual(roster.placements, 1)
        self.assertTrue(roster.stable)
        verdict = verify_roster(roster.as_document(inp), prefs, stations, subs, docs)
        self.assertTrue(verdict.accepted)
        # swapping BAI into Q would create a blocking pair (Q prefers QING)
        swapped = {"BAI": "Q", "QING": None}
        edges = inp.admissible_edges()
        rank = {sid: {s: i for i, s in enumerate(st.ranking)}
                for sid, st in stations.items()}
        admits = {"P": [], "Q": ["BAI"]}
        blockers = find_blocking_pairs(swapped, inp, edges, rank, admits)
        self.assertTrue(any(b["student_id"] == "QING" and b["station_id"] == "Q"
                            for b in blockers))
        pool.close()


# =========================================================================== #
# blocking pairs and independent roster verification details
# =========================================================================== #
class TestRosterVerification(unittest.TestCase):
    def _base(self):
        pool = make_pool(
            ("QING", load("qing_catalog_rev1.json"), load("qing_records.json"),
             load("qing_requirements.json")),
            ("BAI", load("bai_catalog_rev1.json"), load("bai_records.json"),
             load("bai_requirements.json")),
        )
        docs = all_reviews(pool, posts_doc())
        stations = parse_stations(load("stations_open.json"))
        prefs = parse_preferences(load("preferences.json"),
                                  known_stations=frozenset(stations))
        subs = parse_subsidies(load("subsidies.json"),
                               known_stations=frozenset(stations))
        return pool, docs, stations, prefs, subs

    def test_unapproved_pair_rejected(self):
        pool, docs, stations, prefs, subs = self._base()
        # drop QING's P approval, then hand in a roster using that pair anyway
        reduced = [d for d in docs if not (d["student_id"] == "QING" and d["post_id"] == "P")]
        inp = build_matching_input(prefs, stations, reduced, subs)
        roster = solve_matching(inp)
        doc = roster.as_document(inp)
        # force the unapproved pair into the document
        doc["assignments"] = [
            {"student_id": "QING", "station_id": "P"},
            {"student_id": "BAI", "station_id": "Q"},
        ]
        verdict = verify_roster(doc, prefs, stations, subs, reduced)
        self.assertFalse(verdict.accepted)
        self.assertIn("roster_pair_not_approved", {r["code"] for r in verdict.reasons})
        pool.close()

    def test_quota_exceeded_and_subsidy_recomputed(self):
        pool, docs, stations, prefs, subs = self._base()
        inp = build_matching_input(prefs, stations, docs, subs)
        roster = solve_matching(inp).as_document(inp)
        roster["assignments"].append({"student_id": "QING", "station_id": "Q"})
        verdict = verify_roster(roster, prefs, stations, subs, docs)
        codes = {r["code"] for r in verdict.reasons}
        self.assertIn("roster_student_assigned_twice", codes)
        # money on the roster is never trusted
        roster2 = solve_matching(build_matching_input(prefs, stations, docs, subs)).as_document(inp)
        roster2["objective"]["total_subsidy"] = 999
        verdict2 = verify_roster(roster2, prefs, stations, subs, docs)
        self.assertIn("roster_subsidy_mismatch", {r["code"] for r in verdict2.reasons})
        self.assertEqual(verdict2.recomputed_subsidy, 0)
        pool.close()

    def test_stable_but_suboptimal_roster_is_locatable(self):
        pool, docs, stations, prefs, subs = self._base()
        # the crossed roster is stable but costs 2 while cost 0 is available
        crossed = {
            "format": "internship-roster/1",
            "stable": True,
            "objective": {"placements": 2, "total_subsidy": 2},
            "assignments": [
                {"student_id": "QING", "station_id": "Q"},
                {"student_id": "BAI", "station_id": "P"},
            ],
            "unassigned": [],
            "admissions": [],
            "blocking_pairs": [],
        }
        verdict = verify_roster(crossed, prefs, stations, subs, docs)
        self.assertFalse(verdict.accepted)
        self.assertIn("roster_not_cheapest", {r["code"] for r in verdict.reasons})
        self.assertEqual(verdict.optimum["minimum_subsidy_at_max"], 0)
        pool.close()


# =========================================================================== #
# exactness: solver + independent verifier vs brute force, random instances
# =========================================================================== #
def _brute_force_stable(prefs, stations, subsidies, reviews):
    """Yield every stable feasible assignment as {student: station or None}."""
    approved: dict = {}
    for d in reviews:
        if d["decision"] == "eligible":
            approved.setdefault(d["student_id"], set()).add(d["post_id"])
    edges = {}
    for student, wish in prefs.items():
        edges[student] = tuple(
            sid for sid in wish
            if not stations[sid].closed
            and student in stations[sid].ranking
            and sid in approved.get(student, frozenset())
        )
    choices = [[None, *edges[s]] for s in sorted(prefs)]
    for combo in itertools.product(*choices):
        assignment = dict(zip(sorted(prefs), combo))
        admits = {sid: [] for sid in stations}
        ok = True
        for student, sid in assignment.items():
            if sid is not None:
                if len(admits[sid]) >= stations[sid].quota:
                    ok = False
                    break
                admits[sid].append(student)
        if not ok:
            continue
        rank = {sid: {s: i for i, s in enumerate(st.ranking)} for sid, st in stations.items()}
        blockers = find_blocking_pairs(assignment, _Input(prefs, stations), edges, rank, admits)
        if not blockers:
            yield assignment


class _Input:
    def __init__(self, preferences, stations):
        self.preferences = preferences
        self.stations = stations


class TestMatchingExactness(unittest.TestCase):
    def test_random_instances_against_brute_force(self):
        rng = random.Random(20261122)
        for trial in range(120):
            n_students = rng.randint(2, 5)
            n_stations = rng.randint(2, 3)
            students = [f"S{i}" for i in range(n_students)]
            stations = {}
            for j in range(n_stations):
                sid = f"T{j}"
                ranked = rng.sample(students, rng.randint(1, len(students)))
                stations[sid] = Station(
                    station_id=sid,
                    quota=rng.randint(1, max(1, len(students))),
                    ranking=tuple(ranked),
                    closed=False,
                )
            prefs = {}
            for s in students:
                listed = [sid for sid in stations if s in stations[sid].ranking]
                rng.shuffle(listed)
                prefs[s] = tuple(listed[: min(len(listed), MAX_STATION_PICKS)])
            prefs = {s: w for s, w in prefs.items() if w}
            if not prefs:
                continue
            # every mutually listed pair gets an eligible review with some
            # probability, so approval becomes a real constraint
            reviews = []
            for s, wish in prefs.items():
                for sid in wish:
                    if rng.random() < 0.8:
                        reviews.append({"format": "internship-review/1", "decision": "eligible",
                                        "student_id": s, "post_id": sid})
            subsidies = {sid: {s: rng.randint(0, 5) for s in prefs} for sid in stations}
            inp = build_matching_input(prefs, stations, reviews, subsidies)
            roster = solve_matching(inp)

            stable = list(_brute_force_stable(prefs, stations, subsidies, reviews))
            if not stable:
                self.assertFalse(roster.stable)
                continue
            best_placed = max(sum(1 for v in a.values() if v) for a in stable)
            best_cost = min(
                sum(subsidies[sid][s] for s, sid in a.items() if sid)
                for a in stable if sum(1 for v in a.values() if v) == best_placed
            )
            with self.subTest(trial=trial):
                self.assertTrue(roster.stable)
                self.assertEqual(roster.placements, best_placed)
                self.assertEqual(roster.total_subsidy, best_cost)
                verdict = verify_roster(roster.as_document(inp), prefs, stations, subsidies, reviews)
                self.assertTrue(verdict.accepted, verdict.reasons)

    def test_first_stable_is_not_cheapest_trap(self):
        # The qing/bai preference structure admits TWO stable rosters (cost 0
        # and cost 2); a search that signed the first stable leaf it reached
        # could emit the cost-2 one.  Prove both exist by brute force, prove
        # the optimizer takes cost 0, and prove the independent verifier
        # locates a cost-2 signature as roster_not_cheapest.
        pool = make_pool(
            ("QING", load("qing_catalog_rev1.json"), load("qing_records.json"),
             load("qing_requirements.json")),
            ("BAI", load("bai_catalog_rev1.json"), load("bai_records.json"),
             load("bai_requirements.json")),
        )
        docs = all_reviews(pool, posts_doc())
        stations = parse_stations(load("stations_open.json"))
        prefs = parse_preferences(load("preferences.json"),
                                  known_stations=frozenset(stations))
        subsidies = parse_subsidies(load("subsidies.json"),
                                    known_stations=frozenset(stations))
        stable = list(_brute_force_stable(prefs, stations, subsidies, docs))
        self.assertEqual(len(stable), 2)
        costs = {sum(subsidies[sid][s] for s, sid in a.items() if sid) for a in stable}
        self.assertEqual(costs, {0, 2})

        inp = build_matching_input(prefs, stations, docs, subsidies)
        roster = solve_matching(inp)
        self.assertEqual(roster.placements, 2)
        self.assertEqual(roster.total_subsidy, 0)

        expensive = {
            "format": "internship-roster/1",
            "stable": True,
            "objective": {"placements": 2, "total_subsidy": 2},
            "assignments": [
                {"student_id": "QING", "station_id": "Q"},
                {"student_id": "BAI", "station_id": "P"},
            ],
            "unassigned": [],
            "admissions": [],
            "blocking_pairs": [],
        }
        verdict = verify_roster(expensive, prefs, stations, subsidies, docs)
        self.assertFalse(verdict.accepted)
        self.assertIn("roster_not_cheapest", {r["code"] for r in verdict.reasons})
        self.assertEqual(verdict.optimum["minimum_subsidy_at_max"], 0)
        pool.close()


# =========================================================================== #
# scale guards and input contracts
# =========================================================================== #
class TestScaleGuards(unittest.TestCase):
    def test_station_cap(self):
        doc = {"format": "station-profile/1", "stations": [
            {"station_id": f"T{i}", "quota": 1, "ranking": ["S1"]}
            for i in range(MAX_STATIONS + 1)
        ]}
        from gradaudit.errors import SchemaError
        with self.assertRaises(SchemaError):
            parse_stations(doc)

    def test_preference_cap(self):
        stations = {f"T{i}": Station(f"T{i}", 1, ("S1",), False)
                    for i in range(MAX_STATION_PICKS + 1)}
        doc = {"format": "student-preferences/1", "preferences": [
            {"student_id": "S1", "stations": [f"T{i}" for i in range(MAX_STATION_PICKS + 1)]}
        ]}
        from gradaudit.errors import SchemaError
        with self.assertRaises(SchemaError):
            parse_preferences(doc, known_stations=frozenset(stations))

    def test_missing_subsidy_rejected(self):
        stations = {"T1": Station("T1", 1, ("S1",), False)}
        prefs = {"S1": ("T1",)}
        reviews = [{"decision": "eligible", "student_id": "S1", "post_id": "T1"}]
        from gradaudit.errors import SchemaError
        with self.assertRaises(SchemaError):
            build_matching_input(prefs, stations, reviews, {"T1": {}})

    def test_strict_orders_validated(self):
        from gradaudit.errors import SchemaError
        with self.assertRaises(SchemaError):
            parse_stations({"format": "station-profile/1", "stations": [
                {"station_id": "T1", "quota": 1, "ranking": ["S1", "S1"]}]})
        with self.assertRaises(SchemaError):
            parse_preferences({"format": "student-preferences/1", "preferences": [
                {"student_id": "S1", "stations": ["T1", "T1"]}]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
