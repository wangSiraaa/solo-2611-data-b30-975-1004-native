import json
import tempfile
import unittest
from pathlib import Path

from wire.batch import (
    BatchError,
    WireBatch,
    archive_batch,
    load_archive,
    make_batch,
    replay_batch,
    replay_with_tickets,
)
from wire.codebook import UnknownSignal, build_code_plan
from wire.manifest import NONE_SIGNAL, Snapshot, TicketFact, build_roster


def fact(ticket_id, classes, amount=0):
    return TicketFact(ticket_id, frozenset(classes), amount)


EDGES = frozenset(
    {("aA", "A"), ("bB", "B"), ("X", "A"), ("X", "B"), ("ORPHAN", "LONE")}
)


class BatchTests(unittest.TestCase):
    def setUp(self):
        tickets = (
            fact("A1", ["aA"]),
            fact("A2", ["aA"]),
            fact("A3", ["aA"]),
            fact("A4", ["aA"]),
            fact("A5", ["aA"]),
            fact("X1", ["X"]),
            fact("X2", ["X"]),
            fact("B1", ["bB"]),
            fact("B2", ["bB"]),
            fact("B3", ["bB"]),
        )
        self.snapshot = Snapshot.of("cut-day-1", EDGES, tickets)
        self.roster = build_roster(self.snapshot, ("A", "B"))
        self.assertEqual(self.roster.ordered_frequencies(), (5, 2, 3))
        self.plan = build_code_plan(
            self.roster.signals, self.roster.ordered_frequencies(), max_length=2
        )

    def test_batch_roundtrip_bit_for_bit(self):
        batch = make_batch("batch-1", self.roster, self.plan)
        self.assertEqual(batch.bitstream, "0" * 5 + "10" * 2 + "11" * 3)
        self.assertEqual(batch.total_bits, 15)
        signals = replay_batch(batch)
        self.assertEqual(signals, [self.roster.ticket_signals[t] for t in batch.ticket_order])
        rows = replay_with_tickets(batch)
        self.assertEqual([row[1] for row in rows], signals)
        self.assertEqual([row[2] for row in rows], list(batch.ticket_order))

    def test_plan_roster_mismatch_rejected(self):
        other = build_code_plan(("A", "AB", "B"), (2, 2, 6), max_length=2)
        with self.assertRaises(BatchError):
            make_batch("batch-x", self.roster, other)

    def test_ticket_order_must_be_permutation(self):
        with self.assertRaises(BatchError):
            make_batch(
                "batch-2",
                self.roster,
                self.plan,
                ticket_order=[t.ticket_id for t in self.roster.tickets[:-1]],
            )

    def test_out_of_table_signal_bounces_before_sealing(self):
        # A roster carrying a signal absent from the plan cannot be encoded:
        # simulate by tampering through the encoder boundary.
        from wire.codebook import encode

        with self.assertRaises(UnknownSignal):
            encode(self.plan, ["A", "SURPRISE"])

    def test_archive_is_append_only_and_replays_after_reload(self):
        first = make_batch("batch-1", self.roster, self.plan)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wire.jsonl"
            archive_batch(first, path)
            loaded = load_archive(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(replay_with_tickets(loaded[0]), replay_with_tickets(first))

            # A later machine configuration archives alongside, never over.
            roster2 = build_roster(
                Snapshot.of(
                    "cut-day-2",
                    EDGES,
                    (
                        fact("A1", ["bB"]),
                        fact("A2", ["aA"]),
                        fact("X1", ["X"]),
                    ),
                ),
                ("A", "B"),
            )
            plan2 = build_code_plan(
                roster2.signals, roster2.ordered_frequencies(), max_length=2,
                burned={"A": "00"}, machine_id="burn-A-00",
            )
            second = make_batch("batch-2", roster2, plan2)
            archive_batch(second, path)
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([row["batch_id"] for row in records], ["batch-1", "batch-2"])
            reloaded = load_archive(path)
            # Old batch still decodes under its own archived table.
            self.assertEqual(replay_batch(reloaded[0]), replay_batch(first))
            self.assertEqual(replay_batch(reloaded[1]), replay_batch(second))
            self.assertNotEqual(reloaded[0].codewords, reloaded[1].codewords)

    def test_record_roundtrip_contains_full_code_table(self):
        batch = make_batch("batch-1", self.roster, self.plan)
        rebuilt = WireBatch.from_record(batch.to_record())
        self.assertEqual(rebuilt.bitstream, batch.bitstream)
        self.assertEqual(rebuilt.codewords, batch.codewords)
        self.assertEqual(replay_with_tickets(rebuilt), replay_with_tickets(batch))

    def test_none_signal_batch(self):
        # ORPHAN is a known-but-unconnected class; it yields the none-signal.
        roster = build_roster(
            Snapshot.of(
                "cut-none",
                EDGES,
                (fact("T1", ["aA"]), fact("T2", ["ORPHAN"])),
            ),
            ("A", "B"),
        )
        self.assertEqual(roster.signals, ("A", NONE_SIGNAL))
        plan = build_code_plan(roster.signals, roster.ordered_frequencies(), max_length=2)
        batch = make_batch("batch-none", roster, plan)
        self.assertEqual(replay_batch(batch), ["A", NONE_SIGNAL])


if __name__ == "__main__":
    unittest.main()
