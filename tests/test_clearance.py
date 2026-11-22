"""总店清货链路 HeadOfficeLink 与 ClearancePlanner 的合同测试。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sql_param.clearance import HeadOfficeLink
from sql_param.errors import ClearanceError
from sql_param.planner import ClearancePlanner
from sql_param.plan_store import PlanStore
from sql_param.store_ledger import NET_ALL_TEMPLATE, SALES_SCHEMA, StoreSalesBook


def make_store(root: Path, store_id: str, lines: list[tuple[str, str, int]]):
    db_path = root / f"{store_id}.db"
    store = StoreSalesBook(store_id, PlanStore(root / "plans"), db_path, root / "books")
    store.publish_revision(1, NET_ALL_TEMPLATE)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SALES_SCHEMA)
        conn.executemany("INSERT INTO sales_lines(channel, sku, qty) VALUES(?, ?, ?)", lines)
    return store


class ClearanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store_a = make_store(self.root, "A", [
            ("counter", "x", 9), ("web", "z", 8),
        ])
        self.store_b = make_store(self.root, "B", [
            ("counter", "y", 9), ("counter", "z", 8),
        ])
        self.ba = self.store_a.issue_book(1)["book_no"]
        self.bb = self.store_b.issue_book(1)["book_no"]

    def link(self, round_id, budget, **kw):
        return HeadOfficeLink(
            {"A": self.store_a, "B": self.store_b},
            {"A": self.ba, "B": self.bb},
            k=1, page_size=1, page_fee=1, point_fee=1, budget=budget,
            work_dir=self.root / "rounds", round_id=round_id, **kw,
        )

    def test_seal_second_place_in_each_store_wins_total(self):
        # z 在两店都不是榜首，但 8+8=16 压过 x=9、y=9。
        verdict = ClearancePlanner(self.link("r1", 20)).run_until_seal()
        self.assertEqual(verdict["status"], "sealed")
        self.assertEqual(verdict["winners"], [{"sku": "z", "total_qty": 16}])

    def test_suspended_skus_never_win(self):
        link = self.link("r2", 20, suspended=frozenset({"z"}))
        verdict = ClearancePlanner(link).run_until_seal()
        self.assertEqual(verdict["status"], "sealed")
        # x 与 y 同为 9，按货号升序 x 出票。
        self.assertEqual(verdict["winners"], [{"sku": "x", "total_qty": 9}])
        self.assertIn("z", verdict["suspended"])

    def test_billing_replay_free_and_budget_block(self):
        link = self.link("r3", 2)
        link.fetch_page("A")
        link.fetch_page("B")
        self.assertEqual(link.spent, 2)
        # 已付页重放免费（重放 A 的第 1 页）。
        link.next_page["A"] = 1
        link.fetch_page("A")
        self.assertEqual(link.spent, 2)
        link.next_page["A"] = 2
        with self.assertRaises(ClearanceError):
            link.fetch_page("A")  # 下一页要付费，余额不足拒付
        verdict = link.try_seal()
        self.assertEqual(verdict["status"], "unsealed")
        self.assertEqual(verdict["resume_at"], {"A": 2, "B": 2})

    def test_top_up_and_restart_resume_same_books(self):
        link = self.link("r4", 2)
        ClearancePlanner(link).run_until_seal()
        self.assertEqual(link.spent, 2)
        link.top_up(18)
        # 模拟断线：新对象从磁盘恢复同一轮，已付证据不重复收费。
        resumed = self.link("r4", 20)
        self.assertEqual(resumed.spent, 2)
        self.assertEqual(resumed.next_page, {"A": 2, "B": 2})
        verdict = ClearancePlanner(resumed).run_until_seal()
        self.assertEqual(verdict["winners"], [{"sku": "z", "total_qty": 16}])
        self.assertEqual(verdict["pinned_books"], {"A": 1, "B": 1})

    def test_new_book_during_round_cannot_swap_version(self):
        link = self.link("r5", 20)
        link.fetch_page("A")
        # A 店再出新册：本轮仍钉 book 1。
        self.store_a.issue_book(1)
        self.assertEqual(link.pinned["A"], 1)
        verdict = ClearancePlanner(link).run_until_seal()
        self.assertEqual(verdict["pinned_books"], {"A": 1, "B": 1})

    def test_point_lookup_absence_is_exact_zero(self):
        link = self.link("r6", 20)
        link.fetch_page("A")
        link.fetch_page("B")
        # x 只在 A 册；点 B 册缺席，按完整目录语义得确切零。
        reply = link.point_lookup("B", "x")
        self.assertEqual(reply["net_qty"], None)       # 店册 API 标 None
        bounds = link.sku_bounds("x")
        self.assertTrue(bounds["exact"])
        self.assertEqual(bounds["lower"], 9)

    def test_unseen_upper_blocks_premature_seal(self):
        link = self.link("r7", 2)
        link.fetch_page("A")
        link.fetch_page("B")
        # 两店尾最大值都是 8：未见货号上界 16，与 z 的潜在合计持平，不能封。
        self.assertEqual(link.unseen_upper(), 16)
        self.assertEqual(link.try_seal()["status"], "unsealed")

    def test_resident_memory_cap_with_spill(self):
        big_a = make_store(self.root, "BA",
                           [("counter", f"a{i:04d}", 3 - i % 3) for i in range(100)])
        big_b = make_store(self.root, "BB",
                           [("counter", f"b{i:04d}", 3 - i % 3) for i in range(100)])
        bna = big_a.issue_book(1)["book_no"]
        bnb = big_b.issue_book(1)["book_no"]
        link = HeadOfficeLink(
            {"BA": big_a, "BB": big_b}, {"BA": bna, "BB": bnb},
            k=1, page_size=10, page_fee=1, point_fee=1, budget=50,
            work_dir=self.root / "rounds", round_id="cap", max_resident_skus=5,
        )
        ClearancePlanner(link).run_until_seal()
        self.assertLessEqual(link.resident_sku_count(), 5)
        self.assertGreater(link.spilled_sku_count(), 5)
        # 磁盘主库持有全部证据，重开进程可恢复并续跑封榜。
        reopened = HeadOfficeLink(
            {"BA": big_a, "BB": big_b}, {"BA": bna, "BB": bnb},
            k=1, page_size=10, page_fee=1, point_fee=1, budget=500,
            work_dir=self.root / "rounds", round_id="cap", max_resident_skus=5,
        )
        self.assertEqual(reopened.try_seal()["status"], "unsealed")  # 恢复但证据未完
        verdict = ClearancePlanner(reopened).run_until_seal()
        self.assertEqual(verdict["status"], "sealed")
        self.assertLessEqual(reopened.resident_sku_count(), 5)


    def test_morning_budget_six_then_eleven(self):
        # 复刻题面早市：每店千个尾部货号，页费 3、点查 1。
        a = make_store(self.root, "MA",
                       [("counter", "x", 9), ("web", "z", 8)]
                       + [("counter", f"ta{i:04d}", 1) for i in range(1000)])
        b = make_store(self.root, "MB",
                       [("counter", "y", 9), ("counter", "z", 8)]
                       + [("counter", f"tb{i:04d}", 1) for i in range(1000)])
        bna, bnb = a.issue_book(1)["book_no"], b.issue_book(1)["book_no"]

        def open_round(rid, budget):
            return HeadOfficeLink(
                {"MA": a, "MB": b}, {"MA": bna, "MB": bnb},
                k=1, page_size=1, page_fee=3, point_fee=1, budget=budget,
                work_dir=self.root / "rounds", round_id=rid,
            )

        # 预算 6：只够两个首行，必须未封榜并停在续取位。
        short = open_round("six", 6)
        verdict = ClearancePlanner(short).run_until_seal()
        self.assertEqual(verdict["status"], "unsealed")
        self.assertEqual(short.spent, 6)
        self.assertEqual(short.next_page, {"MA": 2, "MB": 2})
        with self.assertRaises(ClearanceError):
            short.fetch_page("MA")

        # 追加到 11，重开进程接同一册续取并封 z=16，全程不超过 11。
        short.top_up(5)
        resumed = open_round("six", 11)
        self.assertEqual(resumed.spent, 6)
        sealed = ClearancePlanner(resumed).run_until_seal()
        self.assertEqual(sealed["status"], "sealed")
        self.assertEqual(sealed["winners"], [{"sku": "z", "total_qty": 16}])
        self.assertLessEqual(resumed.spent, 11)
        self.assertEqual(resumed.unseen_upper(), 9)  # 1+8，严格小于 16

    def test_k_two_tie_break_by_sku(self):
        a = make_store(self.root, "KA", [("counter", "m", 7), ("counter", "n", 7)])
        b = make_store(self.root, "KB", [("counter", "n", 2)])
        bna, bnb = a.issue_book(1)["book_no"], b.issue_book(1)["book_no"]
        link = HeadOfficeLink(
            {"KA": a, "KB": b}, {"KA": bna, "KB": bnb},
            k=2, page_size=1, page_fee=1, point_fee=1, budget=50,
            work_dir=self.root / "rounds", round_id="ktwo",
        )
        verdict = ClearancePlanner(link).run_until_seal()
        self.assertEqual(verdict["winners"],
                         [{"sku": "n", "total_qty": 9}, {"sku": "m", "total_qty": 7}])


if __name__ == "__main__":
    unittest.main()
