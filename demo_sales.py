"""端到端驱动：分店销售册交接 + 总店清货榜，全部基于真实 SQLite 流水与计费链路。

场景对应题面：
  跑次 1：A 店 x=9、z=8；B 店 y=9、z=8；K=1，每店先给一页 → 封 z=16
          （z 不在任一店榜首，却是总店第一）。
  跑次 2：A 店新修订只纳入柜台，z 的 8 件全是网单；重取整组最新册 → 封 x=9。
  跑次 3：固定最初两册、z 列入暂停销售 → 仍封 x=9（x 与 y 同分，货号升序）。
  早市：  两店各加 1000 个互不重名、只售 1 件的尾部货号；每页 3、点查 1，
          预算 11 内封 z=16，并展示已查数与未见项上界为何足够；
          预算 6 时取完两个首行即停（未封榜、留续取位），追加到 11 后接同一册封榜；
          取 A 第一页期间出现的新册不得串版。
另含：负净数拒签、净零省略但可点查、店员负票自查、内存外存上限的直接演示。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from sql_param.clearance import HeadOfficeLink
from sql_param.errors import ClearanceError, LedgerError
from sql_param.planner import ClearancePlanner
from sql_param.plan_store import PlanStore
from sql_param.store_ledger import (
    NET_ALL_TEMPLATE,
    NET_COUNTER_TEMPLATE,
    SALES_SCHEMA,
    StoreSalesBook,
)

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "workdir_sales"
TEMPLATE_ID = "store_net_sales"


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(label: str, payload) -> None:
    print(f"--- {label} ---")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_store(store_id: str, root: Path, lines: list[tuple[str, str, int]]) -> StoreSalesBook:
    """建本店 SQLite 流水库并发布固定 SELECT 修订（r1 全渠道、r2 仅柜台）。"""
    db_path = root / f"{store_id}.db"
    store = StoreSalesBook(store_id, PlanStore(root / "plans"), db_path, root / "books")
    store.publish_revision(1, NET_ALL_TEMPLATE)
    store.publish_revision(2, NET_COUNTER_TEMPLATE)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SALES_SCHEMA)
        conn.executemany(
            "INSERT INTO sales_lines(channel, sku, qty) VALUES(?, ?, ?)", lines
        )
    return store


def fetch_log(verdict: dict) -> list[dict]:
    return verdict.get("ledger_log", [])


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    # ------------------------------------------------------------------
    section("跑次 1：A(x=9,z=8)  B(y=9,z=8)，K=1 —— 预期封 z=16")
    # ------------------------------------------------------------------
    r1 = WORK / "run1"
    store_a = build_store("A", r1, [
        ("counter", "x", 9),
        ("web", "z", 5), ("web", "z", 3),       # z 的 8 件都是网单
    ])
    store_b = build_store("B", r1, [
        ("counter", "y", 9),
        ("counter", "z", 8),
    ])
    book_a1 = store_a.issue_book(1)
    book_b1 = store_b.issue_book(1)
    show("A 册 1 首页", store_a.read_page(book_a1["book_no"], 1, 1))
    show("B 册 1 首页", store_b.read_page(book_b1["book_no"], 1, 1))

    link1 = HeadOfficeLink(
        {"A": store_a, "B": store_b},
        {"A": book_a1["book_no"], "B": book_b1["book_no"]},
        k=1, page_size=1, page_fee=1, point_fee=1, budget=10, work_dir=r1 / "rounds",
        round_id="r1",
    )
    verdict1 = ClearancePlanner(link1).run_until_seal()
    show("跑次 1 封榜结果", verdict1)
    assert verdict1["status"] == "sealed" and verdict1["winners"] == [{"sku": "z", "total_qty": 16}]
    print("计费明细：", fetch_log(verdict1))

    # 店员不接总店，自己拿负票核小计（含退货行）。
    check = store_a.check_negative_ticket(1, "z")
    show("A 店店员自查 z 的负票小计", check)
    assert check["net_qty"] == 8

    # ------------------------------------------------------------------
    section("跑次 2：A 店新修订只认柜台，z 全是网单 —— 重取整组后封 x=9")
    # ------------------------------------------------------------------
    r2 = WORK / "run2"
    store_a2 = build_store("A", r2, [
        ("counter", "x", 9),
        ("web", "z", 5), ("web", "z", 3),
    ])
    store_b2 = build_store("B", r2, [
        ("counter", "y", 9),
        ("counter", "z", 8),
    ])
    old_a = store_a2.issue_book(1)                 # 最初册（含 z）
    store_b2.issue_book(1)
    new_a = store_a2.issue_book(2)                 # 补票/改模板只生成新册
    show("A 店新册（r2 仅柜台）行集", new_a["rows"])
    assert new_a["rows"] == [["x", 9]]             # z 网单不再纳入，且不是零行，直接缺席
    assert old_a["book_no"] == 1 and new_a["book_no"] == 2

    # 一轮固定一组最新店册；旧册仍在磁盘上可审计，但本轮不串版。
    link2 = HeadOfficeLink(
        {"A": store_a2, "B": store_b2},
        {"A": new_a["book_no"], "B": 1},
        k=1, page_size=1, page_fee=1, point_fee=1, budget=10, work_dir=r2 / "rounds",
        round_id="r2",
    )
    verdict2 = ClearancePlanner(link2).run_until_seal()
    show("跑次 2 封榜结果", verdict2)
    assert verdict2["status"] == "sealed" and verdict2["winners"] == [{"sku": "x", "total_qty": 9}]

    # 点查旧册仍能拿到历史 z=8；新册点 z 为完全未知。
    show("A 旧册点查 z（历史证据）", store_a2.point_lookup(1, "z"))
    show("A 新册点查 z（网单不纳入）", store_a2.point_lookup(2, "z"))

    # ------------------------------------------------------------------
    section("跑次 3：固定最初两册，z 暂停销售 —— 同分按货号升序，封 x=9")
    # ------------------------------------------------------------------
    r3 = WORK / "run3"
    store_a3 = build_store("A", r3, [("counter", "x", 9), ("web", "z", 8)])
    store_b3 = build_store("B", r3, [("counter", "y", 9), ("counter", "z", 8)])
    ba3 = store_a3.issue_book(1)
    bb3 = store_b3.issue_book(1)
    link3 = HeadOfficeLink(
        {"A": store_a3, "B": store_b3},
        {"A": ba3["book_no"], "B": bb3["book_no"]},
        k=1, page_size=1, page_fee=1, point_fee=1, budget=10, work_dir=r3 / "rounds",
        round_id="r3", suspended=frozenset({"z"}),
    )
    verdict3 = ClearancePlanner(link3).run_until_seal()
    show("跑次 3 封榜结果（z 暂停）", verdict3)
    assert verdict3["status"] == "sealed" and verdict3["winners"] == [{"sku": "x", "total_qty": 9}]

    # ------------------------------------------------------------------
    section("早市：每店 +1000 个尾部货号，页费 3 / 点查 1，预算 11 封 z=16")
    # ------------------------------------------------------------------
    rm = WORK / "morning"
    a_lines = [("counter", "x", 9), ("web", "z", 8)]
    b_lines = [("counter", "y", 9), ("counter", "z", 8)]
    a_lines += [("counter", f"ta{i:04d}", 1) for i in range(1000)]   # 与 B 互不重名
    b_lines += [("counter", f"tb{i:04d}", 1) for i in range(1000)]
    morning_a = build_store("A", rm, a_lines)
    morning_b = build_store("B", rm, b_lines)
    mba = morning_a.issue_book(1)
    mbb = morning_b.issue_book(1)
    assert mba["row_count"] == 1002 and mbb["row_count"] == 1002

    def open_morning(round_id: str, budget: int) -> HeadOfficeLink:
        return HeadOfficeLink(
            {"A": morning_a, "B": morning_b},
            {"A": mba["book_no"], "B": mbb["book_no"]},
            k=1, page_size=1, page_fee=3, point_fee=1, budget=budget,
            work_dir=rm / "rounds", round_id=round_id,
        )

    link_m = open_morning("m11", 11)
    verdict_m = ClearancePlanner(link_m).run_until_seal()
    show("早市预算 11 封榜结果", verdict_m)
    assert verdict_m["status"] == "sealed" and verdict_m["winners"] == [{"sku": "z", "total_qty": 16}]
    assert verdict_m["spent"] <= 11
    snap = link_m.ranking_snapshot()
    show("封榜瞬间证据快照", {
        "spent": snap["spent"],
        "tail_max": snap["tail_max"],
        "unseen_upper": snap["unseen_upper"],
        "exact_known": snap["exact_known"],
        "resident_skus": snap["resident_skus"],
        "spilled_skus": snap["spilled_skus"],
    })
    print(
        "上界论证：封榜时两店尾最大值 1+8=9，即任何还没见过的货号合计至多 9 < z=16；\n"
        "x 经点查在 B 店缺席（确切零），合计确切 9；y 在 A 店未读、上界 9+1=10；\n"
        "两者都严格小于 z 的确切 16，所以 z 的第一名仅凭这些已付证据即可出票。"
    )

    # 已付回复重取免费：把已付费的 B 店 z 点查再取一次，不增加花费。
    spent_before = link_m.spent
    reply = link_m.point_lookup("B", "z")
    assert link_m.spent == spent_before and reply["net_qty"] == 8

    # ----- 预算 6：取完两个首行必须停在续取位，不能欠费、不能硬封 -----
    link_6 = open_morning("m6", 6)
    verdict_6 = ClearancePlanner(link_6).run_until_seal()
    show("早市预算 6（预期未封榜）", verdict_6)
    assert verdict_6["status"] == "unsealed"
    assert link_6.spent == 6 and link_6.next_page == {"A": 2, "B": 2}
    try:
        link_6.fetch_page("A")
        raise AssertionError("欠费动作未被拒绝")
    except ClearanceError as exc:
        print("欠费拒绝：", exc)

    # 追加到 11，接同一册续取（模拟断线：重开一个链路对象从磁盘恢复）。
    link_6.top_up(5)
    resumed = HeadOfficeLink(
        {"A": morning_a, "B": morning_b},
        {"A": mba["book_no"], "B": mbb["book_no"]},
        k=1, page_size=1, page_fee=3, point_fee=1, budget=11,
        work_dir=rm / "rounds", round_id="m6",
    )
    assert resumed.spent == 6 and resumed.next_page == {"A": 2, "B": 2}
    verdict_6b = ClearancePlanner(resumed).run_until_seal()
    show("追加预算后续取封榜", verdict_6b)
    assert verdict_6b["status"] == "sealed"
    assert verdict_6b["winners"] == [{"sku": "z", "total_qty": 16}]
    assert verdict_6b["spent"] <= 11

    # ----- 串版防护：m11 已读 A 第一页后，A 再出新册，本轮仍钉旧册 -----
    morning_a.issue_book(1)  # 同修订同数据重签 -> 新册号 2
    rogue_reply = morning_a.read_page(2, 1, 1)
    show("A 店期间新出的册（本轮不得使用）", {"book_no": rogue_reply["book_no"]})
    assert link_m.pinned == {"A": 1, "B": 1}
    assert link_m.fetch_page.__self__.pinned["A"] == 1
    show("m11 封榜审计所用店册", link_m.sealed_result["pinned_books"])

    # ------------------------------------------------------------------
    section("附加合同：负净数拒签 / 净零省略可点查 / 内存上限 2000 货号")
    # ------------------------------------------------------------------
    r4 = WORK / "contracts"
    neg_store = build_store("N", r4, [("counter", "p", 5), ("web", "p", -9)])
    try:
        neg_store.issue_book(1)
        raise AssertionError("负净数册未被拒签")
    except LedgerError as exc:
        print("拒签：", exc)

    zero_store = build_store("Z", r4, [
        ("counter", "q", 4), ("web", "q", -4),     # 净零
        ("counter", "w", 2),
    ])
    zbook = zero_store.issue_book(1)
    show("净零册页面（零行省略）", zero_store.read_page(zbook["book_no"], 1, 10))
    show("净零货号点查（确知为零）", zero_store.point_lookup(zbook["book_no"], "q"))
    assert zero_store.point_lookup(zbook["book_no"], "q")["net_qty"] == 0
    assert zero_store.point_lookup(zbook["book_no"], "ghost")["net_qty"] is None

    # 小内存上限：早市证据超过 2000 货号时溢写磁盘，封榜结论不变。
    link_cap = HeadOfficeLink(
        {"A": morning_a, "B": morning_b},
        {"A": mba["book_no"], "B": mbb["book_no"]},
        k=1, page_size=1, page_fee=3, point_fee=1, budget=11,
        work_dir=rm / "rounds", round_id="cap", max_resident_skus=3,
    )
    cap_verdict = ClearancePlanner(link_cap).run_until_seal()
    show("内存上限 3 货号的封榜结果（其余溢写临时磁盘）", {
        "status": cap_verdict["status"],
        "winners": cap_verdict.get("winners"),
        "resident_skus": link_cap.resident_sku_count(),
        "spilled_skus": link_cap.spilled_sku_count(),
    })
    assert cap_verdict["status"] == "sealed"
    assert link_cap.resident_sku_count() <= 3 and link_cap.spilled_sku_count() > 0
    link_cap.close()

    print("\n全部销售册/清货榜端到端断言通过。")
    print("工作目录：", WORK)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
