#!/usr/bin/env python3
"""Paper-archive reconstruction + custodian retention demonstrations.

Runs the four checked-in custody scenarios in ``examples/archive`` and prints
the retained boxes, total cost, and the per-fault certificates.  Also shows the
pure reconstruction report for a readable box subset.

Usage:  PYTHONPATH=. python3 examples/archive_demo.py
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from gradaudit import custody_report, parse_archive

ARCH = os.path.join(HERE, "archive")


def load(name):
    with open(os.path.join(ARCH, name), encoding="utf-8") as fh:
        return json.load(fh)


def banner(text):
    print("\n" + "=" * 74)
    print(text)
    print("=" * 74)


SCENARIOS = [
    ("base.json", "两个 a 副本：最省留 A、B（共费 2），坏任一仍能用页 a 还原同一记录"),
    ("voided_b.json", "只作废 B 里的 a 副本：改留 A、C（共费 4）"),
    ("raised_b.json", "恢复原签封但 B 涨到 5：仍选更省的 A、C（共费 4）"),
    ("two_group_infeasible.json", "一生需两组、逐组都够但只能还原同一条记录 -> 用全部盒仍办不了"),
    ("multi_student.json", "多生 + 共用学校证明 + 孤立凭据：逐盒损失证书"),
]


def main() -> int:
    for name, desc in SCENARIOS:
        banner(f"{name}\n{desc}")
        rep = custody_report(load(name))
        print(f"  feasible = {rep['feasible']}")
        if rep["feasible"]:
            print("  retained boxes:",
                  [(b["box_id"], b["cost"]) for b in rep["retained_boxes"]])
            print(f"  total cost = {rep['total_cost']}")
            for cert in rep["loss_certificates"]:
                used = [(p["student_id"], p["used_records"]) for p in cert["students"]]
                print(f"    lose {cert['missing_box']}: {used}")
        else:
            fail = rep.get("normal_failure") or (rep.get("loss_failures") or [{}])[0]
            for s in fail.get("students", []):
                print(f"    {s['student_id']}: minima={s.get('group_minima')}, "
                      f"alone-max={s.get('group_maxima_ignoring_other_groups')}")

    banner("Reconstruction report for one readable box subset (base.json)")
    base = load("base.json")
    arc = parse_archive(base["archive"])
    report = arc.report(["A"])
    print(json.dumps(report, ensure_ascii=False, indent=2))

    print("\nOK: all archive/custody demonstrations completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
