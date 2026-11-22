import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sql_param.errors import SlipError
from sql_param.executor import BatchExecutor
from sql_param.plan_store import PlanStore
from sql_param.slips import SLIP_FORMAT, export_slip, validate_slip

SCHEMA = "CREATE TABLE t(id INTEGER PRIMARY KEY, v INTEGER, s TEXT);"


class SlipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db_path = root / "data.db"
        self.store = PlanStore(root / "store")
        self.executor = BatchExecutor(self.store, self.db_path, root / "receipts")
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(SCHEMA)
        self.store.publish("put", 1, "INSERT INTO t(id, v, s) VALUES(:id, :v, :s)")
        self.store.publish("bump", 1, "UPDATE t SET v = v + :d WHERE id = :id")
        self.store.publish("get", 1, "SELECT id, v, s FROM t ORDER BY id")
        self.store.publish("one", 1, "SELECT v FROM t WHERE id = :id ORDER BY id")
        self.store.publish("by-v", 1, "SELECT v, s FROM t WHERE v = :v ORDER BY id")

    def run_batch(self, batch_id, items):
        receipt = self.executor.execute_batch(batch_id, items)
        self.assertEqual(receipt["status"], "committed")
        return receipt

    # ---- 出具 -------------------------------------------------------------
    def test_export_stamps_and_public_projection(self):
        receipt = self.run_batch("b1", [
            {"template_id": "put", "revision": 1,
             "bindings": {"id": 1, "v": 5, "s": "x"}},
            {"template_id": "bump", "revision": 1, "bindings": {"d": 2, "id": 1}},
            {"template_id": "get", "revision": 1, "bindings": {}},
        ])
        slip = export_slip(
            receipt, counter_id="c1", sequence=1,
            visibility={1: {"rowcount": True}, 2: {"columns": [0, 2]}},
        )
        self.assertEqual(slip["format"], SLIP_FORMAT)
        self.assertEqual(slip["counter_id"], "c1")
        self.assertEqual(slip["sequence"], 1)
        self.assertEqual(slip["batch_id"], "b1")
        # 未声明的项完全保密：不写任何结果。
        self.assertEqual(slip["items"][0]["result"], {"kind": "insert"})
        self.assertEqual(slip["items"][1]["result"],
                         {"kind": "update", "stamped_rowcount": 1})
        self.assertEqual(
            slip["items"][2]["result"],
            {"kind": "select",
             "public": {"positions": [0, 2], "columns": ["id", "s"], "rows": [[1, "x"]]}},
        )
        # 计划快照与绑定值随条携带。
        self.assertEqual(slip["items"][1]["plan"]["sql"],
                         "UPDATE t SET v = v + ? WHERE id = ?")
        self.assertEqual(slip["items"][1]["ordered_values"], [2, 1])
        validate_slip(slip)

    def test_export_requires_committed_receipt(self):
        self.run_batch("seed", [
            {"template_id": "put", "revision": 1,
             "bindings": {"id": 1, "v": 1, "s": None}},
        ])
        rolled = self.executor.execute_batch("bad", [
            {"template_id": "put", "revision": 1,
             "bindings": {"id": 2, "v": 2, "s": None}},
            {"template_id": "put", "revision": 1,
             "bindings": {"id": 1, "v": 3, "s": None}},
        ])
        self.assertEqual(rolled["status"], "rolled_back")
        with self.assertRaises(SlipError):
            export_slip(rolled, counter_id="c1", sequence=1)

    def test_export_checks_indices_positions_and_spec_kind(self):
        receipt = self.run_batch("b2", [
            {"template_id": "put", "revision": 1,
             "bindings": {"id": 1, "v": None, "s": "y"}},
            {"template_id": "one", "revision": 1, "bindings": {"id": 1}},
        ])
        with self.assertRaises(SlipError):  # 不存在的 item 下标
            export_slip(receipt, counter_id="c1", sequence=1, visibility={5: {"rowcount": True}})
        with self.assertRaises(SlipError):  # 列位越界
            export_slip(receipt, counter_id="c1", sequence=1, visibility={1: {"columns": [1]}})
        with self.assertRaises(SlipError):  # 列位重复
            export_slip(receipt, counter_id="c1", sequence=1, visibility={1: {"columns": [0, 0]}})
        with self.assertRaises(SlipError):  # SELECT 项不接受 rowcount 声明
            export_slip(receipt, counter_id="c1", sequence=1, visibility={1: {"rowcount": True}})
        with self.assertRaises(SlipError):  # 写操作项不接受 columns 声明
            export_slip(receipt, counter_id="c1", sequence=1, visibility={0: {"columns": [0]}})
        with self.assertRaises(SlipError):  # 非法柜台号
            export_slip(receipt, counter_id="bad counter!", sequence=1)
        with self.assertRaises(SlipError):  # 非法序号
            export_slip(receipt, counter_id="c1", sequence=0)

    def test_export_distinguishes_zero_rows_null_and_duplicates(self):
        # 零行
        empty = self.run_batch("q-empty", [
            {"template_id": "one", "revision": 1, "bindings": {"id": 99}},
        ])
        # 一行 NULL
        self.run_batch("seed-null", [
            {"template_id": "put", "revision": 1,
             "bindings": {"id": 1, "v": None, "s": None}},
        ])
        one_null = self.run_batch("q-null", [
            {"template_id": "one", "revision": 1, "bindings": {"id": 1}},
        ])
        # 两条相同记录（用按值过滤的查询，避开前面 seed 的 NULL 行）
        self.run_batch("seed-dup", [
            {"template_id": "put", "revision": 1,
             "bindings": {"id": 2, "v": 7, "s": "a"}},
            {"template_id": "put", "revision": 1,
             "bindings": {"id": 3, "v": 7, "s": "a"}},
        ])
        dup = self.run_batch("q-dup", [
            {"template_id": "by-v", "revision": 1, "bindings": {"v": 7}},
        ])
        vis = {0: {"columns": [0]}}
        rows_empty = export_slip(empty, counter_id="c", sequence=1, visibility=vis)[
            "items"][0]["result"]["public"]["rows"]
        vis_null = {0: {"columns": [0]}}
        rows_null = export_slip(one_null, counter_id="c", sequence=1, visibility=vis_null)[
            "items"][0]["result"]["public"]["rows"]
        rows_dup = export_slip(dup, counter_id="c", sequence=1,
                               visibility={0: {"columns": [0, 1]}})[
            "items"][0]["result"]["public"]["rows"]
        self.assertEqual(rows_empty, [])
        self.assertEqual(rows_null, [[None]])
        self.assertEqual(rows_dup, [[7, "a"], [7, "a"]])
        # 三者互不相同的载荷
        self.assertEqual(len({json.dumps(r, sort_keys=True) for r in
                              (rows_empty, rows_null, rows_dup)}), 3)

    def test_export_preserves_value_types_and_rejects_bool(self):
        receipt = self.run_batch("typed", [
            {"template_id": "put", "revision": 1,
             "bindings": {"id": 1, "v": 0, "s": ""}},
            {"template_id": "get", "revision": 1, "bindings": {}},
        ])
        slip = export_slip(receipt, counter_id="c", sequence=1,
                           visibility={1: {"columns": [1, 2]}})
        self.assertEqual(slip["items"][1]["result"]["public"]["rows"], [[0, ""]])

        tampered = json.loads(json.dumps(receipt))
        tampered["items"][1]["result"]["rows"][0][1] = True  # 伪造 bool 结果值
        with self.assertRaises(SlipError):
            export_slip(tampered, counter_id="c", sequence=1,
                        visibility={1: {"columns": [1]}})

        tampered2 = json.loads(json.dumps(receipt))
        tampered2["items"][0]["result"]["rowcount"] = -1  # 非法行数
        with self.assertRaises(SlipError):
            export_slip(tampered2, counter_id="c", sequence=1,
                        visibility={0: {"rowcount": True}})

    def test_export_checks_receipt_integrity(self):
        receipt = self.run_batch("integ", [
            {"template_id": "put", "revision": 1,
             "bindings": {"id": 1, "v": 1, "s": None}},
        ])
        bad_index = json.loads(json.dumps(receipt))
        bad_index["items"][0]["index"] = 7
        with self.assertRaises(SlipError):
            export_slip(bad_index, counter_id="c", sequence=1)

        bad_values = json.loads(json.dumps(receipt))
        bad_values["items"][0]["ordered_values"] = [1]  # 计划有三个参数
        with self.assertRaises(SlipError):
            export_slip(bad_values, counter_id="c", sequence=1)

    # ---- 独立校验 ---------------------------------------------------------
    def test_validate_slip_rejects_tampering(self):
        receipt = self.run_batch("val", [
            {"template_id": "bump", "revision": 1, "bindings": {"d": 1, "id": 1}},
            {"template_id": "one", "revision": 1, "bindings": {"id": 1}},
        ])
        slip = export_slip(receipt, counter_id="c", sequence=1,
                           visibility={0: {"rowcount": True}, 1: {"columns": [0]}})
        validate_slip(slip)

        def mutated(mutate):
            copy = json.loads(json.dumps(slip))
            mutate(copy)
            return copy

        with self.assertRaises(SlipError):
            validate_slip(mutated(lambda s: s.update(format="other")))
        with self.assertRaises(SlipError):
            validate_slip(mutated(lambda s: s.update(status="rolled_back")))
        with self.assertRaises(SlipError):
            validate_slip(mutated(
                lambda s: s["items"][0]["result"].update(stamped_rowcount=-1)))
        with self.assertRaises(SlipError):
            validate_slip(mutated(
                lambda s: s["items"][0].update(result={"kind": "update", "stamped_rowcount": True})))
        with self.assertRaises(SlipError):  # 公开行宽度与列位不符
            validate_slip(mutated(
                lambda s: s["items"][1]["result"]["public"].update(rows=[[1, 2]])))
        with self.assertRaises(SlipError):  # 公开值混入 bool
            validate_slip(mutated(
                lambda s: s["items"][1]["result"]["public"].update(rows=[[True]])))
        with self.assertRaises(SlipError):  # 下标不连续
            validate_slip(mutated(lambda s: s["items"][1].update(index=5)))


if __name__ == "__main__":
    unittest.main()
