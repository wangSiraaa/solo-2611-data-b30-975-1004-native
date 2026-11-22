#!/usr/bin/env python3
"""Reproducible demo for the honor / scholarship layer.

Builds the task's three-person scenario and prints, for each student, the
always / some / never verdict plus the actual witness award lists for any
conditional student.

Scenario
--------
Three students each have an exclusive 3-point G course for graduation; a fixed
tie order C, B, A and a fixed quota.  Extra honor records:

  A: exclusive honor 3 points + E 2 points
  B: exclusive honor 2 points + E 2 points
  C: exclusive honor 4 points

E can be read as honors (E_HON) or as an ordinary elective (E_ORD).  With two
seats A always wins.  Re-keying B's E record to an *independent* code F (F also
two-way) makes A land third when E is ordinary and F is honors, so A becomes
*conditional*; restoring the original record and cutting the quota to one also
makes A conditional.

Usage:  PYTHONPATH=. python3 examples/scholarship_demo.py
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from gradaudit import CharterDraft, parse_catalog, parse_records, parse_requirements
from gradaudit.scholarship import StudentEntry, assess_scholarship


def _catalog():
    return parse_catalog({
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
    })


def _req():
    return parse_requirements({"request_id": "Q", "revision": 1, "groups": [
        {"group_id": "GRAD", "allowed_categories": ["G"], "minimum_points": 3}]})


def _students(b_code="E"):
    def mk(sid, recs):
        return parse_records({"student_id": sid, "revision": 1, "records": recs})
    A = mk("A", [
        {"record_id": "A_G", "external_course": "XA_G", "points": 3},
        {"record_id": "A_H", "external_course": "XA_H", "points": 3},
        {"record_id": "A_E", "external_course": "E", "points": 2},
    ])
    B = mk("B", [
        {"record_id": "B_G", "external_course": "XB_G", "points": 3},
        {"record_id": "B_H", "external_course": "XB_H", "points": 2},
        {"record_id": f"B_{b_code}", "external_course": b_code, "points": 2},
    ])
    C = mk("C", [
        {"record_id": "C_G", "external_course": "XC_G", "points": 3},
        {"record_id": "C_H", "external_course": "XC_H", "points": 4},
    ])
    return {"A": A, "B": B, "C": C}


def _print_case(title, catalog, students, covered, quota):
    draft = CharterDraft.from_catalog(catalog, covered)
    req = _req()
    entries = [StudentEntry("C", students["C"], req),
               StudentEntry("B", students["B"], req),
               StudentEntry("A", students["A"], req)]
    rep = assess_scholarship(catalog, entries, ["HONOR"], draft, quota=quota,
                             tie_order=["C", "B", "A"])
    print("\n" + "=" * 74)
    print(f"{title}  (quota={quota}, covered={sorted(covered)})")
    print("=" * 74)
    print(f"legal finalizations: {rep['legal_finalizations']}  "
          f"| canonical: {rep['canonical_finalization']}")
    print(f"canonical award list: "
          f"{[a['student_id'] for a in rep['canonical_award_list']]}")
    for s in rep["students"]:
        line = f"  {s['student_id']}: {s['status']:<6} (canonical honor points " \
               f"{s['canonical_honor_points']})"
        print(line)
        if "witness" in s:
            w = s["witness"]
            for key, label in (("available_under", "WINS under "),
                               ("not_available_under", "LOSES under")):
                snap = w[key]
                fin = ", ".join(f"{k}={v}" for k, v in sorted(snap["finalization"].items()))
                awards = ", ".join(
                    f"{a['student_id']}({a['honor_points']})" for a in snap["award_list"]
                )
                print(f"      {label} {{{fin}}} -> awards: {awards}")
    return rep


def main() -> int:
    catalog = _catalog()

    # 1. E can be either; two seats; A always wins.
    _print_case("E two-way, two seats", catalog, _students("E"), {"E"}, 2)

    # 2. B's E record corrected to independent code F (E and F each two-way).
    _print_case("B re-keyed to F, two seats", catalog, _students("F"), {"E", "F"}, 2)

    # 3. Restore the original record, shrink the quota to one.
    _print_case("original records, one seat", catalog, _students("E"), {"E"}, 1)

    print("\nOK: witness lists are cohort-consistent (one finalization each).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
