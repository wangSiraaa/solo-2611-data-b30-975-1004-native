"""End-to-end warranty + repair-shop demo driver.

Run ``python3 -m basket_service warranty-demo`` to rebuild a standalone
database and write a JSON transcript that walks the whole brief from the real
per-unit deal allocation to the service-desk query and the repair bay:

* two same-price readers where only one actually receives the coupon addendum,
  plus a second equal-price sale whose coupon goes to a peer physical unit;
* overlapping factory outages counted once, a late back-dated entry/exit
  record that recomputes a new immutable warranty version while preserving the
  answer previously given to the customer, and a brand-new outage registered
  after budget exhaustion that cannot revive cover;
* a normal return terminating cover on its effective business day;
* repair scheduling by warranty tier, delivery day and technician shifts,
  including the brief's "free only on day 7" and "two-day method vs one free
  day" cases, value maximisation under contention and historical version
  pinning.

Every expectation is derived independently (hand arithmetic / a brute-force
placement scan) rather than by calling the production optimiser.
"""

from __future__ import annotations

import json
from itertools import product
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from . import open_service
from .warranty_engine import Outage, compute_warranty

ROOT = Path(__file__).resolve().parent.parent
SKU = "R"  # 展教读机 stable part number


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def brute_force_best(orders: Sequence[Dict[str, Any]], technicians: Dict[str, Dict[str, Any]], methods: Dict[str, Dict[str, Any]], horizon_end_day: int) -> int:
    """Independent reference: enumerate placements, maximise total value.

    Mirrors the production constraints but is written as a flat Cartesian
    scan over (order -> skip or one feasible placement), so the optimiser's
    search cannot validate itself.
    """

    options: List[List[Tuple[int, Any]]] = []
    for order in orders:
        placements: List[Tuple[int, Any]] = [(0, None)]  # skip
        latest_start = order["latest_start_day"]
        if latest_start is None:
            options.append(placements)
            continue
        for method_id in order["method_ids"]:
            method = methods[method_id]
            for tech_id, tech in technicians.items():
                if tech["specialty"] != method["specialty"]:
                    continue
                last_start = min(latest_start, horizon_end_day - 1)
                for start in range(order["delivery_day"], last_start + 1):
                    days = list(range(start, start + method["duration_days"]))
                    if any(s <= d < e for s, e in tech["unavailable"] for d in days):
                        continue
                    placements.append((order["repair_value"], (tech_id, days)))
        options.append(placements)

    best = -1
    for combo in product(*options):
        slots: Dict[Tuple[str, int], int] = {}
        total = 0
        ok = True
        for value, placement in combo:
            if placement is None:
                continue
            tech_id, days = placement
            for day in days:
                key = (tech_id, day)
                if key in slots:
                    ok = False
                    break
                slots[key] = 1
            if not ok:
                break
            total += value
        if ok:
            best = max(best, total)
    return max(best, 0)


