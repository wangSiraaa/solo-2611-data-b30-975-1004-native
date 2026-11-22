"""分店销售册（店榜）：固定 SELECT 修订 → 净计退货 → 不可变排序册 → 分页/点查。

店侧合同（对应题目"分店销售榜交接"）：

1. 每店的原始流水只留在本店 SQLite；总店永远拿不到流水，只能拿本店按固定
   ``template_id + revision`` 计划、经绑定读取出来的 ``(货号, 整数件数)`` 销售行。
2. 签发新册时跑一次固定 SELECT，按货号净计退货（同货号多行求和，含负数票）：
   - 任一货号净数为负 → 该册拒签（``LedgerError``），不产生册文件；
   - 净数为零的货号可省略，不出现在任何页面里，但册仍"确知其为零"，点查返回 0；
   - 每个货号在一册内最多一行。
3. 册是不可变快照，落盘到 ``<ledger_dir>/<store_id>/book<b>.json``，含签发时的
   计划修订、行集与指纹。改模板或补票只能 ``issue_book`` 生成新册，旧册永不覆盖；
   已送出的第一页只能续同一册——分页/点查接口都要求调用方固定册号。
4. 页面顺序固定：净数降序、货号升序；每页附"剩余未取部分的最大件数"
   （本页之后若还有行为其首行件数，空尾为 0）。
5. 点查返回该册内货号净数；册确知为零（SELECT 出过零行但页面省略）也返回 0，
   完全未知的货号返回 ``None``，由调用方区分。
6. 店员不接总店也能用 ``check_negative_ticket`` 核自己货号的小计：直接按同一固定
   修订读库做净计，不经过任何已签发册。
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .errors import LedgerError
from .plan_store import PlanStore

# 分店流水表的固定形状（由调用方在自己的连接里 executescript 建表）。
SALES_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS sales_lines ("
    "ticket_id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "channel TEXT NOT NULL, "
    "sku TEXT NOT NULL, "
    "qty INTEGER NOT NULL)"
)

# 全渠道净计模板：网单 + 柜台都纳入（修订 1）。
NET_ALL_TEMPLATE = (
    "SELECT sku, SUM(qty) AS qty "
    "FROM sales_lines WHERE sku IS NOT NULL GROUP BY sku"
)
# 仅柜台模板：新修订只纳入柜台记录（修订 2，网单不再纳入）。
NET_COUNTER_TEMPLATE = (
    "SELECT sku, SUM(qty) AS qty "
    "FROM sales_lines WHERE channel = 'counter' AND sku IS NOT NULL GROUP BY sku"
)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


class StoreSalesBook:
    """单店的计划存储、SQLite 流水库与不可变销售册。"""

    def __init__(
        self,
        store_id: str,
        plan_store: PlanStore,
        db_path: str | os.PathLike,
        ledger_dir: str | os.PathLike,
        template_id: str = "store_net_sales",
    ):
        if not isinstance(store_id, str) or not store_id:
            raise LedgerError("store_id must be a non-empty string")
        self.store_id = store_id
        self.plan_store = plan_store
        self.db_path = Path(db_path)
        self.ledger_dir = Path(ledger_dir)
        self.template_id = template_id
        self.store_dir = self.ledger_dir / store_id
        self.store_dir.mkdir(parents=True, exist_ok=True)

    # ---- 计划发布：固定修订，走既有 PlanStore 合同 ------------------------
    def publish_revision(self, revision: int, source: str) -> dict:
        return self.plan_store.publish(self.template_id, revision, source)

    def _book_path(self, book_no: int) -> Path:
        if not isinstance(book_no, int) or isinstance(book_no, bool) or book_no < 1:
            raise LedgerError("book number must be a positive integer")
        return self.store_dir / f"book{book_no}.json"

    # ---- 按固定修订绑定读取 (货号, 整数件数) 销售行 -----------------------
    def _read_net_rows(self, revision: int) -> list[tuple[str, int]]:
        """用保存的计划开独立连接执行固定 SELECT，并强制行形状 (str, int)。"""
        plan = self.plan_store.load(self.template_id, revision)
        sql = plan["sql"]
        if not sql.lstrip()[:6].upper() == "SELECT":
            raise LedgerError(f"{self.template_id} r{revision} saved plan is not a SELECT")
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(sql)
            columns = [desc[0] for desc in cursor.description]
            if len(columns) != 2:
                raise LedgerError(
                    f"{self.store_id}: net sales SELECT must return exactly 2 columns, "
                    f"got {columns}"
                )
            rows: list[tuple[str, int]] = []
            for sku, qty in cursor.fetchall():
                # 合同：货号必须是文本，件数必须是整数（bool 是 int 子类，明确拒绝）。
                if not isinstance(sku, str) or not sku:
                    raise LedgerError(f"{self.store_id}: non-text/empty sku in sales rows: {sku!r}")
                if isinstance(qty, bool) or not isinstance(qty, int):
                    raise LedgerError(f"{self.store_id}: non-integer qty for sku {sku!r}: {qty!r}")
                rows.append((sku, qty))
            return rows
        except sqlite3.Error as exc:
            raise LedgerError(f"{self.store_id}: sqlite error while reading net rows: {exc}") from exc
        finally:
            conn.close()

    # ---- 签发不可变册 -----------------------------------------------------
    def issue_book(self, revision: int) -> dict:
        """按固定修订读库、净计退货并签发下一册；净数为负拒签，零可省略。"""
        raw_rows = self._read_net_rows(revision)

        totals: dict[str, int] = {}
        for sku, qty in raw_rows:
            # SELECT 已带 GROUP BY，这里再兜底一次，保证一店一货号一行。
            totals[sku] = totals.get(sku, 0) + qty

        negatives = sorted(sku for sku, net in totals.items() if net < 0)
        if negatives:
            raise LedgerError(
                f"{self.store_id}: book refused, negative net qty for sku(s): "
                f"{', '.join(negatives)}"
            )

        # 净数为零可省略：页面行只保留正数；零集合单独落盘以支持点查"确知为零"。
        positive = sorted(
            ((sku, net) for sku, net in totals.items() if net > 0),
            key=lambda item: (-item[1], item[0]),
        )
        zero_skus = sorted(sku for sku, net in totals.items() if net == 0)

        existing = sorted(
            int(p.stem[4:])
            for p in self.store_dir.glob("book*.json")
            if p.stem.startswith("book") and p.stem[4:].isdigit()
        )
        book_no = (existing[-1] + 1) if existing else 1
        plan = self.plan_store.load(self.template_id, revision)
        book = {
            "format": "store-sales-book/v1",
            "store_id": self.store_id,
            "book_no": book_no,
            "template_id": self.template_id,
            "revision": revision,
            "plan_source": plan["source"],
            "page_order": "qty DESC, sku ASC",
            "rows": [[sku, qty] for sku, qty in positive],
            "zero_skus": zero_skus,
            "row_count": len(positive),
        }
        _atomic_write_json(self._book_path(book_no), book)
        return book

    def load_book(self, book_no: int) -> dict:
        path = self._book_path(book_no)
        if not path.exists():
            raise LedgerError(f"{self.store_id}: book {book_no} not found")
        with path.open("r", encoding="utf-8") as fh:
            book = json.load(fh)
        if book.get("format") != "store-sales-book/v1" or book.get("store_id") != self.store_id:
            raise LedgerError(f"{self.store_id}: book {book_no} failed integrity check")
        return book

    def latest_book_no(self) -> int | None:
        existing = sorted(
            int(p.stem[4:])
            for p in self.store_dir.glob("book*.json")
            if p.stem.startswith("book") and p.stem[4:].isdigit()
        )
        return existing[-1] if existing else None

    # ---- 分页：固定册号续取，页尾附剩余最大件数 ---------------------------
    def read_page(self, book_no: int, page_no: int, page_size: int) -> dict:
        if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size < 1:
            raise LedgerError("page_size must be a positive integer")
        if not isinstance(page_no, int) or isinstance(page_no, bool) or page_no < 1:
            raise LedgerError("page_no must be a positive integer (1-based)")
        book = self.load_book(book_no)
        rows = book["rows"]
        start = (page_no - 1) * page_size
        if start >= len(rows):
            # 空册（无正净数行）的第 1 页合法，返回空页与零尾；再往后是越页。
            if not (page_no == 1 and not rows):
                raise LedgerError(
                    f"{self.store_id} book{book_no}: page {page_no} is beyond the last page"
                )
        page_rows = rows[start:start + page_size]
        tail = rows[start + page_size:]
        # 剩余未取部分的最大件数：册按件数降序，首行即最大；空尾为零。
        tail_max = tail[0][1] if tail else 0
        return {
            "store_id": self.store_id,
            "book_no": book_no,
            "revision": book["revision"],
            "page_no": page_no,
            "page_size": page_size,
            "rows": [list(item) for item in page_rows],
            "tail_max": tail_max,
            "has_more": bool(tail),
        }

    # ---- 点查：含确知为零；册目录缺席也返回 None 由调用方定性 ------------
    def point_lookup(self, book_no: int, sku: str) -> dict:
        """点查货号在册内的净数。

        - 命中正数行或 ``zero_skus``：返回确切净数（``known_zero`` 区分零）；
        - 册目录缺席返回 ``net_qty=None``：店册是该店全部净计货号的完整目录，
          缺席即本店没有任何该货号流水（确切为零），总店链路据此按确切零入账，
          与"页还没读到、只能持尾最大值上界"的情形严格区分。
        """
        if not isinstance(sku, str) or not sku:
            raise LedgerError("sku must be a non-empty string")
        book = self.load_book(book_no)
        net: int | None = None
        known_zero = False
        for row_sku, qty in book["rows"]:
            if row_sku == sku:
                net = qty
                break
        if net is None and sku in book["zero_skus"]:
            net = 0
            known_zero = True
        return {
            "store_id": self.store_id,
            "book_no": book_no,
            "revision": book["revision"],
            "sku": sku,
            "net_qty": net,
            "known_zero": known_zero,
        }

    # ---- 店员自查负票小计：不经过任何册、不接总店也能核 -------------------
    def check_negative_ticket(self, revision: int, sku: str) -> dict:
        """按同一固定修订直接读库，返回该货号含负票的净计与逐笔明细。"""
        if not isinstance(sku, str) or not sku:
            raise LedgerError("sku must be a non-empty string")
        self.plan_store.load(self.template_id, revision)  # 修订必须已发布
        conn = sqlite3.connect(self.db_path)
        try:
            tickets = conn.execute(
                "SELECT ticket_id, channel, qty FROM sales_lines WHERE sku = ? ORDER BY ticket_id",
                (sku,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise LedgerError(f"{self.store_id}: sqlite error in negative ticket check: {exc}") from exc
        finally:
            conn.close()
        return {
            "store_id": self.store_id,
            "revision": revision,
            "sku": sku,
            "tickets": [list(item) for item in tickets],
            "net_qty": sum(item[2] for item in tickets),
        }
