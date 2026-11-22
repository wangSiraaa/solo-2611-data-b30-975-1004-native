"""离线回单合账 + 授信签单 端到端演示（题目给定场景）。

场景：货位 X 初值 1、Y 初值 9。
- 甲批：X 加 2、Y 减 2，随后查询 X（读数起初封存）；两次 UPDATE 盖章行数 1。
- 乙批：X、Y 分别翻倍；两次 UPDATE 盖章行数 1。
- 可能终局：甲乙 (6,14)、乙甲 (4,16)。
- 申请 P：X 的 5 件，值 8；申请 Q：X 或 Y 合取 16 件，值 6。

流程：
  1. 建起始快照；甲、乙各自拿快照副本离线执行，产出真实 committed 回执。
  2. 柜台从回执出具对账条（甲的读数先封存，另备公开版）。
  3. 回单室合账：封存版 → 两个可行终局；公开读数 3 → 只剩甲乙终局。
  4. 授信轮 1（无在途承诺）：只能签 Q；取出指定终局的逐单逐货位配给；
     下一位授信员重开账簿仍见 Q 占用，同一申请不能签两次。
  5. 授信轮 2（公开读数后）：P、Q 合要 21 件超过任意终局 20 件，只签 P。
  6. 授信轮 3（封存终局 + 已签 5 件灵活承诺）：P、Q 都不能再签。
  7. 矛盾演示：甲公开读数 3 与乙'公开读数 2 相互矛盾 → 整批隔离，授信拒绝。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "workdir" / "offline"

from sql_param import (  # noqa: E402
    BatchExecutor,
    CreditDesk,
    CreditError,
    PlanStore,
    capacities_from_report,
    export_slip,
    reconcile_round,
    verify_order,
)

SCHEMA = (
    "CREATE TABLE bins(id INTEGER PRIMARY KEY, name TEXT NOT NULL, qty INTEGER NOT NULL);"
)
TEMPLATES = {
    "add": "UPDATE bins SET qty = qty + :d WHERE id = :id",
    "sub": "UPDATE bins SET qty = qty - :d WHERE id = :id",
    "mul": "UPDATE bins SET qty = qty * :k WHERE id = :id",
    "read": "SELECT qty FROM bins WHERE id = :id ORDER BY id",
}
BINS = {"X": 1, "Y": 2}


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(label: str, payload) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def run_counter(name: str, batches: list[tuple[str, list[dict]]]) -> list[dict]:
    """一台离线柜台：独立计划库 + 快照副本，真实执行批次并产出回执。"""
    db_path = WORK / f"{name}.db"
    shutil.copy(WORK / "snapshot.db", db_path)
    store = PlanStore(WORK / f"store-{name}")
    for template_id, source in TEMPLATES.items():
        store.publish(template_id, 1, source)
    executor = BatchExecutor(store, db_path, WORK / f"receipts-{name}")
    return [executor.execute_batch(batch_id, items) for batch_id, items in batches]


def terminal_rows(report: dict) -> dict[str, tuple[int, int]]:
    """把终局行转成 {terminal_id: (X, Y)} 便于断言。"""
    out = {}
    for terminal in report["terminals"]:
        qty = {row[1]: row[2] for row in terminal["rows"]}
        out[terminal["terminal_id"]] = (qty["X"], qty["Y"])
    return out


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    # 1. 起始快照：X=1、Y=9。
    section("1. 起始快照与两台离线柜台各自真实执行")
    snapshot = WORK / "snapshot.db"
    with sqlite3.connect(snapshot) as conn:
        conn.execute(SCHEMA)
        conn.executemany("INSERT INTO bins VALUES(?,?,?)", [(1, "X", 1), (2, "Y", 9)])

    [receipt_a] = run_counter("jia", [("a-1", [
        {"template_id": "add", "revision": 1, "bindings": {"d": 2, "id": 1}},
        {"template_id": "sub", "revision": 1, "bindings": {"d": 2, "id": 2}},
        {"template_id": "read", "revision": 1, "bindings": {"id": 1}},
    ])])
    [receipt_b] = run_counter("yi", [("b-1", [
        {"template_id": "mul", "revision": 1, "bindings": {"k": 2, "id": 1}},
        {"template_id": "mul", "revision": 1, "bindings": {"k": 2, "id": 2}},
    ])])
    assert receipt_a["status"] == receipt_b["status"] == "committed"
    read_x = receipt_a["items"][2]["result"]["rows"][0][0]
    show("甲批 committed 回执（真实读到 X=3，先封存）", {
        "batch_id": receipt_a["batch_id"],
        "items": [{"template_id": i["template_id"], "result": i["result"]}
                  for i in receipt_a["items"]],
    })
    assert read_x == 3

    # 2. 出具对账条：甲两版（读数封存 / 公开），乙一版（只盖章行数）。
    section("2. 柜台出具对账条（sql-param-slip/v1）")
    slip_a_sealed = export_slip(
        receipt_a, counter_id="jia", sequence=1,
        visibility={0: {"rowcount": True}, 1: {"rowcount": True}},
    )
    slip_a_open = export_slip(
        receipt_a, counter_id="jia", sequence=1,
        visibility={0: {"rowcount": True}, 1: {"rowcount": True}, 2: {"columns": [0]}},
    )
    slip_b = export_slip(
        receipt_b, counter_id="yi", sequence=1,
        visibility={0: {"rowcount": True}, 1: {"rowcount": True}},
    )
    show("甲对账条（读数封存版）第 3 项", slip_a_sealed["items"][2]["result"])
    show("甲对账条（读数公开版）第 3 项", slip_a_open["items"][2]["result"])
    show("乙对账条第 1 项", slip_b["items"][0]["result"])

    # 3. 回单室合账：封存版两个终局；公开读数 3 后只剩甲乙。
    section("3. 回单室合账：封存读数 → 两个可行终局")
    counters_sealed = [
        {"counter_id": "jia", "slips": [slip_a_sealed]},
        {"counter_id": "yi", "slips": [slip_b]},
    ]
    report_sealed = reconcile_round(snapshot, table="bins", counters=counters_sealed)
    show("封存版合账报告（终局与见证顺序）", {
        "status": report_sealed["status"],
        "terminals": [
            {"terminal_id": t["terminal_id"], "rows": t["rows"],
             "witness_order": [o["batch_id"] for o in t["witness_order"]]}
            for t in report_sealed["terminals"]
        ],
        "batches": report_sealed["batches"],
    })
    assert report_sealed["status"] == "explained"
    assert sorted(terminal_rows(report_sealed).values()) == [(4, 16), (6, 14)]

    # 接收方独立复核一条见证顺序，拿到全部逐项核对证据。
    witness = report_sealed["terminals"][0]["witness_order"]
    verification = verify_order(
        snapshot, table="bins", counters=counters_sealed, order=witness
    )
    show("接收方独立复核见证顺序 t1（逐项证据）", verification["evidence"])
    assert verification["status"] == "explained"

    section("3b. 补公开甲真实读到的 3 → 只剩甲乙终局")
    counters_open = [
        {"counter_id": "jia", "slips": [slip_a_open]},
        {"counter_id": "yi", "slips": [slip_b]},
    ]
    report_open = reconcile_round(snapshot, table="bins", counters=counters_open)
    show("公开版合账报告", {
        "status": report_open["status"],
        "terminals": [
            {"terminal_id": t["terminal_id"], "rows": t["rows"],
             "witness_order": [o["batch_id"] for o in t["witness_order"]]}
            for t in report_open["terminals"]
        ],
    })
    assert list(terminal_rows(report_open).values()) == [(6, 14)]

    applications = [
        {"application_id": "P", "quantity": 5, "bins": ["X"], "value": 8},
        {"application_id": "Q", "quantity": 16, "bins": ["X", "Y"], "value": 6},
    ]

    # 4. 授信轮 1：封存终局、无在途承诺 → 只能签 Q。
    section("4. 授信轮 1（封存终局，无在途承诺）→ 只能签 Q")
    terminals_sealed = capacities_from_report(
        report_sealed, bins=BINS, quantity_column="qty"
    )
    desk1 = CreditDesk(WORK / "ledger-round1.json", terminals_sealed)
    evaluation1 = desk1.evaluate(applications)
    show("轮 1 评估（最佳集合与单验结果）", evaluation1)
    assert evaluation1["best"] == {"application_ids": ["Q"], "total_value": 6}
    assert evaluation1["solo_signable"] == {"P": False, "Q": True}
    show("P 不能加入的供量缺口见证", desk1.gap_witness(applications[0]))
    desk1.sign(applications)
    show("签发后账簿", desk1.signed())

    # 实际取一份配给：指定终局 t1（甲乙终局 (6,14)）。
    allocation = desk1.allocate("t1")
    show("终局 t1 的逐单逐货位配给", allocation)
    assert sum(allocation["orders"]["Q"].values()) == 16
    assert allocation["orders"]["Q"]["X"] <= 6 and allocation["orders"]["Q"]["Y"] <= 14

    # 下一位授信员重开同一账簿：旧承诺仍生效，同一申请不能签两次。
    desk1_next = CreditDesk(WORK / "ledger-round1.json", terminals_sealed)
    assert desk1_next.signed() == desk1.signed()
    show("下一位授信员重开账簿见到的在途承诺", desk1_next.signed())
    try:
        desk1_next.evaluate(applications)
        raise AssertionError("同一申请签两次未被拒绝")
    except CreditError as exc:
        show("同一申请不能签两次（预期失败）", str(exc))

    # 5. 授信轮 2：公开读数后只剩 (6,14)；P、Q 合要 21 件 → 只签 P。
    section("5. 授信轮 2（公开读数，只剩甲乙终局）→ 只签 P")
    desk2 = CreditDesk(
        WORK / "ledger-round2.json",
        capacities_from_report(report_open, bins=BINS, quantity_column="qty"),
    )
    evaluation2 = desk2.evaluate(applications)
    show("轮 2 评估", evaluation2)
    assert evaluation2["best"] == {"application_ids": ["P"], "total_value": 8}
    desk2.sign(applications)
    show("签 P 后 Q 的供量缺口见证（合要 21 件）", desk2.gap_witness(applications[1]))
    show("轮 2 终局 t1 配给", desk2.allocate("t1"))

    # 6. 授信轮 3：封存终局 + 已签 5 件灵活承诺 → 两张新单都不能签。
    section("6. 授信轮 3（封存终局 + 已签 5 件灵活承诺）→ 都不能签")
    desk3 = CreditDesk(WORK / "ledger-round3.json", terminals_sealed)
    desk3.register_commitment("flex-0", 5, ["X", "Y"])
    evaluation3 = desk3.evaluate(applications)
    show("轮 3 评估", evaluation3)
    assert evaluation3["best"] == {"application_ids": [], "total_value": 0}
    show("P 的供量缺口见证", desk3.gap_witness(applications[0]))
    show("Q 的供量缺口见证", desk3.gap_witness(applications[1]))

    # 7. 矛盾演示：甲公开读数 3（要求甲乙序）与乙'公开读数 2（要求乙甲序）→ 隔离。
    section("7. 相互矛盾的盖章值 → 整批隔离")
    [receipt_b2] = run_counter("yi2", [("b2-1", [
        {"template_id": "mul", "revision": 1, "bindings": {"k": 2, "id": 1}},
        {"template_id": "mul", "revision": 1, "bindings": {"k": 2, "id": 2}},
        {"template_id": "read", "revision": 1, "bindings": {"id": 1}},
    ])])
    slip_b2 = export_slip(
        receipt_b2, counter_id="yi", sequence=1,
        visibility={0: {"rowcount": True}, 1: {"rowcount": True}, 2: {"columns": [0]}},
    )
    show("乙'对账条公开读数（单独离线执行真诚读到 2）",
         slip_b2["items"][2]["result"]["public"])
    report_bad = reconcile_round(snapshot, table="bins", counters=[
        {"counter_id": "jia", "slips": [slip_a_open]},
        {"counter_id": "yi", "slips": [slip_b2]},
    ])
    show("矛盾轮合账报告", {
        "status": report_bad["status"],
        "reason": report_bad.get("reason"),
        "batches": report_bad["batches"],
        "search": report_bad["search"],
    })
    assert report_bad["status"] == "quarantined"
    assert all(b["status"] == "quarantined" for b in report_bad["batches"])
    try:
        capacities_from_report(report_bad, bins=BINS, quantity_column="qty")
        raise AssertionError("隔离回单不应进入授信")
    except CreditError as exc:
        show("回单被隔离，授信席拒绝建立（预期失败）", str(exc))

    print("\n全部端到端断言通过。")
    print(f"工作目录: {WORK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
