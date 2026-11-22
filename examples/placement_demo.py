#!/usr/bin/env python3
"""End-to-end demonstration of the internship placement layer.

Uses the qing/bai materials in ``materials/internship`` and runs the whole
contracted story:

  1. department-side joint eligibility reviews (reserved records + a legal
     graduation plan that the *old* course-allocation verifier still accepts);
  2. act 1 -- both students take their first choice, total subsidy 0; the
     optimizer sees two stable rosters and picks the cheaper one, not the first;
  3. act 2 -- qing's catalog drops the P mapping: qing loses P, the only stable
     roster crosses them at cost 2;
  4. act 3 -- P stops recruiting: Q keeps qing, bai is unassigned;
  5. the independent verifiers re-check every review and the final roster;
  6. the disjointness proof -- one dual-category record cannot serve both a
     post and graduation.

Usage:  PYTHONPATH=. python3 examples/placement_demo.py
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from gradaudit.model import Records, parse_catalog, parse_plan, parse_records, parse_requirements
from gradaudit.verify import verify_plan
from gradaudit.placement import (
    ReviewPool,
    StudentLibrary,
    build_matching_input,
    evaluate_eligibility,
    parse_posts,
    parse_preferences,
    parse_stations,
    parse_subsidies,
    solve_matching,
    verify_review,
    verify_roster,
)
from gradaudit import Store

MAT = os.path.join(ROOT, "materials", "internship")


def load(name):
    with open(os.path.join(MAT, name), encoding="utf-8") as fh:
        return json.load(fh)


def banner(text):
    print("\n" + "=" * 74)
    print(text)
    print("=" * 74)


def make_pool(qing_catalog_file):
    pool = ReviewPool()
    for student, cat_file, rec_file, req_file in (
        ("QING", qing_catalog_file, "qing_records.json", "qing_requirements.json"),
        ("BAI", "bai_catalog_rev1.json", "bai_records.json", "bai_requirements.json"),
    ):
        cat_doc, rec_doc, req_doc = load(cat_file), load(rec_file), load(req_file)
        store = Store(":memory:")
        store.import_catalog(cat_doc)
        store.import_records(rec_doc)
        store.import_requirements(req_doc)
        pool.register_student(StudentLibrary(
            student_id=student, store=store,
            catalog_revision=cat_doc["revision"],
            records_revision=rec_doc["revision"],
            requirements_revision=req_doc["revision"],
        ))
    return pool


def run_act(title, station_file, qing_catalog_file):
    banner(title)
    posts = parse_posts(load("posts.json"))
    pool = make_pool(qing_catalog_file)
    reviews = []
    for student in pool.student_ids:
        library = pool._require(student)
        for post in posts.values():
            result = pool.review(student, post)
            print(f"  review {student} x {post.post_id}: {result.decision}", end="")
            if result.decision == "eligible":
                reserved = {r["record_id"] for r in result.reservation}
                used = [a["record_id"] for a in result.graduation_plan["assignments"]]
                print(f"  reserve={sorted(reserved)} graduation_used={used}")
                # independent re-verification, including the old verify entry
                verdict = pool.verify_review_document(result.as_document(), posts)
                assert verdict.accepted, verdict.reasons
                reviews.append(result.as_document())
            else:
                print(f"  reason={result.reasons[0]['code']}")
    pool.close()

    stations = parse_stations(load(station_file))
    preferences = parse_preferences(load("preferences.json"),
                                    known_stations=frozenset(stations))
    subsidies = parse_subsidies(load("subsidies.json"),
                                known_stations=frozenset(stations))
    inp = build_matching_input(preferences, stations, reviews, subsidies)
    roster = solve_matching(inp)
    print(f"  roster: placements={roster.placements} subsidy={roster.total_subsidy}")
    print(f"          assignment={roster.assignment}")
    print(f"          unassigned={roster.unassigned} blocking_pairs={roster.blocking_pairs}")
    verdict = verify_roster(roster.as_document(inp), preferences, stations, subsidies, reviews)
    print(f"  independent roster verify: accepted={verdict.accepted} optimum={verdict.optimum}")
    assert verdict.accepted, verdict.reasons
    return roster


def main() -> int:
    posts = parse_posts(load("posts.json"))

    banner("0. One dual-category record cannot serve both a post and graduation")
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
    result = evaluate_eligibility(catalog, records, requirements, posts["P"])
    print(f"  decision={result.decision} reason={result.reasons[0]['code']}")
    independent = verify_review(result.as_document(), catalog, records, requirements, posts)
    print(f"  independent verifier accepts the refusal: {independent.accepted}")
    assert result.decision == "ineligible" and independent.accepted

    run_act("1. Open stations: first choices, subsidy 0 (two stable rosters, cheapest wins)",
            "stations_open.json", "qing_catalog_rev1.json")
    run_act("2. Qing's P mapping revoked: crossed roster, subsidy 2",
            "stations_open.json", "qing_catalog_rev2_no_p.json")
    run_act("3. P stops recruiting: Q keeps qing, bai unassigned",
            "stations_p_closed.json", "qing_catalog_rev1.json")

    banner("4. The graduation plan still passes the ORIGINAL verify entry point")
    cat = parse_catalog(load("qing_catalog_rev1.json"))
    rec = parse_records(load("qing_records.json"))
    req = parse_requirements(load("qing_requirements.json"))
    review = evaluate_eligibility(cat, rec, req, posts["P"])
    reserved = {r["record_id"] for r in review.reservation}
    remaining = Records(rec.student_id, rec.revision,
                        {k: v for k, v in rec.records.items() if k not in reserved})
    old_verdict = verify_plan(parse_plan(review.graduation_plan), cat, remaining, req)
    print(f"  old verify accepted={old_verdict.accepted} totals={old_verdict.group_totals} "
          f"used={old_verdict.used_records} reserved={sorted(reserved)}")
    assert old_verdict.accepted and not (set(old_verdict.used_records) & reserved)

    print("\nOK: all placement demonstrations completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
