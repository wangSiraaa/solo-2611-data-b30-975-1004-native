"""转换通道 + 周度选道 + SQLite 落库的端到端演示。

与 demo_run.py 相互独立，使用 workdir/channels/ 下的独立存储/数据库/回执。

三个已发布修订（同一模板 ord，展示重复参数、字符串里的 ':k' 与注释假参数）：
  r1 参数 (a, b)          INSERT INTO orders(rev, x, y) ，a、b 各出现一次
  r2 参数 (k)             含字符串字面量 ':k' 与注释 /* :fake */，真参数只有 k
  r3 参数 (x, y)          x 重复出现两次（同名复制同值）

三条通道：
  c12  r1 -> r2，费 1：合并 a、b 为 k（多源必须同类型且相等）
  c23  r2 -> r3，费 1：复制 k 给 x、y（一个来源供多个目标）
  c13  r1 -> r3，费 4：原样转 a -> x、b -> y

场景与题面样例对齐：
  周 W1：两单都送 (3,3)，各 2 分、核对 x=3；预算 2 -> 买 c12+c23，两单都接。
  周 W2：第二单改成 (3,4)；预算 2 -> 只接第一单；预算 4 -> 两单都走 c13 直达，费用 4。
  另演示 str 与同内容 bytes 的 typed 分歧（合并通道对该单不可用）、零跳、
  同形千单门店与混合门店的规划形态，以及业务写失败整批撤回和周内重送核对。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "workdir" / "channels"

from sql_param.channels import Channel, ChannelCatalog  # noqa: E402
from sql_param.errors import ChannelError  # noqa: E402
from sql_param.executor import BatchExecutor  # noqa: E402
from sql_param.plan_store import PlanStore  # noqa: E402
from sql_param.planner import WeeklyLedger, WeeklyPlanner  # noqa: E402

SCHEMA = """
CREATE TABLE orders(
    id TEXT PRIMARY KEY,
    rev INTEGER NOT NULL,
    x TEXT, y TEXT,
    p TEXT
);
"""

R1 = "INSERT INTO orders(id, rev, x, y) VALUES(:id, 1, :a, :b)"
R2 = ("INSERT INTO orders(id, rev, x, y) VALUES(:id, 2, :k, ':k') "
      "/* :fake is not a parameter; :k2 neither */")
R3 = "INSERT INTO orders(id, rev, x, y, p) VALUES(:id, 3, :x, :y, :x)"

# 通道映射：id 在三个修订间原样透传（一个来源可供多个目标，此处每个目标各取一次）。
CHANNELS = [
    {"channel_id": "c12", "source_revision": 1, "target_revision": 2, "fee": 1,
     "mapping": {"id": ["id"], "k": ["a", "b"]}},
    {"channel_id": "c23", "source_revision": 2, "target_revision": 3, "fee": 1,
     "mapping": {"id": ["id"], "x": ["k"], "y": ["k"]}},
    {"channel_id": "c13", "source_revision": 1, "target_revision": 3, "fee": 4,
     "mapping": {"id": ["id"], "x": ["a"], "y": ["b"]}},
]


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(label: str, payload) -> None:
    print(f"--- {label} ---")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def order(order_id, a, b, check_x, priority=2, source=1, target=3):
    return {
        "order_id": order_id,
        "source_revision": source,
        "target_revision": target,
        "priority": priority,
        "bindings": {"id": f"id-{order_id}", "a": a, "b": b},
        "checks": {"x": check_x},
    }


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    store_dir, receipts_dir = WORK / "store", WORK / "receipts"
    db_path = WORK / "orders.db"
    requests_dir = WORK / "requests"
    requests_dir.mkdir(parents=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)

    store = PlanStore(store_dir)
    executor = BatchExecutor(store, db_path, receipts_dir)

    # 1. 发布三个修订并核对参数名（重复参数/字面量/注释假参数）。
    section("1. 发布 ord 的三个修订并按实际计划核名")
    store.publish("ord", 1, R1)
    store.publish("ord", 2, R2)
    store.publish("ord", 3, R3)
    names = {rev: store.distinct_names(store.load("ord", rev)) for rev in (1, 2, 3)}
    show("各修订不同参数名（注释假参数 :fake/:k2 与字面量 ':k' 均不算）", names)
    assert names == {1: ("id", "a", "b"), 2: ("id", "k"), 3: ("id", "x", "y")}

    catalog = ChannelCatalog(store, "ord", [Channel.from_dict(c) for c in CHANNELS])
    planner = WeeklyPlanner(catalog)

    # 2. 模板室逐跳试换：能取回每一步的值与失败处，且不碰数据库。
    section("2. 模板室逐跳试换（独立于订单库）")
    hop_ok = catalog.try_hop(catalog.by_id["c12"],
                             {"id": "id-S1", "a": 3, "b": 3})
    show("c12 对 (a=3,b=3) 试换成功", catalog._hop_to_dict(hop_ok))
    hop_bad = catalog.try_hop(catalog.by_id["c12"],
                              {"id": "id-S2", "a": 3, "b": 4})
    show("c12 对 (a=3,b=4) 试换失败处", catalog._hop_to_dict(hop_bad))
    assert hop_ok.ok and hop_ok.out_bindings == {"id": "id-S1", "k": 3}
    assert not hop_bad.ok and "disagree" in hop_bad.error

    paths = catalog.enumerate_paths(
        1, {"id": "id-S1", "a": 3, "b": 3}, 3, {"x": 3})
    show("(3,3) 订单全部成功路径（含每跳解析值与最终绑定）",
         [{"hops": p["hops"], "final_bindings": p["final_bindings"]} for p in paths])
    assert [p["hops"] for p in paths] == [["c13"], ["c12", "c23"]]

    # 文本与同内容字节按旧绑定合同不相等：合并通道对该单不可用，直达仍可。
    diverge = order("D1", "AB", {"$bytes": "4142"}, "AB")
    eval_div = planner.evaluate_order(diverge)
    show("文本 'AB' 与字节 4142 分歧单的可用路径",
         {"minimal_masks": sorted(eval_div.minimal_masks),
          "hops": [p["hops"] for p in eval_div.paths]})
    assert [p["hops"] for p in eval_div.paths] == [["c13"]]

    # 3. 周 W1：两单 (3,3)，预算 2，买 c12+c23 接两单。
    section("3. 周 W1：两单 (3,3)、预算 2 -> c12+c23 接两单（总分 4，费用 2）")
    week1_orders = [order("S-1", 3, 3, 3), order("S-2", 3, 3, 3)]
    plan1 = planner.plan_week("W1", 2, week1_orders)
    show("W1 规划摘要", _summary(plan1))
    assert plan1["purchased_channel_ids"] == ["c12", "c23"]
    assert plan1["channel_cost"] == 2 and plan1["total_score"] == 4
    assert all(o["status"] == "accepted" for o in plan1["orders"])

    ledger = WeeklyLedger(WORK / "weekly-ledger.json")
    ledger.record_week(plan1)
    (WORK / "W1.json").write_text(json.dumps(plan1, ensure_ascii=False, indent=2),
                                  encoding="utf-8")

    # 4. 用选好的实际绑定走现有 SQLite 批次：通过订单入库，回执可还原通道与修订。
    section("4. W1 中选订单走 BatchExecutor 单事务落库")
    receipt1 = executor.execute_batch("W1-accepted", planner.build_executor_items(plan1))
    show("W1 批次回执状态", {"status": receipt1["status"],
                            "items": [{"revision": i["revision"],
                                       "ordered_values": i["ordered_values"]}
                                      for i in receipt1["items"]]})
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT id, rev, x, y, p FROM orders ORDER BY id").fetchall()
    show("落库行（rev 固定 3；p 是 r3 重复参数 :x 的复制值）", rows)
    assert receipt1["status"] == "committed"
    assert rows == [("id-S-1", 3, "3", "3", "3"), ("id-S-2", 3, "3", "3", "3")]
    # 回执可还原修订；规划回执可还原每一跳通道。
    assert {i["revision"] for i in receipt1["items"]} == {3}
    assert plan1["orders"][0]["route"]["hops"] == ["c12", "c23"]

    # 5. 周 W2：第二单改 (3,4)。预算 2 只接第一张；预算 4 两单走直达 c13。
    section("5. 周 W2：第二单改 (3,4) —— 预算 2 只接一单；预算 4 两单直达")
    week2_orders = [order("T-1", 3, 3, 3), order("T-2", 3, 4, 3)]
    plan2_tight = planner.plan_week("W2-tight", 2, week2_orders)
    show("W2 预算 2 摘要", _summary(plan2_tight))
    assert [o["order_id"] for o in plan2_tight["orders"] if o["status"] == "accepted"] == ["T-1"]
    assert plan2_tight["purchased_channel_ids"] == ["c12", "c23"]
    # T-2 的合并通道因 a!=b 不可用，只剩费 4 的直达；预算 2 买不起。
    assert "none of its channels was purchased" in plan2_tight["orders"][1]["reason"]

    plan2_full = planner.plan_week("W2-full", 4, week2_orders)
    show("W2 预算 4 摘要", _summary(plan2_full))
    assert plan2_full["purchased_channel_ids"] == ["c13"]
    assert plan2_full["channel_cost"] == 4 and plan2_full["total_score"] == 4
    t2 = next(o for o in plan2_full["orders"] if o["order_id"] == "T-2")
    assert t2["route"]["hops"] == ["c13"]
    assert t2["route"]["final_bindings"] == {"id": "id-T-2", "x": 3, "y": 4}
    ledger.record_week(plan2_tight)
    ledger.record_week(plan2_full)

    # 6. 随单核对字段决定可交付性：同一张单两条路径目标绑定不同，只认声明值。
    section("6. 目标核对字段：声明 x=9 时两条路径都不满足 -> 拒绝")
    mismatched = order("M1", 3, 3, 9)
    plan_m = planner.plan_week("W-mismatch", 9, [mismatched])
    show("核对不过的裁决", plan_m["orders"])
    assert plan_m["orders"][0]["status"] == "rejected"
    assert "declared target check" in plan_m["orders"][0]["reason"]

    # 7. 门店形态：同形千单复用少数通道；混合门店每单值不同，逐单试换。
    section("7. 门店形态：同形千单 vs 混合门店")
    uniform = [order(f"U-{i:04d}", 3, 3, 3, priority=1) for i in range(1000)]
    plan_uniform = planner.plan_week("W-uniform", 2, uniform)
    show("同形千单门店", {"accepted": sum(o["status"] == "accepted" for o in plan_uniform["orders"]),
                         "purchased": plan_uniform["purchased_channel_ids"],
                         "total_score": plan_uniform["total_score"],
                         "subsets_scanned": plan_uniform["considered_subsets"]})
    assert plan_uniform["total_score"] == 1000
    mixed = [order(f"X-{i}", i % 5, (i + 1) % 5, i % 5, priority=1) for i in range(20)]
    plan_mixed = planner.plan_week("W-mixed", 9, mixed)
    show("混合门店（值各不相同）",
         {"accepted": sum(o["status"] == "accepted" for o in plan_mixed["orders"]),
          "rejected": sum(o["status"] == "rejected" for o in plan_mixed["orders"]),
          "purchased": plan_mixed["purchased_channel_ids"]})

    # 8. 业务写失败整批撤回：r3 表上两条同 id 插入，第二条主键冲突，首条一并撤销。
    section("8. 业务写失败整批撤回（SQLite 约束失败 -> ROLLBACK）")
    bad_items = [
        {"template_id": "ord", "revision": 3,
         "bindings": {"id": "dup", "x": 1, "y": 1}},
        {"template_id": "ord", "revision": 3,
         "bindings": {"id": "dup", "x": 2, "y": 2}},
    ]
    rollback = executor.execute_batch("W-rollback", bad_items)
    with sqlite3.connect(db_path) as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM orders WHERE id='dup'")]
    show("回滚批回执与库内核对", {"status": rollback["status"],
                                 "error": rollback.get("error"),
                                 "dup_rows_left": ids})
    assert rollback["status"] == "rolled_back" and ids == []
    assert executor.read_batch("W-rollback")["status"] == "rolled_back"

    # 9. 周内重送核对：原样重送可核原结果；改值重送报 same_payload=false。
    section("9. 周内旧订单重送核对")
    resend_same = ledger.verify_resend("W1", week1_orders[0])
    resend_changed = ledger.verify_resend("W1", order("S-1", 3, 4, 3))
    show("原样重送 S-1", resend_same)
    show("改值后重送 S-1", resend_changed)
    assert resend_same["same_payload"] and resend_same["original_status"] == "accepted"
    assert not resend_changed["same_payload"]

    # 10. CLI 互操作：用 cli.plan_week 跑同一周度请求文件。
    section("10. CLI：python -m cli.plan_week 落盘周计划并核对重送")
    request_file = requests_dir / "week1.json"
    request_file.write_text(json.dumps({
        "week_id": "W1-cli", "template_id": "ord", "budget": 2,
        "channels": CHANNELS, "orders": week1_orders,
    }, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "cli.plan_week", str(store_dir),
         str(WORK / "cli-out"), str(request_file)],
        check=True, capture_output=True, text=True,
    )
    show("CLI 规划输出", json.loads(proc.stdout))
    resend_file = requests_dir / "resend.json"
    resend_file.write_text(json.dumps(week1_orders[0], ensure_ascii=False), encoding="utf-8")
    verify_proc = subprocess.run(
        [sys.executable, "-m", "cli.plan_week", "--verify",
         str(WORK / "cli-out" / "weekly-ledger.json"), "W1-cli", str(resend_file)],
        check=True, capture_output=True, text=True,
    )
    show("CLI 重送核对", json.loads(verify_proc.stdout))

    # 11. 通道定义仍受实际计划约束：引用注释里的假参数必须被拒绝。
    section("11. 负例：引用不存在的来源名（注释假参数 :fake）拒绝建目录")
    try:
        ChannelCatalog(store, "ord", [Channel.from_dict({
            "channel_id": "bad", "source_revision": 2, "target_revision": 3, "fee": 1,
            "mapping": {"x": ["fake"], "y": ["k"]},
        })])
        raise AssertionError("假参数通道未被拒绝")
    except ChannelError as exc:
        show("预期拒绝", str(exc))

    print("\n全部通道/规划/落库/重送演示通过。")
    print(f"计划目录: {store_dir}")
    print(f"回执目录: {receipts_dir}")
    print(f"数据库:   {db_path}")
    return 0


def _summary(plan: dict) -> dict:
    return {
        "week_id": plan["week_id"],
        "budget": plan["budget"],
        "purchased_channel_ids": plan["purchased_channel_ids"],
        "channel_cost": plan["channel_cost"],
        "total_score": plan["total_score"],
        "considered_subsets": plan["considered_subsets"],
        "orders": [{"order_id": o["order_id"], "status": o["status"],
                    "reason": o.get("reason"),
                    "hops": o.get("route", {}).get("hops")} for o in plan["orders"]],
    }


if __name__ == "__main__":
    raise SystemExit(main())
