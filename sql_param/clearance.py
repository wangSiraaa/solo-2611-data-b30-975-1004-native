"""总店清货名单：固定店册 + 按页/点查计费的取证链路 + 可证明的前 K 封榜。

总店合同（对应题目"总店清货名单"）：

- 一轮开始就固定一组店册 ``{store_id: book_no}``；分店之后再签发新册不影响本轮，
  已送出的第一页只能续同一册，任何跨册读取都被拒绝（不能串版）。
- 链路按整页计费（每页 ``page_fee``，与页内多少行无关）、点查按货号计费
  （每次 ``point_fee``）。每轮有实际取数余额，任何会欠费的动作直接拒绝；
  已付回复落盘，重取免费。中途断线重开进程，状态与已付回复都能续。
- 证据按写穿方式全部落在临时 SQLite，内存只保留最近使用的至多
  ``max_resident_skus``（默认 2000）个不同货号的缓存，夜间临时磁盘即此文件。
- 点查语义：店册是该店全部正净数货号的完整目录，册内缺席即该店此货号净数
  确切为零（与"已读页未覆盖、只持上界"严格区分），点查永远给出确定答案。
- 封榜只依据已读证据：
  * 每个货号对已读店取确切净数，对未读完且尚未点查的店只持有上界
    （该店当前页尾最大件数）；
  * 暂停销售货号即使有净数也不参选；
  * 当候选货号的确切合计严格大于一切未确定竞争者的上界（含"完全没见过的货号"
    这一抽象竞争者，其上界为各店尾最大值之和），名次才可出票；
  * 不能确定且余额不够下一步取证时，明确返回 ``unsealed`` 并保留每册续取页号。
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections import OrderedDict
from pathlib import Path

from .errors import ClearanceError
from .store_ledger import StoreSalesBook


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


class HeadOfficeLink:
    """一轮清货取证的计费链路、证据外存与封榜判定（状态全部可落盘续取）。"""

    def __init__(
        self,
        books: dict[str, StoreSalesBook],
        pinned: dict[str, int],
        k: int,
        page_size: int,
        page_fee: int,
        point_fee: int,
        budget: int,
        work_dir: str | os.PathLike,
        round_id: str = "round-1",
        suspended: frozenset[str] | set[str] | tuple[str, ...] = (),
        max_resident_skus: int = 2000,
    ):
        if not books or set(books) != set(pinned):
            raise ClearanceError("pinned book set must name exactly the available stores")
        if not isinstance(k, int) or isinstance(k, bool) or k < 1:
            raise ClearanceError("k must be a positive integer")
        for name, value in (("page_size", page_size), ("page_fee", page_fee),
                            ("point_fee", point_fee), ("max_resident_skus", max_resident_skus)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ClearanceError(f"{name} must be a positive integer")
        if not isinstance(budget, int) or isinstance(budget, bool) or budget < 0:
            raise ClearanceError("budget must be a non-negative integer")
        self.books = dict(books)
        self.pinned = dict(pinned)
        self.k = k
        self.page_size = page_size
        self.page_fee = page_fee
        self.point_fee = point_fee
        self.round_id = round_id
        self.suspended = frozenset(suspended)
        self.max_resident = max_resident_skus

        self.work_dir = Path(work_dir) / round_id
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.reply_dir = self.work_dir / "replies"
        self.reply_dir.mkdir(exist_ok=True)
        self.state_path = self.work_dir / "state.json"
        self.spill_path = self.work_dir / "evidence.sqlite"

        # 每册续取位置：下一个待取页号（1 起）。
        self.next_page: dict[str, int] = {store: 1 for store in sorted(self.books)}
        # 每册最近一页给出的"剩余未取最大件数"，初值无上界信息。
        self.tail_max: dict[str, int] = {store: None for store in sorted(self.books)}
        self.exhausted: dict[str, bool] = {store: False for store in sorted(self.books)}
        self.spent = 0
        self.budget = budget
        self.ledger_log: list[dict] = []
        self.sealed_result: dict | None = None

        # 内存 LRU 缓存：sku -> {store -> {"status", "net_qty", "upper_qty"}}。
        # 真正的证据主库是 SQLite，写穿落盘；缓存淘汰只丢内存、不丢证据。
        self._resident: OrderedDict[str, dict[str, dict]] = OrderedDict()
        self._spill_conn: sqlite3.Connection | None = None
        self._init_spill()
        self._restore()

    # ---- 落盘状态与临时磁盘主库 -------------------------------------------
    def _init_spill(self) -> None:
        conn = sqlite3.connect(self.spill_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS evidence ("
            "sku TEXT NOT NULL, store_id TEXT NOT NULL, status TEXT NOT NULL, "
            "net_qty INTEGER, upper_qty INTEGER NOT NULL, PRIMARY KEY(sku, store_id))"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_sku ON evidence(sku)")
        conn.commit()
        self._spill_conn = conn

    def _reply_path(self, kind: str, store: str, key: str) -> Path:
        return self.reply_dir / f"{kind}__{store}__{key}.json"

    def _save_state(self) -> None:
        payload = {
            "format": "clearance-round/v1",
            "round_id": self.round_id,
            "pinned": self.pinned,
            "k": self.k,
            "page_size": self.page_size,
            "page_fee": self.page_fee,
            "point_fee": self.point_fee,
            "budget": self.budget,
            "spent": self.spent,
            "suspended": sorted(self.suspended),
            "next_page": self.next_page,
            "tail_max": self.tail_max,
            "exhausted": self.exhausted,
            "max_resident_skus": self.max_resident,
            "ledger_log": self.ledger_log,
            "sealed_result": self.sealed_result,
        }
        _atomic_write_json(self.state_path, payload)

    def _restore(self) -> None:
        """断线重开：状态文件与已付回复、外存证据全部续上，不重复收费。"""
        if not self.state_path.exists():
            return
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if state["pinned"] != self.pinned:
            raise ClearanceError(
                f"{self.round_id}: pinned books on disk {state['pinned']} differ from {self.pinned}"
            )
        for field in ("k", "page_size", "page_fee", "point_fee"):
            if state[field] != getattr(self, field):
                raise ClearanceError(f"{self.round_id}: restored state disagrees on {field}")
        if state["max_resident_skus"] != self.max_resident:
            raise ClearanceError(f"{self.round_id}: restored state disagrees on max_resident_skus")
        if sorted(state["suspended"]) != sorted(self.suspended):
            raise ClearanceError(f"{self.round_id}: restored state disagrees on suspended")
        self.spent = state["spent"]
        self.next_page = dict(state["next_page"])
        self.tail_max = dict(state["tail_max"])
        self.exhausted = dict(state["exhausted"])
        self.ledger_log = list(state["ledger_log"])
        self.sealed_result = state["sealed_result"]
        # 证据以 SQLite 为准，内存只预热最近使用的若干货号（不超过内存上限）。
        self._resident.clear()
        assert self._spill_conn is not None
        rows = self._spill_conn.execute(
            "SELECT sku, store_id, status, net_qty, upper_qty FROM evidence "
            "ORDER BY sku, store_id"
        ).fetchall()
        warmed: dict[str, dict[str, dict]] = {}
        for sku, store, status, net, upper in rows:
            warmed.setdefault(sku, {})[store] = {
                "status": status, "net_qty": net, "upper_qty": upper,
            }
        for sku in list(warmed)[: self.max_resident]:
            self._resident[sku] = warmed[sku]

    # ---- 证据读写：SQLite 写穿 + 内存 LRU 缓存 ----------------------------
    def _enforce_resident_cap(self) -> None:
        while len(self._resident) > self.max_resident:
            self._resident.popitem(last=False)  # 仅丢缓存；证据已在 SQLite。

    def _get_evidence(self, sku: str) -> dict[str, dict]:
        if sku in self._resident:
            self._resident.move_to_end(sku)
            return self._resident[sku]
        assert self._spill_conn is not None
        rows = self._spill_conn.execute(
            "SELECT store_id, status, net_qty, upper_qty FROM evidence WHERE sku = ?",
            (sku,),
        ).fetchall()
        per_store = {
            store: {"status": status, "net_qty": net, "upper_qty": upper}
            for store, status, net, upper in rows
        }
        if per_store:
            self._resident[sku] = per_store
            self._resident.move_to_end(sku)
            self._enforce_resident_cap()
        return per_store

    def _put_store_evidence(self, sku: str, store: str, status: str,
                            net_qty: int | None, upper_qty: int) -> None:
        assert self._spill_conn is not None
        self._spill_conn.execute(
            "INSERT OR REPLACE INTO evidence VALUES(?, ?, ?, ?, ?)",
            (sku, store, status, net_qty, upper_qty),
        )
        self._spill_conn.commit()
        # 同步更新内存缓存（写穿）。
        per_store = self._resident.pop(sku, None)
        if per_store is None:
            old = self._spill_conn.execute(
                "SELECT store_id, status, net_qty, upper_qty FROM evidence WHERE sku = ?",
                (sku,),
            ).fetchall()
            per_store = {s: {"status": st, "net_qty": n, "upper_qty": u}
                         for s, st, n, u in old}
        per_store[store] = {"status": status, "net_qty": net_qty, "upper_qty": upper_qty}
        self._resident[sku] = per_store
        self._resident.move_to_end(sku)
        self._enforce_resident_cap()

    def resident_sku_count(self) -> int:
        """诊断用：当前内存缓存里的不同货号数（永不超过 max_resident_skus）。"""
        return len(self._resident)

    def spilled_sku_count(self) -> int:
        """诊断用：临时磁盘主库里的不同货号总数（证据全集）。"""
        assert self._spill_conn is not None
        return self._spill_conn.execute("SELECT COUNT(DISTINCT sku) FROM evidence").fetchone()[0]

    # ---- 计费动作 ---------------------------------------------------------
    def _charge(self, fee: int) -> None:
        if self.spent + fee > self.budget:
            raise ClearanceError(
                f"{self.round_id}: insufficient budget — need {fee}, "
                f"spent {self.spent}/{self.budget}"
            )

    def fetch_page(self, store: str) -> dict:
        """顺序取下一页；固定册号、整页计费，已付页重放免费。"""
        if store not in self.books:
            raise ClearanceError(f"unknown store {store!r}")
        if self.exhausted[store]:
            raise ClearanceError(f"{store}: pinned book already fully read")
        page_no = self.next_page[store]
        reply_path = self._reply_path("page", store, f"p{page_no}")
        if reply_path.exists():  # 已付回复重取免费
            return json.loads(reply_path.read_text(encoding="utf-8"))

        self._charge(self.page_fee)
        ledger = self.books[store]
        book_no = self.pinned[store]
        reply = ledger.read_page(book_no, page_no, self.page_size)
        if reply["revision"] != ledger.load_book(book_no)["revision"]:
            raise ClearanceError(f"{store}: book revision drift mid-round")  # 不可变册兜底
        self.spent += self.page_fee

        # 页内行：该店这些货号拿到确切净数；与同册更早证据冲突即合同损坏。
        for sku, qty in reply["rows"]:
            prior = self._get_evidence(sku).get(store)
            if prior is not None and prior["status"] == "known" and prior["net_qty"] != qty:
                raise ClearanceError(f"{store}: conflicting net qty for {sku} within one book")
            self._put_store_evidence(sku, store, "known", qty, qty)

        self.tail_max[store] = reply["tail_max"]
        self.exhausted[store] = not reply["has_more"]
        self.next_page[store] = page_no + 1
        self.ledger_log.append({
            "action": "page", "store": store, "book_no": book_no, "page_no": page_no,
            "fee": self.page_fee, "rows": len(reply["rows"]), "tail_max": reply["tail_max"],
        })
        _atomic_write_json(reply_path, reply)
        self._save_state()
        return reply

    def point_lookup(self, store: str, sku: str) -> dict:
        """点查指定货号在固定册的净数；册内缺席即确切为零。按货号计费，重放免费。"""
        if store not in self.books:
            raise ClearanceError(f"unknown store {store!r}")
        reply_path = self._reply_path("point", store, sku)
        if reply_path.exists():
            return json.loads(reply_path.read_text(encoding="utf-8"))

        self._charge(self.point_fee)
        ledger = self.books[store]
        book_no = self.pinned[store]
        reply = ledger.point_lookup(book_no, sku)
        self.spent += self.point_fee

        # 店册是完整目录：在册（正数行或 zero_skus）给确切净数；缺席也是确切零，
        # 因为该店没有任何可产生此货号净数的流水。点查永远不产生 pending 上界。
        net = reply["net_qty"] if reply["net_qty"] is not None else 0
        prior = self._get_evidence(sku).get(store)
        if prior is not None and prior["status"] == "known" and prior["net_qty"] != net:
            raise ClearanceError(f"{store}: conflicting net qty for {sku} within one book")
        self._put_store_evidence(sku, store, "known", net, net)

        self.ledger_log.append({
            "action": "point", "store": store, "book_no": book_no, "sku": sku,
            "fee": self.point_fee, "net_qty": net,
            "absent_zero": reply["net_qty"] is None,
        })
        _atomic_write_json(reply_path, reply)
        self._save_state()
        return reply

    def top_up(self, amount: int) -> None:
        """同轮追加预算；已付回复与续取位置保持不变。"""
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise ClearanceError("top-up amount must be a non-negative integer")
        self.budget += amount
        self._save_state()

    # ---- 上界、确切合计与封榜判定 -----------------------------------------
    def _all_known_skus(self) -> list[str]:
        assert self._spill_conn is not None
        return [row[0] for row in self._spill_conn.execute(
            "SELECT DISTINCT sku FROM evidence ORDER BY sku"
        ).fetchall()]

    def sku_bounds(self, sku: str) -> dict:
        """返回货号的下界（确切店求和）、上界（含未读店尾最大值）与逐店状态。"""
        per_store = self._get_evidence(sku)
        lower = 0
        upper = 0
        exact = True
        for store in sorted(self.books):
            info = per_store.get(store)
            if info is not None and info["status"] == "known":
                lower += info["net_qty"]
                upper += info["net_qty"]
            elif self.exhausted[store]:
                continue  # 整册读完仍无证据：该店净数确切为零。
            else:
                # 未读完且未点查：只能按该店当前尾最大值持有上界。
                tail = self.tail_max[store]
                if tail is None:
                    raise ClearanceError(
                        f"no bound available for {sku}: store {store} has never been read"
                    )
                upper += tail
                exact = False
        return {"sku": sku, "lower": lower, "upper": upper, "exact": exact,
                "per_store": per_store}

    def unseen_upper(self) -> int:
        """抽象竞争者"任何还没见过的货号"的总上界：各店当前尾最大值之和。"""
        if any(tail is None for tail in self.tail_max.values()):
            raise ClearanceError("every store must deliver its first page before sealing")
        return sum(tail for tail in self.tail_max.values() if tail is not None)

    def ranking_snapshot(self) -> dict:
        """供审计/演示：全部已知货号的界、暂停表与未见项上界。"""
        items = []
        for sku in self._all_known_skus():
            if sku in self.suspended:
                continue
            items.append(self.sku_bounds(sku))
        exact_items = sorted(
            (item for item in items if item["exact"] and item["lower"] > 0),
            key=lambda item: (-item["lower"], item["sku"]),
        )
        return {
            "round_id": self.round_id,
            "spent": self.spent,
            "budget": self.budget,
            "tail_max": dict(self.tail_max),
            "unseen_upper": self.unseen_upper(),
            "suspended": sorted(self.suspended),
            "exact_known": [{"sku": x["sku"], "total": x["lower"]} for x in exact_items],
            "bounded": [{"sku": x["sku"], "lower": x["lower"], "upper": x["upper"]}
                        for x in sorted(items, key=lambda x: (-x["upper"], x["sku"]))],
            "next_page": dict(self.next_page),
            "exhausted": dict(self.exhausted),
            "resident_skus": self.resident_sku_count(),
            "spilled_skus": self.spilled_sku_count(),
        }

    def try_seal(self) -> dict:
        """能从已读证据确定前 K 名就出票，否则返回未封榜原因与续取位置。"""
        if self.sealed_result is not None:
            return self.sealed_result
        if any(tail is None for tail in self.tail_max.values()):
            # 首页未齐时不存在任何上界推理，只能未封榜（由规划器继续取首页）。
            return {
                "status": "unsealed",
                "round_id": self.round_id,
                "reason": "first page of every pinned book is required before sealing",
                "spent": self.spent,
                "budget": self.budget,
                "resume_at": dict(self.next_page),
                "tail_max": dict(self.tail_max),
                "unseen_upper": None,
                "top_bounds": [],
            }
        unseen_bound = self.unseen_upper()
        bounds = {sku: self.sku_bounds(sku) for sku in self._all_known_skus()
                  if sku not in self.suspended}
        # 中选者本身必须确切：只有确切合计才能按（件数降序，货号升序）出票。
        exact = sorted(
            (b for b in bounds.values() if b["exact"] and b["lower"] > 0),
            key=lambda b: (-b["lower"], b["sku"]),
        )
        if len(exact) < self.k:
            return self._unsealed("fewer than K exact positive skus", bounds, unseen_bound)

        chosen = exact[:self.k]
        weakest = chosen[-1]
        chosen_skus = {item["sku"] for item in chosen}
        # 竞争者不要求确切：按它最乐观的可能（达到上界）在全局名次键
        # （件数降序，货号升序）上与最弱中选者比较，只有严格更靠前才可能翻盘。
        # 上界等于最弱件数时，货号名字典序更靠后的竞争者同分也赢不了，不构成威胁。
        for other in bounds.values():
            if other["sku"] in chosen_skus:
                continue
            if other["upper"] > weakest["lower"] or (
                other["upper"] == weakest["lower"] and other["sku"] < weakest["sku"]
            ):
                return self._unsealed(
                    f"sku {other['sku']} upper {other['upper']} can outrank slot {self.k}",
                    bounds, unseen_bound,
                )
        # 完全没见过的货号没有货号名：上界一旦等于最弱中选件数，就可能存在一个
        # 字典序更小的同名次货号插队，因此未见上界必须严格小于最弱中选件数。
        if unseen_bound >= weakest["lower"]:
            return self._unsealed(
                f"unseen sku upper {unseen_bound} can tie/beat slot {self.k}",
                bounds, unseen_bound,
            )

        result = {
            "status": "sealed",
            "round_id": self.round_id,
            "k": self.k,
            "winners": [{"sku": item["sku"], "total_qty": item["lower"]} for item in chosen],
            "spent": self.spent,
            "budget": self.budget,
            "pinned_books": dict(self.pinned),
            "suspended": sorted(self.suspended),
            "unseen_upper_at_seal": unseen_bound,
            "evidence_skus": len(self._all_known_skus()),
            "ledger_log": list(self.ledger_log),
        }
        self.sealed_result = result
        self._save_state()
        return result

    def _unsealed(self, reason: str, bounds: dict, unseen_bound: int) -> dict:
        return {
            "status": "unsealed",
            "round_id": self.round_id,
            "reason": reason,
            "spent": self.spent,
            "budget": self.budget,
            "resume_at": dict(self.next_page),
            "tail_max": dict(self.tail_max),
            "unseen_upper": unseen_bound,
            "top_bounds": sorted(
                ({"sku": b["sku"], "lower": b["lower"], "upper": b["upper"], "exact": b["exact"]}
                 for b in bounds.values()),
                key=lambda b: (-b["upper"], b["sku"]),
            )[:10],
        }

    def close(self) -> None:
        if self._spill_conn is not None:
            self._spill_conn.close()
            self._spill_conn = None
