import os
import tempfile
import threading
import unittest

from cs.handover import Handover
from cs.legacy import LegacyHall
from cs.reservation_store import ReservationError, ReservationStore

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


def fresh(testcase, branches=(("A", ["A"]), ("B", ["B"]))):
    path = tempfile.mktemp(suffix=".db")
    testcase.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
    store = ReservationStore.open_reservation(path)
    store.init_catalog(CATALOG)
    for branch_id, classes in branches:
        store.add_branch(branch_id, classes)
    store.seed(facts=FACTS, order=["X", "Y"])
    testcase.addCleanup(store.close)
    return store


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.store = fresh(self)

    def test_seed_order(self):
        self.assertEqual(self.store.eligible("B"), ["X", "Y"])
        self.assertEqual(self.store.eligible("A"), ["X"])

    def test_same_batch_path_swap_keeps_age(self):
        self.store.submit_qualification(
            {
                "add_edges": [["M", "B"]],
                "remove_edges": [],
                "fact_revisions": [
                    {"ticket_id": "X", "revision": 2, "amount": 0, "classes": ["M"]}
                ],
            },
            request_id="swap",
        )
        self.assertEqual(self.store.eligible("B"), ["X", "Y"])

    def test_real_exit_then_return(self):
        self.store.submit_qualification(
            {
                "add_edges": [],
                "remove_edges": [["M", "B"]],
                "fact_revisions": [
                    {"ticket_id": "X", "revision": 2, "amount": 0, "classes": ["A"]}
                ],
            },
            request_id="exit",
        )
        self.assertEqual(self.store.eligible("B"), ["Y"])
        self.store.submit_qualification(
            {
                "add_edges": [],
                "remove_edges": [],
                "fact_revisions": [
                    {"ticket_id": "X", "revision": 3, "amount": 0, "classes": ["L"]}
                ],
            },
            request_id="return",
        )
        self.assertEqual(self.store.eligible("B"), ["Y", "X"])

    def test_replayed_cut_is_idempotent(self):
        bundle = {
            "add_edges": [],
            "remove_edges": [],
            "fact_revisions": [
                {"ticket_id": "Z", "revision": 1, "amount": 0, "classes": ["B"]}
            ],
        }
        first = self.store.submit_qualification(bundle, request_id="cut-1")
        second = self.store.submit_qualification(bundle, request_id="cut-1")
        self.assertEqual(first["cut_id"], second["cut_id"])
        self.assertEqual(len(self.store.list_cuts()), 2)  # seed + one cut

    def test_noop_cut_does_not_advance(self):
        before = self.store.eligible("B")
        result = self.store.submit_qualification(
            {"add_edges": [], "remove_edges": [], "fact_revisions": []},
            request_id="noop",
        )
        self.assertTrue(result.get("noop"))
        self.assertEqual(self.store.eligible("B"), before)

    def test_gate_insert_front_restore_drop(self):
        self.store.apply_gate(
            branch_id="B",
            kind="insert",
            ticket_id="MISS",
            justification="漏登",
            request_id="g1",
            entered_shift="S000000",
            entered_seq=0,
            basis={"class_id": "B", "path": ["B"]},
        )
        self.assertEqual(self.store.eligible("B")[0], "MISS")
        self.store.apply_gate(
            branch_id="B",
            kind="drop",
            ticket_id="MISS",
            justification="待核",
            request_id="g2",
        )
        self.assertNotIn("MISS", self.store.eligible("B"))
        restored = self.store.apply_gate(
            branch_id="B",
            kind="restore",
            ticket_id="MISS",
            justification="恢复",
            request_id="g3",
        )
        self.assertEqual(restored["basis"]["path"], ["B"])
        self.assertIn("MISS", self.store.eligible("B"))

    def test_book_frozen_across_catalog_change(self):
        book = self.store.open_book(
            branch_id="B", opened_by="qo", request_id="bk"
        )
        frozen_entries = book["entries"]
        self.store.submit_qualification(
            {
                "add_edges": [],
                "remove_edges": [],
                "fact_revisions": [
                    {"ticket_id": "X", "revision": 2, "amount": 0, "classes": ["A"]}
                ],
            },
            request_id="move-away",
        )
        reopened = self.store.reopen_book(book["book_id"], by="qo")
        self.assertEqual(reopened["entries"], frozen_entries)
        self.assertEqual(reopened["cut"], book["frozen"]["cut"])

    def test_book_snapshot_is_at_its_cut(self):
        book = self.store.open_book(branch_id="B", opened_by="qo", request_id="bk2")
        snap = self.store.snapshot("B", book["area_seq"])
        self.assertEqual(snap["eligible"], book["entries"])


