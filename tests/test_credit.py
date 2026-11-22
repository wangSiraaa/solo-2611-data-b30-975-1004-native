import json
import tempfile
import unittest
from pathlib import Path

from sql_param.credit import (
    LEDGER_FORMAT,
    CreditDesk,
    capacities_from_report,
)
from sql_param.errors import CreditError
from sql_param.reconcile import RECONCILE_FORMAT

# 题目场景的两个可行终局：甲乙 (6,14)、乙甲 (4,16)
SEALED_TERMINALS = [
    {"terminal_id": "t1", "capacities": {"X": 6, "Y": 14}},
    {"terminal_id": "t2", "capacities": {"X": 4, "Y": 16}},
]
OPEN_TERMINALS = [{"terminal_id": "t1", "capacities": {"X": 6, "Y": 14}}]

P = {"application_id": "P", "quantity": 5, "bins": ["X"], "value": 8}
Q = {"application_id": "Q", "quantity": 16, "bins": ["X", "Y"], "value": 6}


def make_report(terminals, status="explained"):
    rows_by_terminal = {
        "t1": [[1, "X", 6], [2, "Y", 14]],
        "t2": [[1, "X", 4], [2, "Y", 16]],
    }
    return {
        "format": RECONCILE_FORMAT,
        "table": "bins",
        "pk_column": "id",
        "status": status,
        "terminals": [
            {"terminal_id": t["terminal_id"], "columns": ["id", "name", "qty"],
             "rows": rows_by_terminal[t["terminal_id"]]}
            for t in terminals
        ],
        "batches": [],
    }


class CreditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = self.root / "ledger.json"

    def desk(self, terminals, ledger=None):
        return CreditDesk(ledger or self.ledger, terminals)

    # ---- 题目场景 ---------------------------------------------------------
    def test_round1_only_q_signable_without_commitments(self):
        desk = self.desk(SEALED_TERMINALS)
        evaluation = desk.evaluate([P, Q])
        self.assertEqual(evaluation["best"], {"application_ids": ["Q"], "total_value": 6})
        self.assertEqual(evaluation["solo_signable"], {"P": False, "Q": True})
        witness = desk.gap_witness(P)
        self.assertEqual(witness["terminal_id"], "t2")
        self.assertEqual(witness["deficient_orders"], ["P"])
        self.assertEqual(
            (witness["demand"], witness["available"], witness["deficit"]), (5, 4, 1)
        )
        self.assertIsNone(desk.gap_witness(Q))

    def test_round1_allocation_in_specified_terminal(self):
        desk = self.desk(SEALED_TERMINALS)
        desk.sign([P, Q])
        self.assertEqual([e["order_id"] for e in desk.signed()], ["Q"])
        allocation = desk.allocate("t1")
        self.assertEqual(allocation["orders"], {"Q": {"X": 6, "Y": 10}})
        self.assertEqual(allocation["total_demand"], 16)
        allocation_t2 = desk.allocate("t2")
        self.assertEqual(allocation_t2["orders"], {"Q": {"X": 4, "Y": 12}})
        with self.assertRaises(CreditError):
            desk.allocate("t-unknown")

    def test_round2_single_terminal_signs_p_only(self):
        desk = self.desk(OPEN_TERMINALS)
        evaluation = desk.evaluate([P, Q])
        # P、Q 各自都可签，但合要 21 件超过终局 20 件 → 只签价值更高的 P。
        self.assertEqual(evaluation["solo_signable"], {"P": True, "Q": True})
        self.assertEqual(evaluation["best"], {"application_ids": ["P"], "total_value": 8})
        desk.sign([P, Q])
        witness = desk.gap_witness(Q)
        self.assertEqual(witness["deficient_orders"], ["P", "Q"])
        self.assertEqual(
            (witness["demand"], witness["available"], witness["deficit"]), (21, 20, 1)
        )
        self.assertEqual(desk.allocate("t1")["orders"], {"P": {"X": 5}})

    def test_round3_existing_flexible_commitment_blocks_both(self):
        desk = self.desk(SEALED_TERMINALS)
        desk.register_commitment("flex-0", 5, ["X", "Y"])
        evaluation = desk.evaluate([P, Q])
        self.assertEqual(evaluation["best"], {"application_ids": [], "total_value": 0})
        self.assertEqual(evaluation["solo_signable"], {"P": False, "Q": False})
        witness_q = desk.gap_witness(Q)
        self.assertEqual(witness_q["deficient_orders"], ["flex-0", "Q"])
        self.assertEqual(witness_q["deficit"], 1)

    # ---- 账簿与签发 -------------------------------------------------------
    def test_signing_persists_for_next_officer(self):
        desk = self.desk(SEALED_TERMINALS)
        desk.sign([P, Q])
        # 下一位授信员重开同一账簿：仍生效的旧承诺必须算入。
        next_desk = self.desk(SEALED_TERMINALS)
        self.assertEqual(next_desk.signed(), desk.signed())
        evaluation = next_desk.evaluate([
            {"application_id": "R", "quantity": 4, "bins": ["X", "Y"], "value": 1},
        ])
        # Q 已占 16 件，两个终局都只剩 4 件可用。
        self.assertEqual(evaluation["best"], {"application_ids": ["R"], "total_value": 1})
        bigger = {"application_id": "S", "quantity": 5, "bins": ["X", "Y"], "value": 1}
        self.assertEqual(next_desk.evaluate([bigger])["best"]["application_ids"], [])

    def test_same_application_cannot_be_signed_twice(self):
        desk = self.desk(SEALED_TERMINALS)
        desk.sign([P, Q])
        with self.assertRaises(CreditError):
            desk.evaluate([Q])
        with self.assertRaises(CreditError):
            desk.gap_witness(Q)
        # 账簿文件本身也防重复。
        with self.assertRaises(CreditError):
            desk.register_commitment("Q", 1, ["X"])

    def test_sign_explicit_subset_must_be_jointly_feasible(self):
        desk = self.desk(OPEN_TERMINALS)
        with self.assertRaises(CreditError):
            desk.sign([P, Q], application_ids=["P", "Q"])
        # 显式签一个可行子集允许。
        entries = desk.sign([P, Q], application_ids=["Q"])
        self.assertEqual([e["order_id"] for e in entries], ["Q"])

    def test_ledger_file_format_and_tamper_detection(self):
        desk = self.desk(SEALED_TERMINALS)
        desk.register_commitment("c-1", 2, ["X"], value=3)
        payload = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(payload["format"], LEDGER_FORMAT)
        self.assertEqual(payload["signed"][0]["order_id"], "c-1")
        # 篡改账簿：旧承诺超过供给 → 重开即拒绝。
        payload["signed"][0]["quantity"] = 99
        self.ledger.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CreditError):
            self.desk(SEALED_TERMINALS)

    def test_register_commitment_requires_feasibility(self):
        desk = self.desk(SEALED_TERMINALS)
        with self.assertRaises(CreditError):
            desk.register_commitment("too-big", 21, ["X", "Y"])
        with self.assertRaises(CreditError):
            desk.register_commitment("bad-bin", 1, ["Z"])
        self.assertEqual(desk.signed(), [])

    # ---- 输入合同 ---------------------------------------------------------
    def test_limits_and_validation(self):
        with self.assertRaises(CreditError):  # 没有可行终局（回单被隔离）不建席
            self.desk([])
        seven_bins = [{"terminal_id": "t", "capacities": {f"b{i}": 1 for i in range(7)}}]
        with self.assertRaises(CreditError):
            self.desk(seven_bins)
        desk = self.desk(SEALED_TERMINALS)
        many = [
            {"application_id": f"a{i}", "quantity": 1, "bins": ["X"], "value": 1}
            for i in range(13)
        ]
        with self.assertRaises(CreditError):  # 每轮至多 12 张申请
            desk.evaluate(many)
        with self.assertRaises(CreditError):  # 未知货位
            desk.evaluate([{"application_id": "Z", "quantity": 1, "bins": ["Z"], "value": 1}])
        with self.assertRaises(CreditError):  # 数量必须为正整数
            desk.evaluate([{"application_id": "Z", "quantity": 0, "bins": ["X"], "value": 1}])
        with self.assertRaises(CreditError):  # bool 不是合法数量
            desk.evaluate([{"application_id": "Z", "quantity": True, "bins": ["X"], "value": 1}])
        with self.assertRaises(CreditError):  # 重复申请号
            desk.evaluate([P, dict(P)])

    def test_max_value_tie_is_deterministic(self):
        terminals = [{"terminal_id": "t", "capacities": {"X": 5, "Y": 5}}]
        desk = self.desk(terminals)
        a = {"application_id": "A", "quantity": 5, "bins": ["X"], "value": 4}
        b = {"application_id": "B", "quantity": 5, "bins": ["Y"], "value": 4}
        c = {"application_id": "C", "quantity": 10, "bins": ["X", "Y"], "value": 9}
        # {A,B}=8 与 {C}=9：唯一最大是 C。
        evaluation = desk.evaluate([a, b, c])
        self.assertEqual(evaluation["best"], {"application_ids": ["C"], "total_value": 9})
        # {A,B}=8 与 {C}=8 同分：取申请号字典序最小者。
        c2 = {"application_id": "C", "quantity": 10, "bins": ["X", "Y"], "value": 8}
        evaluation2 = desk.evaluate([a, b, c2])
        self.assertEqual(
            evaluation2["best"], {"application_ids": ["A", "B"], "total_value": 8}
        )

    # ---- 报告转换 ---------------------------------------------------------
    def test_capacities_from_report(self):
        report = make_report([SEALED_TERMINALS[0], SEALED_TERMINALS[1]])
        terminals = capacities_from_report(report, bins={"X": 1, "Y": 2},
                                           quantity_column="qty")
        self.assertEqual(terminals, SEALED_TERMINALS)
        with self.assertRaises(CreditError):  # 隔离报告没有可信终局
            capacities_from_report(make_report([], status="quarantined"),
                                   bins={"X": 1}, quantity_column="qty")
        with self.assertRaises(CreditError):  # 终局缺货位行
            capacities_from_report(report, bins={"X": 1, "Z": 9}, quantity_column="qty")
        with self.assertRaises(CreditError):  # 缺数量列
            capacities_from_report(report, bins={"X": 1}, quantity_column="missing")


if __name__ == "__main__":
    unittest.main()
