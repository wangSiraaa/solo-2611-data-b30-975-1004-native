import unittest

from cs import reservation


class OrderingTests(unittest.TestCase):
    EDGES = [("A", "R"), ("B", "R"), ("L", "A"), ("L", "B")]

    def project(self, tickets, previous=None, seq=1):
        return reservation.compute_projection(
            cut_id=f"Q{seq}",
            branch_id="B",
            managed_classes=["B"],
            edges=self.EDGES,
            tickets=tickets,
            previous=previous or {},
            entered_shift=f"S{seq:06d}",
            entered_seq=seq,
        )

    def test_enter_order_then_ticket_id(self):
        result = self.project({"T2": ["B"], "T1": ["B"]})
        self.assertEqual(result.eligible, ["T1", "T2"])

    def test_continuous_stay_keeps_shift(self):
        first = self.project({"X": ["L"], "Y": ["B"]}, seq=1)
        # Next cut: X drops L and takes B in the same committed batch.  It is
        # reachable on both sides, so its age is preserved.
        second = self.project(
            {"X": ["B"], "Y": ["B"]}, previous=first.stay_map(), seq=2
        )
        self.assertEqual(second.eligible, ["X", "Y"])
        self.assertEqual(second.stay_map()["X"][0], "S000001")

    def test_exit_then_return_reenters(self):
        first = self.project({"X": ["L"], "Y": ["B"]}, seq=1)
        # X truly leaves B (class A only) at cut 2.
        gone = self.project({"X": ["A"], "Y": ["B"]}, previous=first.stay_map(), seq=2)
        self.assertEqual(gone.eligible, ["Y"])
        # X returns at cut 3: it sorts after Y, which never left.
        back = self.project(
            {"X": ["L"], "Y": ["B"]}, previous=gone.stay_map(), seq=3
        )
        self.assertEqual(back.eligible, ["Y", "X"])
        self.assertEqual(back.stay_map()["X"][0], "S000003")

    def test_initial_stays_only_on_first_projection(self):
        first = self.project(
            {"X": ["L"], "Y": ["B"]},
            previous={},
            seq=1,
        )
        self.assertEqual(first.eligible, ["X", "Y"])
        # Using explicit initial stays (seeded historical order).
        seeded = reservation.compute_projection(
            cut_id="Q9",
            branch_id="B",
            managed_classes=["B"],
            edges=self.EDGES,
            tickets={"X": ["L"], "Y": ["B"]},
            previous={},
            entered_shift="S000009",
            entered_seq=9,
            initial_stays={"Y": ("S000001", 1), "X": ("S000002", 2)},
        )
        self.assertEqual(seeded.eligible, ["Y", "X"])

    def test_match_has_witness_path(self):
        result = self.project({"X": ["L"]}, seq=1)
        entry = result.candidates[0]
        self.assertEqual(entry.match.path[0], "L")
        self.assertEqual(entry.match.path[-1], "B")


if __name__ == "__main__":
    unittest.main()
