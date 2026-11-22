import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sql_param.errors import ReconcileError
from sql_param.executor import BatchExecutor
from sql_param.plan_store import PlanStore
from sql_param.reconcile import reconcile_round, verify_order
from sql_param.slips import export_slip

SCHEMA = (
    "CREATE TABLE bins(id INTEGER PRIMARY KEY, name TEXT NOT NULL, qty INTEGER NOT NULL);"
)
TEMPLATES = {
    "add": "UPDATE bins SET qty = qty + :d WHERE id = :id",
    "sub": "UPDATE bins SET qty = qty - :d WHERE id = :id",
    "mul": "UPDATE bins SET qty = qty * :k WHERE id = :id",
    "read": "SELECT qty FROM bins WHERE id = :id ORDER BY id",
    "put": "INSERT INTO bins(id, name, qty) VALUES(:id, :name, :qty)",
}


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snapshot = self.root / "snapshot.db"
        with sqlite3.connect(self.snapshot) as conn:
            conn.execute(SCHEMA)
            conn.executemany(
                "INSERT INTO bins VALUES(?,?,?)", [(1, "X", 1), (2, "Y", 9)]
            )

    def run_counter(self, name, batches, templates=None, setup_sql=None):
        """一台离线柜台：独立计划库 + 快照副本，真实执行并返回回执。"""
        db_path = self.root / f"{name}.db"
        shutil.copy(self.snapshot, db_path)
        if setup_sql is not None:
            with sqlite3.connect(db_path) as conn:
                conn.executescript(setup_sql)
        store = PlanStore(self.root / f"store-{name}")
        for template_id, source in (templates or TEMPLATES).items():
            store.publish(template_id, 1, source)
        executor = BatchExecutor(store, db_path, self.root / f"receipts-{name}")
        return [executor.execute_batch(bid, items) for bid, items in batches]

    def demo_slips(self, public_read):
        """题目场景：甲加2/减2后读 X；乙翻倍。返回 (counters, 甲读数)。"""
        [receipt_a] = self.run_counter("jia", [("a-1", [
            {"template_id": "add", "revision": 1, "bindings": {"d": 2, "id": 1}},
            {"template_id": "sub", "revision": 1, "bindings": {"d": 2, "id": 2}},
            {"template_id": "read", "revision": 1, "bindings": {"id": 1}},
        ])])
        [receipt_b] = self.run_counter("yi", [("b-1", [
            {"template_id": "mul", "revision": 1, "bindings": {"k": 2, "id": 1}},
            {"template_id": "mul", "revision": 1, "bindings": {"k": 2, "id": 2}},
        ])])
        visibility_a = {0: {"rowcount": True}, 1: {"rowcount": True}}
        if public_read:
            visibility_a[2] = {"columns": [0]}
        slip_a = export_slip(receipt_a, counter_id="jia", sequence=1,
                             visibility=visibility_a)
        slip_b = export_slip(receipt_b, counter_id="yi", sequence=1,
                             visibility={0: {"rowcount": True}, 1: {"rowcount": True}})
        counters = [
            {"counter_id": "jia", "slips": [slip_a]},
            {"counter_id": "yi", "slips": [slip_b]},
        ]
        return counters, receipt_a["items"][2]["result"]["rows"][0][0]

    @staticmethod
    def terminal_xy(report):
        out = {}
        for terminal in report["terminals"]:
            qty = {row[1]: row[2] for row in terminal["rows"]}
            out[terminal["terminal_id"]] = (qty["X"], qty["Y"])
        return out

    # ---- 交错顺序搜索 -----------------------------------------------------
    def test_contention_yields_two_terminals(self):
        counters, read_x = self.demo_slips(public_read=False)
        self.assertEqual(read_x, 3)
        report = reconcile_round(self.snapshot, table="bins", counters=counters)
        self.assertEqual(report["status"], "explained")
        self.assertEqual(sorted(self.terminal_xy(report).values()), [(4, 16), (6, 14)])
        self.assertTrue(all(b["status"] == "explained" for b in report["batches"]))
        # 每个终局都带见证顺序与逐项核对证据。
        for terminal in report["terminals"]:
            self.assertEqual(len(terminal["witness_order"]), 2)
            checks = [
                item.get("check")
                for batch in terminal["evidence"]
                for item in batch["items"]
            ]
            self.assertIn("rowcount", checks)

    def test_public_read_pins_single_terminal(self):
        counters, _ = self.demo_slips(public_read=True)
        report = reconcile_round(self.snapshot, table="bins", counters=counters)
        self.assertEqual(report["status"], "explained")
        self.assertEqual(list(self.terminal_xy(report).values()), [(6, 14)])
        order = [o["batch_id"] for o in report["terminals"][0]["witness_order"]]
        self.assertEqual(order, ["a-1", "b-1"])

    def test_disjoint_counters_commute_to_one_terminal(self):
        [ra] = self.run_counter("c1", [("c1-1", [
            {"template_id": "add", "revision": 1, "bindings": {"d": 5, "id": 1}},
        ])])
        [rb] = self.run_counter("c2", [("c2-1", [
            {"template_id": "sub", "revision": 1, "bindings": {"d": 4, "id": 2}},
        ])])
        counters = [
            {"counter_id": "c1", "slips": [export_slip(
                ra, counter_id="c1", sequence=1, visibility={0: {"rowcount": True}})]},
            {"counter_id": "c2", "slips": [export_slip(
                rb, counter_id="c2", sequence=1, visibility={0: {"rowcount": True}})]},
        ]
        report = reconcile_round(self.snapshot, table="bins", counters=counters)
        self.assertEqual(report["status"], "explained")
        # 互不相交的主键：两种顺序都可行，但终局只有一个 (6, 5)。
        self.assertEqual(list(self.terminal_xy(report).values()), [(6, 5)])

    def test_contradictory_stamps_quarantine_all(self):
        counters, _ = self.demo_slips(public_read=True)  # 甲公开读 3 → 要求甲乙序
        [rb2] = self.run_counter("yi2", [("b2-1", [
            {"template_id": "mul", "revision": 1, "bindings": {"k": 2, "id": 1}},
            {"template_id": "mul", "revision": 1, "bindings": {"k": 2, "id": 2}},
            {"template_id": "read", "revision": 1, "bindings": {"id": 1}},
        ])])
        slip_b2 = export_slip(rb2, counter_id="yi", sequence=1,
                              visibility={0: {"rowcount": True}, 1: {"rowcount": True},
                                          2: {"columns": [0]}})  # 乙'公开读 2 → 要求乙甲序
        counters[1] = {"counter_id": "yi", "slips": [slip_b2]}
        report = reconcile_round(self.snapshot, table="bins", counters=counters)
        self.assertEqual(report["status"], "quarantined")
        self.assertEqual(report["terminals"], [])
        self.assertTrue(all(b["status"] == "quarantined" for b in report["batches"]))
        self.assertIn("reason", report)

    def test_conflicting_inserts_quarantine_all(self):
        items_a = [("ca-1", [{"template_id": "put", "revision": 1,
                              "bindings": {"id": 9, "name": "Z", "qty": 5}}])]
        items_b = [("cb-1", [{"template_id": "put", "revision": 1,
                              "bindings": {"id": 9, "name": "Z", "qty": 7}}])]
        [ra] = self.run_counter("ca", items_a)
        [rb] = self.run_counter("cb", items_b)
        counters = [
            {"counter_id": "ca", "slips": [export_slip(
                ra, counter_id="ca", sequence=1, visibility={0: {"rowcount": True}})]},
            {"counter_id": "cb", "slips": [export_slip(
                rb, counter_id="cb", sequence=1, visibility={0: {"rowcount": True}})]},
        ]
        # 两台争同一货位：都真诚盖章行数 1，但主键冲突使任何顺序都无法解释两者。
        report = reconcile_round(self.snapshot, table="bins", counters=counters)
        self.assertEqual(report["status"], "quarantined")

    def test_local_order_is_respected(self):
        # 甲机两个批次：先插 id=5 再给它加 1；本机顺序不可颠倒。
        [ra1] = self.run_counter("la", [("la-1", [
            {"template_id": "put", "revision": 1,
             "bindings": {"id": 5, "name": "W", "qty": 1}},
        ])])
        store = PlanStore(self.root / "store-la")
        executor = BatchExecutor(store, self.root / "la.db", self.root / "receipts-la")
        [ra2] = [executor.execute_batch("la-2", [
            {"template_id": "add", "revision": 1, "bindings": {"d": 1, "id": 5}},
        ])]
        [rb] = self.run_counter("lb", [("lb-1", [
            {"template_id": "add", "revision": 1, "bindings": {"d": 1, "id": 1}},
        ])])
        counters = [
            {"counter_id": "la", "slips": [
                export_slip(ra1, counter_id="la", sequence=1,
                            visibility={0: {"rowcount": True}}),
                export_slip(ra2, counter_id="la", sequence=2,
                            visibility={0: {"rowcount": True}}),
            ]},
            {"counter_id": "lb", "slips": [export_slip(
                rb, counter_id="lb", sequence=1, visibility={0: {"rowcount": True}})]},
        ]
        report = reconcile_round(self.snapshot, table="bins", counters=counters)
        self.assertEqual(report["status"], "explained")
        for terminal in report["terminals"]:
            qty = {row[0]: row[2] for row in terminal["rows"]}
            self.assertEqual(qty[5], 2)
            self.assertEqual(qty[1], 2)
        # 独立复核：违反本机顺序的交错直接拒绝。
        with self.assertRaises(ReconcileError):
            verify_order(self.snapshot, table="bins", counters=counters, order=[
                {"counter_id": "la", "batch_id": "la-2"},
                {"counter_id": "la", "batch_id": "la-1"},
                {"counter_id": "lb", "batch_id": "lb-1"},
            ])

    # ---- 独立复核 ---------------------------------------------------------
    def test_verify_order_independent_evidence(self):
        counters, _ = self.demo_slips(public_read=True)
        good = verify_order(self.snapshot, table="bins", counters=counters, order=[
            {"counter_id": "jia", "batch_id": "a-1"},
            {"counter_id": "yi", "batch_id": "b-1"},
        ])
        self.assertEqual(good["status"], "explained")
        qty = {row[1]: row[2] for row in good["final_state"]["rows"]}
        self.assertEqual((qty["X"], qty["Y"]), (6, 14))
        # 颠倒顺序：甲会读到 4 而不是盖章的 3 → 失败证据指到具体项。
        bad = verify_order(self.snapshot, table="bins", counters=counters, order=[
            {"counter_id": "yi", "batch_id": "b-1"},
            {"counter_id": "jia", "batch_id": "a-1"},
        ])
        self.assertEqual(bad["status"], "failed")
        self.assertEqual(bad["failed_at"], {"batch_id": "a-1", "item_index": 2})
        last = bad["evidence"][-1]["items"][-1]
        self.assertEqual(last["expected_rows"], [[3]])
        self.assertEqual(last["actual_rows"], [[4]])
        # 顺序必须恰好覆盖全部批次。
        with self.assertRaises(ReconcileError):
            verify_order(self.snapshot, table="bins", counters=counters, order=[
                {"counter_id": "jia", "batch_id": "a-1"},
            ])

    # ---- 模板限制 ---------------------------------------------------------
    def test_contract_rejects_other_table_function_and_unordered_public(self):
        # 引用其它表
        [ra] = self.run_counter("t1", [("t1-1", [
            {"template_id": "peek", "revision": 1, "bindings": {}},
        ])], templates={"peek": "SELECT id FROM other ORDER BY id"},
            setup_sql="CREATE TABLE other(id INTEGER PRIMARY KEY);")
        slip = export_slip(ra, counter_id="t1", sequence=1,
                           visibility={0: {"columns": [0]}})
        with self.assertRaises(ReconcileError):
            reconcile_round(self.snapshot, table="bins",
                            counters=[{"counter_id": "t1", "slips": [slip]}])

        # 函数调用（即使是确定性的内建函数也不放行）
        [rb] = self.run_counter("t2", [("t2-1", [
            {"template_id": "f", "revision": 1, "bindings": {}},
        ])], templates={"f": "SELECT abs(qty) FROM bins ORDER BY id"})
        slip_b = export_slip(rb, counter_id="t2", sequence=1,
                             visibility={0: {"columns": [0]}})
        with self.assertRaises(ReconcileError):
            reconcile_round(self.snapshot, table="bins",
                            counters=[{"counter_id": "t2", "slips": [slip_b]}])

        # 公开 SELECT 没有 ORDER BY 主键 → 拒绝；封存则放行
        [rc] = self.run_counter("t3", [("t3-1", [
            {"template_id": "u", "revision": 1, "bindings": {"id": 1}},
        ])], templates={"u": "SELECT qty FROM bins WHERE id = :id"})
        slip_c_open = export_slip(rc, counter_id="t3", sequence=1,
                                  visibility={0: {"columns": [0]}})
        with self.assertRaises(ReconcileError):
            reconcile_round(self.snapshot, table="bins",
                            counters=[{"counter_id": "t3", "slips": [slip_c_open]}])
        slip_c_sealed = export_slip(rc, counter_id="t3", sequence=1)
        report = reconcile_round(self.snapshot, table="bins",
                                 counters=[{"counter_id": "t3", "slips": [slip_c_sealed]}])
        self.assertEqual(report["status"], "explained")

    def test_contract_requires_integer_pk(self):
        bad_snapshot = self.root / "bad.db"
        with sqlite3.connect(bad_snapshot) as conn:
            conn.execute("CREATE TABLE t(id INT PRIMARY KEY, v INTEGER)")
            conn.execute("INSERT INTO t VALUES(1, 1)")
        [ra] = self.run_counter("pk", [("pk-1", [
            {"template_id": "g", "revision": 1, "bindings": {}},
        ])], templates={"g": "SELECT v FROM t ORDER BY id"},
            setup_sql="CREATE TABLE t(id INT PRIMARY KEY, v INTEGER);"
                      "INSERT INTO t VALUES(1, 1);")
        slip = export_slip(ra, counter_id="pk", sequence=1,
                           visibility={0: {"columns": [0]}})
        with self.assertRaises(ReconcileError):  # INT 不是 rowid 别名要求的 INTEGER
            reconcile_round(bad_snapshot, table="t",
                            counters=[{"counter_id": "pk", "slips": [slip]}])

    def test_zero_rows_null_and_duplicates_are_distinct_facts(self):
        nullable = self.root / "nullable.db"
        with sqlite3.connect(nullable) as conn:
            conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v INTEGER)")
            conn.execute("INSERT INTO t VALUES(1, NULL)")
        templates = {"get": "SELECT v FROM t ORDER BY id"}
        [ra] = self.run_counter("n1", [("n1-1", [
            {"template_id": "get", "revision": 1, "bindings": {}},
        ])], templates=templates,
            setup_sql="CREATE TABLE t(id INTEGER PRIMARY KEY, v INTEGER);"
                      "INSERT INTO t VALUES(1, NULL);")
        slip = export_slip(ra, counter_id="n1", sequence=1,
                           visibility={0: {"columns": [0]}})
        counters = [{"counter_id": "n1", "slips": [slip]}]
        # 真实结果是一行 NULL：可以解释。
        report = reconcile_round(nullable, table="t", counters=counters)
        self.assertEqual(report["status"], "explained")

        def tampered(rows):
            copy = json.loads(json.dumps(counters))
            copy[0]["slips"][0]["items"][0]["result"]["public"]["rows"] = rows
            return copy

        # 篡改成零行 → 无解；篡改成两条相同 NULL → 也无解。
        self.assertEqual(
            reconcile_round(nullable, table="t", counters=tampered([]))["status"],
            "quarantined",
        )
        self.assertEqual(
            reconcile_round(nullable, table="t", counters=tampered([[None], [None]]))["status"],
            "quarantined",
        )

    # ---- 限额 -------------------------------------------------------------
    def test_round_limits(self):
        counters, _ = self.demo_slips(public_read=False)
        with self.assertRaises(ReconcileError):
            reconcile_round(self.snapshot, table="bins", counters=counters, max_batches=1)
        with self.assertRaises(ReconcileError):
            reconcile_round(self.snapshot / "missing.db", table="bins", counters=counters)
        # 柜台号重复 / 批次号重复 / 本机序号倒退都拒绝。
        dup_counter = counters + [dict(counters[0])]
        with self.assertRaises(ReconcileError):
            reconcile_round(self.snapshot, table="bins", counters=dup_counter)
        dup_batch = [
            {"counter_id": "jia", "slips": counters[0]["slips"]},
            {"counter_id": "yi", "slips": counters[0]["slips"]},
        ]
        with self.assertRaises(ReconcileError):
            reconcile_round(self.snapshot, table="bins", counters=dup_batch)
        [ra] = self.run_counter("s1", [("s1-1", [
            {"template_id": "add", "revision": 1, "bindings": {"d": 1, "id": 1}},
        ])])
        store = PlanStore(self.root / "store-s1")
        executor = BatchExecutor(store, self.root / "s1.db", self.root / "receipts-s1")
        ra2 = executor.execute_batch("s1-2", [
            {"template_id": "add", "revision": 1, "bindings": {"d": 1, "id": 1}},
        ])
        out_of_order = [{"counter_id": "s1", "slips": [
            export_slip(ra2, counter_id="s1", sequence=2,
                        visibility={0: {"rowcount": True}}),
            export_slip(ra, counter_id="s1", sequence=1,
                        visibility={0: {"rowcount": True}}),
        ]}]
        with self.assertRaises(ReconcileError):
            reconcile_round(self.snapshot, table="bins", counters=out_of_order)


if __name__ == "__main__":
    unittest.main()
