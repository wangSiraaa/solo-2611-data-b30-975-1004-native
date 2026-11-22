#!/usr/bin/env python3
"""Replay the required handover scenarios against two independently restartable
local services.

The script is deterministic: it starts the old-hall service and the new-system
service as real HTTP servers on ephemeral ports, drives them over HTTP, and
exercises restart by stopping and restarting each service against the same
database file.  Every assertion in the task is checked and printed.

    python3 scenario.py

It also runs the small example twice: once with nothing yet issued (the
qualification rules) and once for the lost-reply handover.  Output is a list of
labelled checks so the result can be inspected without reading test internals.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from cs.legacy_server import serve as serve_legacy  # noqa: E402
from cs.new_server import serve as serve_new  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: Any = None) -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + ("" if detail is None else f"  -> {detail}"))
    if not condition:
        FAILURES.append(label)


# --------------------------------------------------------------------- client
class Service:
    def __init__(self, factory, db: str, port: int, **kwargs) -> None:
        self.factory = factory
        self.db = db
        self.port = port
        self.kwargs = kwargs
        self.server = None
        self.thread = None

    def start(self) -> "Service":
        self.server = self.factory(self.db, self.port, **self.kwargs)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
            if self.thread is not None:
                self.thread.join(timeout=5)
                self.thread = None

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))


def free_port() -> int:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def ensure_dirs(base: Path) -> None:
    base.mkdir(parents=True, exist_ok=True)


CATALOG = {
    "format": "classification-catalog/1",
    "revision": "G1",
    "nodes": [
        {"id": "R", "name": "全部"},
        {"id": "A", "name": "办件"},
        {"id": "B", "name": "办件"},
        {"id": "L", "name": "交叉子类"},
        {"id": "M", "name": "替代路径"},
    ],
    "edges": [["A", "R"], ["B", "R"], ["L", "A"], ["L", "B"]],
}

FACTS = [
    {"ticket_id": "X", "revision": 1, "amount": 0, "classes": ["L"]},
    {"ticket_id": "Y", "revision": 1, "amount": 0, "classes": ["B"]},
]


def bootstrap_new(service: Service) -> None:
    from cs.reservation_store import ReservationStore

    store = ReservationStore.open_reservation(service.db)
    store.init_catalog(CATALOG)
    store.add_branch("A", ["A"], name="A 分馆")
    store.add_branch("B", ["B"], name="B 分馆")
    store.add_window("new-w", "B", "NEW")
    store.add_window("a-w", "A", "A")
    store.seed(facts=FACTS, order=["X", "Y"], request_id="seed")
    store.close()


def new_cut(service: Service, request_id: str, bundle: dict, at: str | None = None) -> dict:
    body = {"request_id": request_id, "bundle": bundle}
    if at:
        body["at"] = at
    return service.post("/cut", body)


def queues(service: Service, branch: str) -> list[str]:
    return service.post("/queue", {"branch_id": branch})["eligible"]


# ------------------------------------------------------------------ scenarios
def scenario_qualification(base: Path) -> None:
    print("\n== 资格处：候办册与年龄规则 ==")
    db = str(base / "qual.sqlite")
    new = Service(serve_new, db, free_port()).start()
    try:
        bootstrap_new(new)
        check("初始 B 候办 X 先于 Y", queues(new, "B") == ["X", "Y"], queues(new, "B"))
        check("初始 A 只有 X", queues(new, "A") == ["X"], queues(new, "A"))

        # Same committed batch: replace X's path (L->M) and add M->B in one batch.
        new_cut(
            new,
            "q-swap",
            {
                "add_edges": [["M", "B"]],
                "remove_edges": [],
                "fact_revisions": [
                    {"ticket_id": "X", "revision": 2, "amount": 0, "classes": ["M"]}
                ],
            },
        )
        check(
            "同一批换路径仍可达：B 仍先给 X（续龄）",
            queues(new, "B") == ["X", "Y"],
            queues(new, "B"),
        )

        # X exits B in one committed cut, returns in a later cut.
        new_cut(
            new,
            "q-exit",
            {
                "add_edges": [],
                "remove_edges": [["M", "B"]],
                "fact_revisions": [
                    {"ticket_id": "X", "revision": 3, "amount": 0, "classes": ["M"]}
                ],
            },
        )
        check("真正退出一个已提交班次后 X 不在 B", queues(new, "B") == ["Y"], queues(new, "B"))
        new_cut(
            new,
            "q-back",
            {
                "add_edges": [["M", "B"]],
                "remove_edges": [],
                "fact_revisions": [
                    {"ticket_id": "X", "revision": 4, "amount": 0, "classes": ["M"]}
                ],
            },
        )
        check(
            "退出后回来从新班次排：B 先给 Y 再给 X",
            queues(new, "B") == ["Y", "X"],
            queues(new, "B"),
        )

        # Same shift stable ordering by ticket id with a third ticket Z.
        new_cut(
            new,
            "q-z",
            {
                "add_edges": [],
                "remove_edges": [],
                "fact_revisions": [
                    {"ticket_id": "Z0", "revision": 1, "amount": 0, "classes": ["B"]}
                ],
            },
        )
        check(
            "相同班次按稳定工单号排序",
            queues(new, "B") == ["Y", "X", "Z0"],
            queues(new, "B"),
        )

        # Grievance: missed registration for a ticket not derived by the graph.
        gate = new.post(
            "/gate",
            {
                "branch_id": "B",
                "kind": "insert",
                "ticket_id": "MISSED",
                "justification": "漏登申诉，补入当时班次",
                "request_id": "gate-missed",
                "entered_shift": "S000000",
                "entered_seq": 0,
                "basis": {"class_id": "B", "path": ["B"]},
            },
        )
        check("漏登申诉可独立入册并放到队首", queues(new, "B")[0] == "MISSED", queues(new, "B"))

        # Drop and restore the missed entry, keeping its basis.
        new.post(
            "/gate",
            {
                "branch_id": "B",
                "kind": "drop",
                "ticket_id": "MISSED",
                "justification": "材料待核，暂缓",
                "request_id": "gate-drop",
            },
        )
        check("drop 后不在候办册", "MISSED" not in queues(new, "B"), queues(new, "B"))
        restored = new.post(
            "/gate",
            {
                "branch_id": "B",
                "kind": "restore",
                "ticket_id": "MISSED",
                "justification": "材料齐全，恢复当时分类依据",
                "request_id": "gate-restore",
            },
        )
        check(
            "restore 恢复原归类依据",
            {k: restored["basis"][k] for k in ("class_id", "path")}
            == {"class_id": "B", "path": ["B"]},
            restored["basis"],
        )
        check("restore 后重新入册", "MISSED" in queues(new, "B"), queues(new, "B"))

        # Book from a committed cut, then re-open reads the frozen snapshot.
        book = new.post(
            "/book/open",
            {"branch_id": "B", "opened_by": "资格处", "request_id": "book-1"},
        )
        reopened = new.post(
            "/book/reopen",
            {"book_id": book["book_id"], "by": "资格处"},
        )
        check(
            "关门重开后的候办册与切面一致",
            reopened["entries"] == book["entries"],
            reopened["entries"],
        )
        check(
            "重开可查归类依据",
            all(e["path"] for e in reopened["classification"]),
            reopened["classification"],
        )
    finally:
        new.stop()


def scenario_handover(base: Path) -> None:
    print("\n== 一次营业中移交：旧窗口扣号回包丢失 ==")
    new_db = str(base / "handover_new.sqlite")
    old_db = str(base / "handover_old.sqlite")
    new_port = free_port()
    old_port = free_port()
    old = Service(serve_legacy, old_db, old_port).start()
    new = Service(serve_new, new_db, new_port, legacy_url=f"http://127.0.0.1:{old_port}").start()
    try:
        bootstrap_new(new)
        new.post("/day/open", {"branch_id": "B", "service_day": "D1", "capacity": 2})
        # A馆 has the old window; B馆 is the new branch taking over.
        old.post("/admin/open-day", {"hall_id": "A", "service_day": "D1", "capacity": 2})
        old.post(
            "/admin/person",
            {"request_id": "OLDREQ1", "ticket_id": "X"},
        )

        # A馆旧窗口 takes X's seat, but the reply is lost (we ignore it).
        old.post(
            "/reserve",
            {
                "request_id": "OLDREQ1",
                "window_id": "a-old-w1",
                "hall_id": "A",
                "service_day": "D1",
            },
        )
        # New hall immediately asks for a ticket.  X already holds one at the
        # old hall, so the branch must give its seat to Y.
        issued = new.post("/issue", {"request_id": "NEWREQ1", "window_id": "new-w", "service_day": "D1"})
        check("新分馆跳过已领的 X，把号给 Y", issued.get("ticket_id") == "Y", issued)
        check("新分馆出票成功", issued.get("status") == "issued", issued.get("status"))

        # Retransmit the old request: original ticket, no new seat.
        replay = old.post("/lookup", {"request_id": "OLDREQ1"})
        check(
            "旧窗口按原请求号重传取回 X 原票",
            replay.get("status") == "issued"
            and replay.get("ticket_id") == "X"
            and replay.get("ticket_no") == "1",
            replay,
        )
        old_day = old.post("/admin/day", {"hall_id": "A", "service_day": "D1"})
        check("旧窗口没有重复扣号", old_day["issued"] == 1, old_day)
        person = new.post("/request", {"request_id": "NEWREQ1"})
        check("新窗口可查实际出的票", person.get("ticket_no") == "1", person)

        summary = new.post("/window/summary", {"window_id": "new-w", "service_day": "D1"})
        check(
            "可查窗口实际出票与余号",
            summary["issued"] == 1 and summary["remaining"] == 1,
            {"issued": summary["issued"], "remaining": summary["remaining"]},
        )

        # Both services restart; ledger survives and quotas continue.
        new.stop()
        old.stop()
        old = Service(serve_legacy, old_db, old_port).start()
        new = Service(serve_new, new_db, new_port, legacy_url=f"http://127.0.0.1:{old_port}").start()
        restarted = new.post("/window/summary", {"window_id": "new-w", "service_day": "D1"})
        check(
            "重启后窗口出票与余号保持",
            restarted["issued"] == 1 and restarted["remaining"] == 1,
            {"issued": restarted["issued"], "remaining": restarted["remaining"]},
        )
        replayed_after_restart = old.post("/lookup", {"request_id": "OLDREQ1"})
        check(
            "重启后旧请求仍取回原票",
            replayed_after_restart.get("ticket_id") == "X"
            and replayed_after_restart.get("ticket_no") == "1",
            replayed_after_restart,
        )

        # A third person applies: falls back from the queue head; but X,Y both
        # have seats, so nobody is left.
        third = new.post("/issue", {"request_id": "NEWREQ2", "window_id": "new-w", "service_day": "D1"})
        check("其余可办人员用尽后明确不再出号", third.get("status") == "exhausted", third)

        # Cross-day retransmit: the same old request id a day later recovers the
        # original receipt and does not touch the new day's quota.
        old.post("/admin/open-day", {"hall_id": "A", "service_day": "D2", "capacity": 1})
        next_day = old.post(
            "/reserve",
            {
                "request_id": "OLDREQ1",
                "window_id": "a-old-w1",
                "hall_id": "A",
                "service_day": "D2",
            },
        )
        check(
            "隔天重传仍取回原票，不占新一天号",
            next_day.get("ticket_no") == "1"
            and next_day.get("service_day") == "D1"
            and old.post("/admin/day", {"hall_id": "A", "service_day": "D2"})["issued"] == 0,
            next_day,
        )
    finally:
        new.stop()
        old.stop()


def scenario_quota_burn(base: Path) -> None:
    print("\n== 保持最初资格，B 唯一号当天已兑现 ==")
    db = str(base / "quota.sqlite")
    new = Service(serve_new, db, free_port()).start()
    try:
        bootstrap_new(new)
        new.post("/day/open", {"branch_id": "B", "service_day": "D1", "capacity": 1})
        new.post(
            "/redeem",
            {
                "branch_id": "B",
                "service_day": "D1",
                "ticket_id": "Y",
                "window_id": "new-w",
            },
        )
        status = new.post("/day", {"branch_id": "B", "service_day": "D1"})
        check("已兑现后 B 无余号", status["remaining"] == 0, status)
        issued = new.post("/issue", {"request_id": "Q1", "window_id": "new-w", "service_day": "D1"})
        check("B 不能再给任何人出号", issued.get("status") == "exhausted", issued)
        tickets = new.post("/tickets", {"branch_id": "B", "service_day": "D1"})["tickets"]
        check(
            "号源不换人：已经兑现给 Y",
            len(tickets) == 1 and tickets[0]["ticket_id"] == "Y",
            tickets,
        )
    finally:
        new.stop()


def scenario_cross_hall_one_person(base: Path) -> None:
    print("\n== 同一人跨馆当天只能领一张，改类不换票 ==")
    new_db = str(base / "cross_new.sqlite")
    old_db = str(base / "cross_old.sqlite")
    old_port = free_port()
    old = Service(serve_legacy, old_db, old_port).start()
    new = Service(
        serve_new, new_db, free_port(), legacy_url=f"http://127.0.0.1:{old_port}"
    ).start()
    try:
        bootstrap_new(new)
        new.post("/day/open", {"branch_id": "B", "service_day": "D2", "capacity": 2})
        new.post("/day/open", {"branch_id": "A", "service_day": "D2", "capacity": 2})
        first = new.post(
            "/issue", {"request_id": "C1", "window_id": "new-w", "service_day": "D2"}
        )
        check("B 先按候办顺序给 X", first.get("ticket_id") == "X", first)
        # Change X's class after issue; the ticket stays.
        new_cut(
            new,
            "cross-cut",
            {
                "add_edges": [],
                "remove_edges": [],
                "fact_revisions": [
                    {"ticket_id": "X", "revision": 2, "amount": 0, "classes": ["B"]}
                ],
            },
        )
        tickets = new.post("/tickets", {"branch_id": "B", "service_day": "D2"})["tickets"]
        check(
            "已领后即使改类也保留原凭条",
            len(tickets) == 1 and tickets[0]["ticket_id"] == "X",
            tickets,
        )
        second = new.post(
            "/issue", {"request_id": "C2", "window_id": "a-w", "service_day": "D2"}
        )
        check("另一馆不能给已领的 X 再出票", second.get("ticket_id") != "X", second)
        # Old hall tries the same person: must be refused a second seat.
        old.post("/admin/open-day", {"hall_id": "OLD", "service_day": "D2", "capacity": 2})
        old.post("/admin/person", {"request_id": "OLDX", "ticket_id": "X"})
        denied = old.post(
            "/reserve",
            {
                "request_id": "OLDX",
                "window_id": "old-w1",
                "hall_id": "OLD",
                "service_day": "D2",
            },
        )
        check(
            "旧馆知道 X 已在别处领票，不再出号",
            denied.get("status") == "awaiting",
            denied,
        )
    finally:
        new.stop()
        old.stop()


def scenario_multi_branch_handover(base: Path) -> None:
    print("\n== 迁移按分馆营业：一馆短暂关闭，其他馆照办 ==")
    db = str(base / "multi.sqlite")
    new = Service(serve_new, db, free_port()).start()
    try:
        bootstrap_new(new)
        # W reaches both A and B through L, so A has someone to serve too.
        new_cut(
            new,
            "multi-add-w",
            {
                "add_edges": [],
                "remove_edges": [],
                "fact_revisions": [
                    {"ticket_id": "W", "revision": 1, "amount": 0, "classes": ["L"]}
                ],
            },
        )
        new.post("/day/open", {"branch_id": "B", "service_day": "D3", "capacity": 2})
        new.post("/day/open", {"branch_id": "A", "service_day": "D3", "capacity": 2})
        # Simulate B closed for handover by using a fresh store marker.
        from cs.reservation_store import ReservationStore

        store = ReservationStore.open_reservation(db)
        store.set_branch_open("B", False)
        store.close()
        b_issue = new.post("/issue", {"request_id": "M1", "window_id": "new-w", "service_day": "D3"})
        check("关闭交接中的 B 时 B 窗口被拒", b_issue.get("error") is not None or b_issue.get("status") == "exhausted", b_issue)
        a_issue = new.post("/issue", {"request_id": "M2", "window_id": "a-w", "service_day": "D3"})
        check("其余已营业的 A 馆仍能办理", a_issue.get("status") == "issued", a_issue)
        store = ReservationStore.open_reservation(db)
        store.set_branch_open("B", True)
        store.close()
        b_after = new.post("/issue", {"request_id": "M3", "window_id": "new-w", "service_day": "D3"})
        check("B 重开后继续办理", b_after.get("status") == "issued", b_after)
    finally:
        new.stop()


def scenario_shared_workorders(base: Path) -> None:
    print("\n== 大分馆候办高度重叠：不重复出票 ==")
    db = str(base / "overlap.sqlite")
    new = Service(serve_new, db, free_port()).start()
    try:
        bootstrap_new(new)
        # W reaches both A and B too: the two big branches' queues overlap.
        new_cut(
            new,
            "overlap-add-w",
            {
                "add_edges": [],
                "remove_edges": [],
                "fact_revisions": [
                    {"ticket_id": "W", "revision": 1, "amount": 0, "classes": ["L"]}
                ],
            },
        )
        new.post("/day/open", {"branch_id": "A", "service_day": "D4", "capacity": 5})
        new.post("/day/open", {"branch_id": "B", "service_day": "D4", "capacity": 5})
        # Both X and Y reach both A and B (Y has B; X has L which reaches A and B).
        first_b = new.post("/issue", {"request_id": "O1", "window_id": "new-w", "service_day": "D4"})
        second_a = new.post("/issue", {"request_id": "O2", "window_id": "a-w", "service_day": "D4"})
        check("B 先领 X", first_b.get("ticket_id") == "X", first_b)
        check(
            "重叠候办时 A 不会给 X 重出，改给下一位",
            second_a.get("ticket_id") == "W",
            second_a,
        )
        count = len(new.post("/tickets", {"branch_id": "A", "service_day": "D4"})["tickets"]) + len(
            new.post("/tickets", {"branch_id": "B", "service_day": "D4"})["tickets"]
        )
        check("跨两馆合计只出两张实名票", count == 2, count)
        all_a = [t["ticket_id"] for t in new.post("/tickets", {"branch_id": "A", "service_day": "D4"})["tickets"]]
        all_b = [t["ticket_id"] for t in new.post("/tickets", {"branch_id": "B", "service_day": "D4"})["tickets"]]
        check("同一人不同时出现在两馆", not (set(all_a) & set(all_b)), {"A": all_a, "B": all_b})
    finally:
        new.stop()


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="handover-"))
    print(f"workdir: {base}")
    try:
        scenario_qualification(base)
        scenario_handover(base)
        scenario_quota_burn(base)
        scenario_cross_hall_one_person(base)
        scenario_multi_branch_handover(base)
        scenario_shared_workorders(base)
    finally:
        # keep the directory for inspection; print its path
        pass
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("ALL SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
