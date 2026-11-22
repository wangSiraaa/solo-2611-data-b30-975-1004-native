"""通道规划与现有 SQLite 批次的集成测试、CLI 测试与限额性能测试。"""
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from sql_param.channels import Channel, ChannelCatalog
from sql_param.executor import BatchExecutor
from sql_param.plan_store import PlanStore
from sql_param.planner import WeeklyLedger, WeeklyPlanner

SCHEMA = "CREATE TABLE orders(id TEXT PRIMARY KEY, rev INTEGER, x TEXT, y TEXT, p TEXT);"


def publish_three(store: PlanStore) -> None:
    store.publish("ord", 1, "INSERT INTO orders(id, rev, x, y) VALUES(:id, 1, :a, :b)")
    store.publish(
        "ord", 2,
        "INSERT INTO orders(id, rev, x, y) VALUES(:id, 2, :k, ':k') /* :fake */")
    store.publish("ord", 3,
                  "INSERT INTO orders(id, rev, x, y, p) VALUES(:id, 3, :x, :y, :x)")


def standard_channels():
    return [
        {"channel_id": "c12", "source_revision": 1, "target_revision": 2, "fee": 1,
         "mapping": {"id": ["id"], "k": ["a", "b"]}},
        {"channel_id": "c23", "source_revision": 2, "target_revision": 3, "fee": 1,
         "mapping": {"id": ["id"], "x": ["k"], "y": ["k"]}},
        {"channel_id": "c13", "source_revision": 1, "target_revision": 3, "fee": 4,
         "mapping": {"id": ["id"], "x": ["a"], "y": ["b"]}},
    ]


def order(order_id, a, b, check_x, priority=2):
    return {"order_id": order_id, "source_revision": 1, "target_revision": 3,
            "priority": priority,
            "bindings": {"id": f"id-{order_id}", "a": a, "b": b},
            "checks": {"x": check_x}}


class ExecutionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db_path = root / "orders.db"
        store = PlanStore(root / "store")
        publish_three(store)
        self.catalog = ChannelCatalog(
            store, "ord", [Channel.from_dict(c) for c in standard_channels()])
        self.planner = WeeklyPlanner(self.catalog)
        self.executor = BatchExecutor(store, self.db_path, root / "receipts")
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(SCHEMA)

    def rows(self):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT id, rev, x, y, p FROM orders ORDER BY id").fetchall()

    def test_accepted_orders_land_at_target_revision_with_real_bindings(self):
        plan = self.planner.plan_week("W1", 2, [order("S-1", 3, 3, 3),
                                                order("S-2", 3, 3, 3)])
        receipt = self.executor.execute_batch(
            "W1", self.planner.build_executor_items(plan))
        self.assertEqual(receipt["status"], "committed")
        self.assertEqual({i["revision"] for i in receipt["items"]}, {3})
        self.assertEqual(self.rows(), [
            ("id-S-1", 3, "3", "3", "3"),
            ("id-S-2", 3, "3", "3", "3"),
        ])
        # 回执可还原修订；周计划可还原通道，二者合起来可复原整条转换链。
        self.assertEqual(plan["orders"][0]["route"]["hops"], ["c12", "c23"])
        self.assertEqual(
            [(h["from_revision"], h["to_revision"]) for h in
             plan["orders"][0]["route"]["hop_results"]], [(1, 2), (2, 3)])

    def test_business_write_failure_rolls_back_the_whole_accepted_batch(self):
        plan = self.planner.plan_week("W", 2, [order("S-1", 3, 3, 3)])
        items = self.planner.build_executor_items(plan)
        # 追加一个同主键条目，制造第二跳 SQLite 约束失败。
        items.append({"template_id": "ord", "revision": 3,
                      "bindings": {"id": "id-S-1", "x": 9, "y": 9}})
        receipt = self.executor.execute_batch("W-bad", items)
        self.assertEqual(receipt["status"], "rolled_back")
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.executor.read_batch("W-bad")["status"], "rolled_back")

    def test_blob_bindings_survive_the_full_chain(self):
        blob_order = {
            "order_id": "B1", "source_revision": 1, "target_revision": 3, "priority": 1,
            "bindings": {"id": "id-B1", "a": {"$bytes": "00ff"},
                         "b": {"$bytes": "00ff"}},
            "checks": {"x": {"$bytes": "00ff"}},
        }
        plan = self.planner.plan_week("W", 2, [blob_order])
        receipt = self.executor.execute_batch(
            "W", self.planner.build_executor_items(plan))
        self.assertEqual(receipt["status"], "committed")
        row = self.rows()[0]
        self.assertEqual(row[2], b"\x00\xff")  # x 列实际落库为 BLOB


class ParallelEdgeAndLimitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = PlanStore(root / "store")
        publish_three(self.store)

    def test_parallel_channels_between_same_two_revisions(self):
        # 同一对修订之间允许两条并行通道（不同映射策略），各自独立计费、独立试换。
        channels = standard_channels() + [{
            "channel_id": "c13-alt", "source_revision": 1, "target_revision": 3,
            "fee": 3,
            "mapping": {"id": ["id"], "x": ["b"], "y": ["a"]},  # 交换 a/b
        }]
        catalog = ChannelCatalog(
            self.store, "ord", [Channel.from_dict(c) for c in channels])
        planner = WeeklyPlanner(catalog)
        # (a=1,b=2) 且核对 x=2：只有交换通道满足；规划必须能选中它。
        swapped = {
            "order_id": "Q1", "source_revision": 1, "target_revision": 3, "priority": 5,
            "bindings": {"id": "id-Q1", "a": 1, "b": 2}, "checks": {"x": 2},
        }
        plan = planner.plan_week("W", 3, [swapped])
        self.assertEqual(plan["purchased_channel_ids"], ["c13-alt"])
        self.assertEqual(plan["orders"][0]["route"]["final_bindings"],
                         {"id": "id-Q1", "x": 2, "y": 1})

    def test_sixteen_channel_exhaustive_scan_performance(self):
        # 16 条可购通道 -> 65536 个子集，配合上千同形订单必须在可接受时间内精确穷举。
        channels = list(standard_channels())
        # 用 r1->r2 的并行通道补齐到 16 条（映射相同、id 不同、费 1）。
        while len(channels) < 16:
            idx = len(channels)
            channels.append({
                "channel_id": f"e{idx:02d}", "source_revision": 1,
                "target_revision": 2, "fee": 1,
                "mapping": {"id": ["id"], "k": ["a", "b"]},
            })
        catalog = ChannelCatalog(
            self.store, "ord", [Channel.from_dict(c) for c in channels])
        planner = WeeklyPlanner(catalog)
        orders = [order(f"U-{i:04d}", 3, 3, 3, priority=1) for i in range(1000)]
        started = time.perf_counter()
        plan = planner.plan_week("W-big", 16, orders)
        elapsed = time.perf_counter() - started
        self.assertEqual(plan["considered_subsets"], 1 << 16)
        self.assertEqual(plan["total_score"], 1000)
        self.assertLess(elapsed, 5.0)

    def test_seventeenth_channel_is_rejected(self):
        channels = []
        for i in range(17):
            channels.append({
                "channel_id": f"e{i:02d}", "source_revision": 1, "target_revision": 2,
                "fee": 1, "mapping": {"id": ["id"], "k": ["a", "b"]},
            })
        with self.assertRaises(ValueError):
            ChannelCatalog(self.store, "ord",
                           [Channel.from_dict(c) for c in channels])


class WeeklyCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root
        store = PlanStore(root / "store")
        publish_three(store)
        self.request = root / "week.json"
        self.request.write_text(json.dumps({
            "week_id": "W1", "template_id": "ord", "budget": 2,
            "channels": standard_channels(),
            "orders": [order("S-1", 3, 3, 3), order("S-2", 3, 3, 3)],
        }, ensure_ascii=False), encoding="utf-8")

    def run_cli(self, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, "-m", "cli.plan_week", *map(str, args)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, expect, proc.stderr)
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def test_plan_and_verify_resend_via_cli(self):
        out_dir = self.root / "out"
        summary = self.run_cli(self.root / "store", out_dir, self.request)
        self.assertEqual(summary["total_score"], 4)
        self.assertEqual(summary["purchased_channel_ids"], ["c12", "c23"])
        self.assertTrue((out_dir / "W1.json").exists())
        self.assertTrue((out_dir / "weekly-ledger.json").exists())

        # 原样重送：退出码 0，same_payload=true。
        resend = self.root / "resend.json"
        resend.write_text(json.dumps(order("S-1", 3, 3, 3)), encoding="utf-8")
        verified = self.run_cli(
            "--verify", out_dir / "weekly-ledger.json", "W1", resend)
        self.assertTrue(verified["same_payload"])

        # 改值重送：退出码 3。
        changed = self.root / "changed.json"
        changed.write_text(json.dumps(order("S-1", 3, 4, 3)), encoding="utf-8")
        self.run_cli("--verify", out_dir / "weekly-ledger.json", "W1",
                     changed, expect=3)

    def test_immutable_week_cannot_be_planned_twice(self):
        out_dir = self.root / "out"
        self.run_cli(self.root / "store", out_dir, self.request)
        proc = subprocess.run(
            [sys.executable, "-m", "cli.plan_week",
             str(self.root / "store"), str(out_dir), str(self.request)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("already exists", proc.stderr)


if __name__ == "__main__":
    unittest.main()
