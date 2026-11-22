import unittest

from wire.manifest import (
    NONE_SIGNAL,
    ManifestError,
    Snapshot,
    TicketFact,
    build_roster,
    reachable_targets,
    signal_key,
    tally,
    verify_active_list,
)


def fact(ticket_id, classes, amount=0):
    return TicketFact(ticket_id, frozenset(classes), amount)


# Directory: cross leaf X reaches both targets A and B; aA only reaches A;
# bB only reaches B; orphan O is a known class that reaches neither target.
EDGES = frozenset(
    {
        ("aA", "A"),
        ("bB", "B"),
        ("X", "cross"),
        ("cross", "A"),
        ("cross", "B"),
        ("O", "lone"),
    }
)


class ReachabilityTests(unittest.TestCase):
    def test_specific_leaf_hits_one_target(self):
        hits, paths = reachable_targets(["aA"], ("A", "B"), EDGES)
        self.assertEqual(hits, frozenset({"A"}))
        self.assertEqual(paths["A"], ("aA", "A"))

    def test_cross_leaf_hits_both_targets_once_each(self):
        hits, paths = reachable_targets(["X", "aA"], ("A", "B"), EDGES)
        self.assertEqual(hits, frozenset({"A", "B"}))
        # Two labels and two parent paths still produce one witness per target.
        self.assertEqual(set(paths), {"A", "B"})
        self.assertTrue(paths["A"][0] in {"X", "aA"})

    def test_target_covers_itself(self):
        hits, _ = reachable_targets(["A"], ("A", "B"), EDGES)
        self.assertEqual(hits, frozenset({"A"}))

    def test_orphan_gets_no_target(self):
        hits, paths = reachable_targets(["O"], ("A", "B"), EDGES)
        self.assertEqual(hits, frozenset())
        self.assertEqual(paths, {})


class RosterTests(unittest.TestCase):
    def setUp(self):
        tickets = (
            fact("T01", ["aA"]),
            fact("T02", ["aA"], amount=0),       # zero amount still counts
            fact("T03", ["X"]),
            fact("T04", ["X", "bB"]),             # two labels, one signal
            fact("T05", ["bB"]),
            fact("T06", ["O"]),                   # none-signal
        )
        self.snapshot = Snapshot.of("cut-1", EDGES, tickets)
        self.roster = build_roster(self.snapshot, ("A", "B"))

    def test_signal_table_order_and_frequencies(self):
        self.assertEqual(self.roster.signals, ("A", "AB", "B", NONE_SIGNAL))
        self.assertEqual(tally(self.roster), {"A": 2, "AB": 2, "B": 1, NONE_SIGNAL: 1})
        self.assertEqual(self.roster.ordered_frequencies(), (2, 2, 1, 1))
        self.assertEqual(self.roster.ticket_signals["T04"], "AB")

    def test_zero_amount_ticket_still_counted(self):
        self.assertEqual(self.roster.frequencies["A"], 2)

    def test_complete_hit_set_is_one_signal(self):
        self.assertEqual(signal_key(["A", "B"]), "AB")
        self.assertEqual(signal_key([]), NONE_SIGNAL)

    def test_independent_station_signoff_checks_lists(self):
        ids = [t.ticket_id for t in self.roster.tickets]
        verify_active_list(self.roster, ids)  # exact list passes
        with self.assertRaises(ManifestError):
            verify_active_list(self.roster, ids[:-1])  # omitted ticket
        with self.assertRaises(ManifestError):
            verify_active_list(self.roster, ids + [ids[0]])  # duplicate
        with self.assertRaises(ManifestError):
            verify_active_list(self.roster, ids + ["GHOST"])

    def test_duplicate_ticket_rejected_on_cut(self):
        with self.assertRaises(ManifestError):
            Snapshot.of(
                "cut-bad",
                EDGES,
                [fact("T01", ["aA"]), fact("T01", ["bB"])],
            )

    def test_unknown_class_rejected(self):
        with self.assertRaises(ManifestError):
            build_roster(Snapshot.of("cut-2", EDGES, [fact("T9", ["ZZ"])]), ("A", "B"))

    def test_cycle_rejected(self):
        with self.assertRaises(ManifestError):
            Snapshot.of("cut-3", {("A", "B"), ("B", "A")}, [])

    def test_deterministic_roster_for_same_cut(self):
        again = build_roster(self.snapshot, ("A", "B"))
        self.assertEqual(again.signals, self.roster.signals)
        self.assertEqual(again.frequencies, self.roster.frequencies)


if __name__ == "__main__":
    unittest.main()
