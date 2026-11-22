"""分店销售册 StoreSalesBook 的合同测试。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sql_param.errors import LedgerError
from sql_param.plan_store import PlanStore
from sql_param.store_ledger import (
    NET_ALL_TEMPLATE,
    NET_COUNTER_TEMPLATE,
    SALES_SCHEMA,
    StoreSalesBook,
)


class StoreLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db_path = root / "store.db"
        self.store = StoreSalesBook("S1", PlanStore(root / "plans"),
                                    self.db_path, root / "books")
        self.store.publish_revision(1, NET_ALL_TEMPLATE)
        self.store.publish_revision(2, NET_COUNTER_TEMPLATE)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(SALES_SCHEMA)

    def seed(self, lines):
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO sales_lines(channel, sku, qty) VALUES(?, ?, ?)", lines
            )

    def test_net_returns_sorting_and_tail_max(self):
        self.seed([("counter", "x", 9), ("web", "z", 5), ("web", "z", 3)])
        book = self.store.issue_book(1)
        # 件数降序、货号升序；z 的两笔网单净计为 8，一店一货号一行。
        self.assertEqual(book["rows"], [["x", 9], ["z", 8]])
        p1 = self.store.read_page(book["book_no"], 1, 1)
        self.assertEqual(p1["rows"], [["x", 9]])
        self.assertEqual(p1["tail_max"], 8)
        self.assertTrue(p1["has_more"])
        p2 = self.store.read_page(book["book_no"], 2, 1)
        self.assertEqual(p2["rows"], [["z", 8]])
        self.assertEqual(p2["tail_max"], 0)       # 空尾为零
        self.assertFalse(p2["has_more"])

    def test_negative_net_refuses_book(self):
        self.seed([("counter", "p", 5), ("web", "p", -9)])
        with self.assertRaises(LedgerError):
            self.store.issue_book(1)
        self.assertIsNone(self.store.latest_book_no())   # 拒签不落任何册

    def test_zero_omitted_from_pages_but_known_in_lookup(self):
        self.seed([("counter", "q", 4), ("web", "q", -4), ("counter", "w", 2)])
        book = self.store.issue_book(1)
        self.assertEqual(book["rows"], [["w", 2]])       # 零行省略
        self.assertEqual(book["zero_skus"], ["q"])
        self.assertEqual(self.store.point_lookup(1, "q")["net_qty"], 0)
        self.assertTrue(self.store.point_lookup(1, "q")["known_zero"])
        # 册目录缺席：net_qty 为 None（总店链路按确切零入账）。
        self.assertIsNone(self.store.point_lookup(1, "ghost")["net_qty"])

    def test_counter_only_revision_drops_web_sales(self):
        self.seed([("counter", "x", 9), ("web", "z", 8)])
        book1 = self.store.issue_book(1)
        book2 = self.store.issue_book(2)
        self.assertEqual({r[0] for r in book1["rows"]}, {"x", "z"})
        self.assertEqual(book2["rows"], [["x", 9]])      # 网单 z 不再纳入
        self.assertEqual(book1["book_no"], 1)
        self.assertEqual(book2["book_no"], 2)            # 新册不覆盖旧册
        # 旧册仍可点查历史证据。
        self.assertEqual(self.store.point_lookup(1, "z")["net_qty"], 8)

    def test_books_are_immutable_and_page_beyond_end_rejected(self):
        self.seed([("counter", "x", 1)])
        self.store.issue_book(1)
        with self.assertRaises(LedgerError):
            self.store.read_page(1, 2, 1)
        with self.assertRaises(LedgerError):
            self.store.read_page(9, 1, 1)

    def test_negative_ticket_self_check_without_head_office(self):
        self.seed([("counter", "z", 5), ("web", "z", -2), ("web", "z", 5)])
        check = self.store.check_negative_ticket(1, "z")
        self.assertEqual(check["net_qty"], 8)
        self.assertEqual([t[2] for t in check["tickets"]], [5, -2, 5])

    def test_bad_row_shape_rejected(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE other(sku TEXT, qty TEXT)")
            conn.execute("INSERT INTO other VALUES('a', 'x')")
        self.store.publish_revision(3, "SELECT sku, qty FROM other")
        with self.assertRaises(LedgerError):
            self.store.issue_book(3)


if __name__ == "__main__":
    unittest.main()