class IssuanceTests(unittest.TestCase):
    def setUp(self):
        self.store = fresh(self)
        self.store.add_window("b1", "B", "NEW")
        self.store.add_window("a1", "A", "A")
        self.store.open_day("B", "D1", 2)
        self.store.open_day("A", "D1", 2)

    def test_issue_in_queue_order(self):
        first = self.store.issue(request_id="r1", window_id="b1", service_day="D1")
        self.assertEqual((first["status"], first["ticket_id"]), ("issued", "X"))
        second = self.store.issue(request_id="r2", window_id="b1", service_day="D1")
        self.assertEqual(second["ticket_id"], "Y")
        self.assertEqual((second["ticket_no"]), "2")

    def test_one_person_one_ticket_per_day_across_halls(self):
        self.store.issue(request_id="r1", window_id="b1", service_day="D1")
        # X now holds a ticket; the other branch must skip X.
        other = self.store.issue(request_id="r2", window_id="a1", service_day="D1")
        self.assertNotEqual(other.get("ticket_id"), "X")
        held = self.store.has_ticket("X", "D1")
        self.assertIsNotNone(held)

    def test_retry_request_returns_same_ticket(self):
        first = self.store.issue(request_id="r1", window_id="b1", service_day="D1")
        again = self.store.issue(request_id="r1", window_id="b1", service_day="D1")
        self.assertEqual(first, again)

    def test_class_change_after_issue_keeps_ticket(self):
        self.store.issue(request_id="r1", window_id="b1", service_day="D1")
        self.store.submit_qualification(
            {
                "add_edges": [],
                "remove_edges": [],
                "fact_revisions": [
                    {"ticket_id": "X", "revision": 2, "amount": 0, "classes": ["A"]}
                ],
            },
            request_id="reclass",
        )
        self.assertEqual(self.store.has_ticket("X", "D1")["ticket_no"], "1")

    def test_closed_branch_refuses(self):
        self.store.set_branch_open("B", False)
        result = self.store.issue(request_id="r1", window_id="b1", service_day="D1")
        self.assertEqual(result["status"], "closed")

    def test_redeemed_seat_blocks_others(self):
        self.store.open_day("B", "D2", 1)
        self.store.redeem_seat(
            branch_id="B", service_day="D2", ticket_id="Y", window_id="b1"
        )
        result = self.store.issue(request_id="r9", window_id="b1", service_day="D2")
        self.assertEqual(result["status"], "exhausted")
        with self.assertRaises(ReservationError):
            self.store.redeem_seat(
                branch_id="B", service_day="D2", ticket_id="X", window_id="b1"
            )

    def test_concurrent_windows_one_person_one_seat(self):
        # Exactly one person is waiting.  Two windows race; only one seat may be
        # handed out, and the loser finds nobody else at the head.
        path = tempfile.mktemp(suffix=".db")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        store = ReservationStore.open_reservation(path)
        self.addCleanup(store.close)
        store.init_catalog(CATALOG)
        store.add_branch("B", ["B"])
        store.add_window("w1", "B", "N")
        store.add_window("w2", "B", "N")
        store.seed(
            facts=[{"ticket_id": "X", "revision": 1, "amount": 0, "classes": ["B"]}],
            order=["X"],
        )
        store.open_day("B", "D1", 5)
        results = []
        barrier = threading.Barrier(2)

        def worker(window):
            other = ReservationStore.open_reservation(path)
            try:
                barrier.wait()
                results.append(
                    other.issue(
                        request_id="race-" + window,
                        window_id=window,
                        service_day="D1",
                    )
                )
            finally:
                other.close()

        threads = [threading.Thread(target=worker, args=(w,)) for w in ("w1", "w2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        issued = [r for r in results if r.get("status") == "issued"]
        self.assertEqual(len(issued), 1)
        self.assertEqual(issued[0]["ticket_id"], "X")
        self.assertEqual(store.day_status("B", "D1")["issued"], 1)

    def test_concurrent_windows_issue_once_per_person(self):
        store = self.store
        results = []

        def worker(window):
            other = ReservationStore.open_reservation(
                store.conn.execute("PRAGMA database_list").fetchone()[2]
            )
            try:
                results.append(
                    other.issue(
                        request_id="race-" + window,
                        window_id=window,
                        service_day="D1",
                    )
                )
            finally:
                other.close()

        threads = [
            threading.Thread(target=worker, args=("b1",)),
            threading.Thread(target=worker, args=("a1",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        tickets = store.tickets_for("B", "D1") + store.tickets_for("A", "D1")
        persons = [t["ticket_id"] for t in tickets]
        self.assertEqual(len(persons), len(set(persons)))
        issued = [r for r in results if r.get("status") == "issued"]
        self.assertTrue(issued)


class HandoverTests(unittest.TestCase):
    def setUp(self):
        self.store = fresh(self)
        self.store.add_window("b1", "B", "NEW")
        self.store.open_day("B", "D1", 2)
        old_path = tempfile.mktemp(suffix=".db")
        self.addCleanup(lambda: os.path.exists(old_path) and os.unlink(old_path))
        self.old = LegacyHall.open(old_path)
        self.addCleanup(self.old.close)
        self.old.open_day("OLD", "D1", 2)
        self.handover = Handover(self.store, self.old)

    def test_lost_reply_recovery(self):
        # Old window takes X's seat; the reply is "lost".
        lost = self.old.reserve(
            request_id="OLD1",
            window_id="old-w",
            hall_id="OLD",
            service_day="D1",
            ticket_id="X",
        )
        self.assertEqual(lost["status"], "issued")
        # New hall asks; X holds one so it must give Y.
        issued = self.handover.issue_at_new_hall(
            request_id="NEW1", window_id="b1", service_day="D1"
        )
        self.assertEqual(issued["ticket_id"], "Y")
        # Old retry returns the original ticket, not a second seat.
        replay = self.old.reserve(
            request_id="OLD1",
            window_id="old-w",
            hall_id="OLD",
            service_day="D1",
            ticket_id="X",
        )
        self.assertEqual(replay["ticket_no"], lost["ticket_no"])
        self.assertEqual(self.old.day_status("OLD", "D1")["issued"], 1)

    def test_pending_request_reported_in_progress(self):
        # Simulate a request that reserved but has not issued: craft a pending
        # row directly through the durable phase-1 marker.
        self.old.conn.execute(
            "INSERT INTO lh_requests(request_id, window_id, hall_id, service_day, "
            "ticket_id, status, created_at) VALUES('P1','old-w','OLD','D1','X','pending','now')"
        )
        located = self.old.locate_person("X", "D1")
        self.assertEqual(located["status"], "in_progress")

    def test_lookup_never_consumes_quota(self):
        before = self.old.day_status("OLD", "D1")["issued"]
        for _ in range(3):
            self.old.lookup("missing-request")
        self.assertEqual(self.old.day_status("OLD", "D1")["issued"], before)

    def test_next_day_retransmit_does_not_spend_new_day(self):
        self.old.open_day("OLD", "D2", 1)
        issued = self.old.reserve(
            request_id="OLD1",
            window_id="old-w",
            hall_id="OLD",
            service_day="D1",
            ticket_id="X",
        )
        # Same request id retransmitted a day later: recover the same receipt,
        # and leave the new day's quota untouched.
        replay = self.old.reserve(
            request_id="OLD1",
            window_id="old-w",
            hall_id="OLD",
            service_day="D2",
            ticket_id="X",
        )
        self.assertEqual(replay["ticket_no"], issued["ticket_no"])
        self.assertEqual(replay["service_day"], "D1")
        self.assertEqual(self.old.day_status("OLD", "D2")["issued"], 0)

    def test_unsuccessful_request_is_explicit(self):
        self.old.open_day("OLD", "D3", 1)
        first = self.old.reserve(
            request_id="A1",
            window_id="old-w",
            hall_id="OLD",
            service_day="D3",
            ticket_id="X",
        )
        self.assertEqual(first["status"], "issued")
        denied = self.old.reserve(
            request_id="A2",
            window_id="old-w",
            hall_id="OLD",
            service_day="D3",
            ticket_id="Y",
        )
        self.assertEqual(denied["status"], "awaiting")
        self.assertNotIn("ticket_no", denied)
        self.assertIn("error", denied)
        # Querying by the denied request id returns the explicit non-issued
        # state, never a fabricated ticket.
        self.assertEqual(self.old.lookup("A2")["status"], "awaiting")
        self.assertEqual(self.old.lookup("never-sent")["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