def run_warranty_demo(db_path: str, out_path: str) -> Dict[str, Any]:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(db_path + suffix)
        if candidate.exists():
            candidate.unlink()

    svc = open_service(db_path)
    transcript: Dict[str, Any] = {"database": db_path, "scenarios": {}, "checks": []}
    checks = transcript["checks"]

    def check(name: str, ok: bool, expected: Any, actual: Any) -> None:
        checks.append({"check": name, "ok": bool(ok), "expected": expected, "actual": actual})

    # ---------------------------------------------------------------
    # 1. Real per-unit deal allocation: two same-price readers.
    # ---------------------------------------------------------------
    basket = [
        {"unit_id": "uA", "sku": SKU, "price": 1000},
        {"unit_id": "uB", "sku": SKU, "price": 1000},
    ]
    coupon = [{"coupon_id": "EXT", "D": 100, "requires": {SKU: 1}}]
    snap = svc.publish_snapshot([{"sku": SKU, "price": 1000, "quantity": 2}], coupon, snapshot_id="w-snap")
    quote = svc.quote(basket, [], "w-q1", snap)
    svc.register_sale("S1", quote=quote)
    owner = {a["unit_id"]: a["coupon_id"] for a in quote["allocation"]}
    transcript["scenarios"]["deal_allocation"] = {
        "assignments": quote["assignments"],
        "per_unit_paid": [{"unit_id": a["unit_id"], "coupon_id": a["coupon_id"], "paid": a["paid"]} for a in quote["allocation"]],
    }
    check("coupon_lands_on_exactly_one_physical_unit", sorted(u for u, c in owner.items() if c) == ["uA"], ["uA"], sorted(u for u, c in owner.items() if c))

    # Warranty terms: 3 base days, EXT addendum +2 days.
    svc.publish_warranty_term("w-base", SKU, 3)
    svc.publish_warranty_term("w-ext", SKU, 3, coupon_id="EXT", bonus_days=2)
    enrolled = svc.enroll_warranties("S1", 0)
    budget = {w["unit_id"]: w["warranty_days"] for w in enrolled}
    transcript["scenarios"]["warranty_enrollment"] = enrolled
    check("unit_a_budget_5_from_base_plus_addendum", budget["uA"] == 5, 5, budget["uA"])
    check("unit_b_budget_3_base_only", budget["uB"] == 3, 3, budget["uB"])

    # ---------------------------------------------------------------
    # 2. Overlapping outages counted once; brief day-7 / day-8 result.
    # ---------------------------------------------------------------
    # NOTE on the brief's numbers: a literal half-open reading of [1,4)+[2,5)
    # unions to four calendar days (1..4).  To satisfy the stated "3 paused
    # days, start legal on day 7, expired on day 8" we use the minimal pair
    # whose union is exactly three days; the literal pair is demonstrated below.
    svc.register_outage("a-o1", "S1", "uA", 1, 3, note="第一次返厂")
    svc.register_outage("a-o2", "S1", "uA", 2, 4, note="第二次返厂(重叠一天只停一次)")
    svc.register_outage("b-o1", "S1", "uB", 1, 3)
    svc.register_outage("b-o2", "S1", "uB", 2, 4)
    w_a = svc.get_warranty("S1", "uA")
    w_b = svc.get_warranty("S1", "uB")
    transcript["scenarios"]["overlapping_outages"] = {"uA": w_a, "uB": w_b}
    check("uA_paused_3_expiry_day_8_latest_start_7", (w_a["paused_days"], w_a["expiry_day"], w_a["latest_start_day"]) == (3, 8, 7), (3, 8, 7), (w_a["paused_days"], w_a["expiry_day"], w_a["latest_start_day"]))
    check("uB_paused_3_expiry_day_6_latest_start_5", (w_b["paused_days"], w_b["expiry_day"], w_b["latest_start_day"]) == (3, 6, 5), (3, 6, 5), (w_b["paused_days"], w_b["expiry_day"], w_b["latest_start_day"]))

    # Literal brief intervals, transparent disclosure of the 4-day union.
    literal = compute_warranty(sold_day=0, warranty_days=5, outages=[Outage(1, 4), Outage(2, 5)])
    transcript["scenarios"]["literal_interval_disclosure"] = {
        "intervals": [[1, 4], [2, 5]],
        "union": literal["outage_intervals"],
        "paused_days": literal["paused_days"],
        "expiry_day": literal["expiry_day"],
        "note": "严格左闭右开+重叠取并 => 并集4天; 题面文字称3天, 故主动用 [1,3)+[2,4) 复现其目标到期日",
    }
    check("literal_half_open_union_is_four_days", literal["paused_days"] == 4 and literal["expiry_day"] == 9, (4, 9), (literal["paused_days"], literal["expiry_day"]))

    # Service desk query with full explanation.
    q_a7 = svc.query_warranty("desk-A-7", "S1", "uA", 7)
    q_a8 = svc.query_warranty("desk-A-8", "S1", "uA", 8)
    q_b7 = svc.query_warranty("desk-B-7", "S1", "uB", 7)
    transcript["scenarios"]["service_desk"] = {"uA_day7": q_a7, "uA_day8": q_a8, "uB_day7": q_b7}
    check("desk_uA_day7_covered", q_a7["covered"] is True, True, q_a7["covered"])
    check("desk_uA_day8_expired_budget", q_a8["covered"] is False and q_a8["reason"] == "WARRANTY_BUDGET_EXHAUSTED", (False, "WARRANTY_BUDGET_EXHAUSTED"), (q_a8["covered"], q_a8["reason"]))
    check("desk_uB_day7_expired", q_b7["covered"] is False, False, q_b7["covered"])
    check("desk_explains_origin_coupon_and_paused", q_a7["term"]["coupon_id"] == "EXT" and q_a7["paused_days"] == 3, ("EXT", 3), (q_a7["term"]["coupon_id"], q_a7["paused_days"]))

    # ---------------------------------------------------------------
    # 3. Second equal-price sale: coupon goes to a peer unit.
    # ---------------------------------------------------------------
    basket2 = [
        {"unit_id": "n1", "sku": SKU, "price": 1000},
        {"unit_id": "n2", "sku": SKU, "price": 1000},
    ]
    quote2 = svc.quote(basket2, [], "w-q2", snap)
    svc.register_sale("S2", quote=quote2)
    svc.enroll_warranties("S2", 0)
    owner2 = {a["unit_id"]: a["coupon_id"] for a in quote2["allocation"]}
    coupon_unit = next(u for u, c in owner2.items() if c == "EXT")
    base_unit = {"n1", "n2"} - {coupon_unit}
    base_unit = next(iter(base_unit))
    for unit in ("n1", "n2"):
        svc.register_outage(f"{unit}-o1", "S2", unit, 1, 3)
        svc.register_outage(f"{unit}-o2", "S2", unit, 2, 4)
    w_base = svc.get_warranty("S2", base_unit)
    transcript["scenarios"]["peer_allocation"] = {
        "coupon_unit": coupon_unit,
        "base_only_unit": base_unit,
        "base_only_warranty": w_base,
    }
    check("peer_base_unit_expires_day_6_despite_same_outages", w_base["expiry_day"] == 6, 6, w_base["expiry_day"])

    # ---------------------------------------------------------------
    # 4. Late back-dated report recomputes a new version; old answer kept.
    # ---------------------------------------------------------------
    svc.register_sale("S3", quote=svc.quote([{"unit_id": "d1", "sku": SKU, "price": 1000}], [], "w-q3", snap))
    svc.enroll_warranties("S3", 10)  # old sale, business deal day 10
    early_answer = svc.query_warranty("desk-early", "S3", "d1", 12)  # covered under v1
    svc.register_outage("late-factory", "S3", "d1", 11, 13, registered_day=20, note="返厂晚报, 业务日11-12")
    after_backdate = svc.get_warranty("S3", "d1")
    kept_answer = svc.get_warranty_query("desk-early")
    transcript["scenarios"]["late_backdate"] = {
        "answer_before": early_answer,
        "warranty_after": after_backdate,
        "historical_answer_kept": kept_answer,
        "versions": svc.warranty_versions("S3", "d1"),
    }
    check("backdate_creates_version_2_expiry_17", (after_backdate["current_version"], after_backdate["expiry_day"]) == (2, 17), (2, 17), (after_backdate["current_version"], after_backdate["expiry_day"]))
    check("old_desk_answer_pinned_to_version_1", kept_answer["warranty_version"] == 1 and kept_answer["covered"] is True, (1, True), (kept_answer["warranty_version"], kept_answer["covered"]))

    # ---------------------------------------------------------------
    # 5. Outage registered after exhaustion cannot revive the unit.
    # ---------------------------------------------------------------
    # A coupon-free snapshot so the single unit really gets only the 3-day base.
    snap_plain = svc.publish_snapshot([{"sku": SKU, "price": 1000, "quantity": 1}], [], snapshot_id="w-snap-plain")
    svc.register_sale("S4", quote=svc.quote([{"unit_id": "e1", "sku": SKU, "price": 1000}], [], "w-q4", snap_plain))
    svc.enroll_warranties("S4", 0)  # 3-day budget, no outage -> expiry day 3
    svc.register_outage("post-death", "S4", "e1", 5, 7, registered_day=7, note="保期耗尽后的全新停用")
    dead = svc.get_warranty("S4", "e1")
    dead_query = svc.query_warranty("desk-dead", "S4", "e1", 6)
    transcript["scenarios"]["post_expiry_outage"] = {"warranty": dead, "query_day6": dead_query}
    check("post_expiry_outage_keeps_expiry_day_3", dead["expiry_day"] == 3 and dead["paused_days"] == 0, (3, 0), (dead["expiry_day"], dead["paused_days"]))
    check("post_expiry_days_reported_as_ineffectual", dead_query["ineffectual_outage_days"] == 2, 2, dead_query["ineffectual_outage_days"])

    # ---------------------------------------------------------------
    # 6. Normal return exits warranty on the effective business day.
    # ---------------------------------------------------------------
    svc.register_sale("S5", quote=svc.quote([{"unit_id": "r1", "sku": SKU, "price": 1000}], [], "w-q5", snap_plain))
    svc.enroll_warranties("S5", 0)
    svc.return_units("ret-r1", "S5", ["r1"], {"r1": "RESTOCK"})
    svc.mark_return_exit("S5", "r1", 2, "ret-r1")
    returned = svc.get_warranty("S5", "r1")
    transcript["scenarios"]["return_exit"] = returned
    check("return_sets_cover_until_day_2", (returned["end_reason"], returned["cover_until_day"], returned["latest_start_day"]) == ("RETURNED_EXIT", 2, 1), ("RETURNED_EXIT", 2, 1), (returned["end_reason"], returned["cover_until_day"], returned["latest_start_day"]))

    # ---------------------------------------------------------------
    # 7. Repair bay: brief day-7 case and two-day method case.
    # ---------------------------------------------------------------
    svc.register_repair_method("m1d", SKU, 1, "L1")
    svc.register_repair_method("m2d", SKU, 2, "L1")
    svc.register_technician("tech-day7", "L1", unavailable=[(0, 7), (8, 60)])
    svc.open_work_order("WO-A", "S1", "uA", 6, 500, ["m1d"])
    svc.open_work_order("WO-B", "S1", "uB", 6, 500, ["m1d"])
    brief_batch = svc.plan_repairs("BAY-BRIEF", ["WO-A", "WO-B"], 30)
    transcript["scenarios"]["repair_brief"] = brief_batch
    decision = {d["work_order_id"]: d for d in brief_batch["decisions"]}
    check("brief_accepts_uA_on_day7", decision["WO-A"]["accepted"] and decision["WO-A"]["start_day"] == 7, (True, 7), (decision["WO-A"]["accepted"], decision["WO-A"]["start_day"]))
    check("brief_rejects_uB_no_covered_start", (not decision["WO-B"]["accepted"]) and decision["WO-B"]["reason_code"] == "NO_COVERED_START_DAY", (False, "NO_COVERED_START_DAY"), (decision["WO-B"]["accepted"], decision["WO-B"]["reason_code"]))

    # Two-day method with only one free day -> cannot schedule.  Use the coupon
    # holder from the second sale (a distinct physical unit whose 5-day budget
    # plus the same outages keeps cover through day 7), because uA already has
    # a booked work order.
    two_day_unit = coupon_unit
    svc.open_work_order("WO-2D", "S2", two_day_unit, 6, 500, ["m2d"])
    two_day = svc.plan_repairs("BAY-TWODAY", ["WO-2D"], 30)
    transcript["scenarios"]["repair_two_day_one_slot"] = two_day
    td = two_day["decisions"][0]
    check("two_day_method_rejected_with_single_free_day", (not td["accepted"]) and td["reason_code"] == "NO_SLOT_WITHIN_HORIZON", (False, "NO_SLOT_WITHIN_HORIZON"), (td["accepted"], td["reason_code"]))

    # ---------------------------------------------------------------
    # 8. Contention: value maximisation, cross-checked by brute force.
    # ---------------------------------------------------------------
    n = 14
    rush_sku = "G"
    units = [{"unit_id": f"g{i}", "sku": rush_sku, "price": 1000} for i in range(n)]
    svc.register_sale(
        "SG",
        quote=svc.quote(units, [], "w-qg", svc.publish_snapshot([{"sku": rush_sku, "price": 1000, "quantity": n}], [], snapshot_id="w-snap-g")),
    )
    # A separate SKU carries its own longer base term (20 days).
    svc.publish_warranty_term("w-g", rush_sku, 20)
    svc.enroll_warranties("SG", 0)
    svc.register_repair_method("gm", rush_sku, 1, "L")
    svc.register_technician("rush-tech", "L", unavailable=[(3, 40)])  # only days 0..2
    values = [10 * (i + 1) for i in range(n)]
    for i, value in enumerate(values):
        svc.open_work_order(f"G{i}", "SG", f"g{i}", 0, value, ["gm"])
    rush = svc.plan_repairs("BAY-RUSH", [f"G{i}" for i in range(n)], 40)
    # Independent brute force on the same inputs.
    methods = {"gm": {"duration_days": 1, "specialty": "L"}}
    techs = {"rush-tech": {"specialty": "L", "unavailable": [(3, 40)]}}
    bf_orders = [
        {"delivery_day": 0, "repair_value": v, "method_ids": ["gm"], "latest_start_day": 19}
        for v in values
    ]
    expected_total = brute_force_best(bf_orders, techs, methods, 40)
    transcript["scenarios"]["rush_contention"] = {"batch": rush, "brute_force_total": expected_total}
    check("rush_matches_brute_force_max_value", rush["total_value"] == expected_total, expected_total, rush["total_value"])
    check("rush_accepts_three_highest_value", rush["accepted"] == 3 and rush["total_value"] == 10 * (14 + 13 + 12), 390, rush["total_value"])

    # ---------------------------------------------------------------
    # 9. Historical scheduling pinned to the warranty version at order time.
    # ---------------------------------------------------------------
    svc.register_sale("SH", quote=svc.quote([{"unit_id": "h1", "sku": SKU, "price": 1000}], [], "w-qh", snap_plain))
    svc.enroll_warranties("SH", 0)  # 3-day base -> expiry day 3
    svc.register_repair_method("hm", SKU, 1, "L1")
    svc.register_technician("hist-tech", "L1")
    svc.open_work_order("OLD", "SH", "h1", 2, 100, ["hm"])  # pinned to v1, start day 2 legal
    svc.register_outage("h-back", "SH", "h1", 0, 3, registered_day=9)  # now v2 -> expiry day 6
    old_batch = svc.plan_repairs("BAY-HIST-OLD", ["OLD"], 20)
    old_decision = old_batch["decisions"][0]
    transcript["scenarios"]["historical_version"] = {
        "order_pinned_version": svc.get_work_order("OLD")["warranty_version"],
        "old_batch_decision": old_decision,
        "stored_decision": svc.get_booking_decision("OLD"),
        "current_version": svc.get_warranty("SH", "h1")["current_version"],
    }
    check("historical_order_uses_pinned_v1_start_day_2", (old_decision["accepted"], old_decision["start_day"], old_decision["warranty_version"]) == (True, 2, 1), (True, 2, 1), (old_decision["accepted"], old_decision["start_day"], old_decision["warranty_version"]))

    # ---------------------------------------------------------------
    # 10. Reopen persistence.
    # ---------------------------------------------------------------
    del svc
    reopened = open_service(db_path)
    reopened_batch = reopened.get_repair_batch("BAY-BRIEF")
    reopened_query = reopened.get_warranty_query("desk-A-7")
    check("reopen_preserves_booking_and_query", reopened_batch["total_value"] == brief_batch["total_value"] and reopened_query["covered"] is True, brief_batch["total_value"], reopened_batch["total_value"])

    transcript["summary"] = {
        "checks": len(checks),
        "passed": sum(1 for c in checks if c["ok"]),
        "failed": sum(1 for c in checks if not c["ok"]),
    }
    _write_json(Path(out_path), transcript)
    return transcript
