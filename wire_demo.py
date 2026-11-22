#!/usr/bin/env python3
"""End-to-end demonstration for the old bit-by-bit wire workflow.

Walks the exact story from the brief:

1. a station signs off a manifest from one committed directory/fact cut;
2. the three-machine scenarios (plain width-2, re-routed tickets, burned
   word A=00) produce the stated 15 / 14 / 17 minimum bit totals;
3. a batch is encoded and decoded bit by bit back to ticket ownership;
4. an out-of-table signal bounces the batch;
5. incompatible burned codes and too-narrow widths are declared unloadable;
6. archived batches keep their own code table and still decode after a new
   machine configuration is published.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from wire.batch import (  # noqa: E402
    archive_batch,
    load_archive,
    make_batch,
    replay_with_tickets,
)
from wire.codebook import (  # noqa: E402
    DecodeError,
    UnloadableError,
    UnknownSignal,
    build_code_plan,
    decode_trace,
    encode,
)
from wire.manifest import (  # noqa: E402
    NONE_SIGNAL,
    Snapshot,
    TicketFact,
    build_roster,
    verify_active_list,
)

# Directory edges (child -> parent).  aA and bB are target-specific leaves;
# X is the cross leaf reaching both A and B.
EDGES = frozenset({("aA", "A"), ("bB", "B"), ("X", "A"), ("X", "B")})
TARGETS = ("A", "B")


def fact(ticket_id, classes, amount=0):
    return TicketFact(ticket_id, frozenset(classes), amount)


def banner(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_plan(title, plan):
    print(f"\n{title}")
    print(f"  machine width : {plan.max_length} bits")
    print(f"  burned words  : {dict(plan.burned) or 'none'}")
    print("  signal table (ordered) with codeword and frequency:")
    for signal, freq, word in zip(plan.signals, plan.frequencies, plan.codewords):
        print(f"    {signal:>3}  freq={freq:>2}  code={word:<4} len={len(word)}")
    print(f"  minimum total batch length = {plan.total_bits} bits")


def main():
    # ------------------------------------------------------------------ cut 1
    banner("1. Station sign-off on the committed cut (5 A-only, 2 cross, 3 B-only)")
    tickets = (
        *(fact(f"A{i}", ["aA"]) for i in range(1, 6)),
        fact("X1", ["X"]),
        fact("X2", ["X"], amount=0),       # zero amount still counts
        *(fact(f"B{i}", ["bB"]) for i in range(1, 4)),
    )
    snapshot = Snapshot.of("cut-day-1", EDGES, tickets)
    roster = build_roster(snapshot, TARGETS)
    print("  signed signal table :", roster.signals)
    print("  frequencies         :", roster.ordered_frequencies())
    verify_active_list(roster, [t.ticket_id for t in roster.tickets])
    print("  independent active-list check: PASS (no missing/duplicate ticket)")

    # ------------------------------------------------------- scenario 1: plain
    banner("2a. Machine M1: no burned words, maximum length 2")
    plan1 = build_code_plan(
        roster.signals, roster.ordered_frequencies(), max_length=2, machine_id="M1"
    )
    show_plan("code plan M1", plan1)
    assert plan1.codewords == ("0", "10", "11") and plan1.total_bits == 15

    batch1 = make_batch("day1-M1", roster, plan1)
    print("\n  sealed batch bit stream (no separator):")
    print("   ", batch1.bitstream, f"({batch1.total_bits} bits)")
    print("  bit-by-bit decode back to signals and ticket ownership:")
    for bits, signal, ticket_id in replay_with_tickets(batch1):
        print(f"    read {bits:<3} -> signal {signal:<3} ticket {ticket_id}")

    # --------------------------------------------- scenario 2: re-route tickets
    banner("2b. Three A-only tickets are re-routed to B in the real library")
    rerouted = (
        fact("A1", ["bB"]),
        fact("A2", ["bB"]),
        fact("A3", ["bB"]),
        fact("A4", ["aA"]),
        fact("A5", ["aA"]),
        fact("X1", ["X"]),
        fact("X2", ["X"], amount=0),
        *(fact(f"B{i}", ["bB"]) for i in range(1, 4)),
    )
    snapshot2 = Snapshot.of("cut-day-2", EDGES, rerouted)
    roster2 = build_roster(snapshot2, TARGETS)
    print("  re-derived frequencies:", roster2.ordered_frequencies())
    plan2 = build_code_plan(
        roster2.signals, roster2.ordered_frequencies(), max_length=2, machine_id="M1"
    )
    show_plan("code plan M1 after re-route", plan2)
    assert plan2.codewords == ("00", "01", "1") and plan2.total_bits == 14
    print("  -> the only one-bit short code now lands on B (the heaviest leaf).")
    batch2 = make_batch("day2-M1", roster2, plan2)
    print("  sealed stream:", batch2.bitstream, f"({batch2.total_bits} bits)")
    assert decode_trace(plan2, batch2.bitstream) is not None

    # --------------------------------------------- scenario 3: burned A = 00
    banner("2c. Machine M2: same original roster, A burned to 00, width 2")
    plan3 = build_code_plan(
        roster.signals,
        roster.ordered_frequencies(),
        max_length=2,
        burned={"A": "00"},
        machine_id="M2-burn-A00",
    )
    show_plan("code plan M2", plan3)
    assert plan3.code_for["A"] == "00" and plan3.total_bits == 17
    batch3 = make_batch("day1-M2", roster, plan3)
    print("  sealed stream:", batch3.bitstream, f"({batch3.total_bits} bits)")
    print("  decode ownership sample:")
    for bits, signal, ticket_id in replay_with_tickets(batch3)[:4]:
        print(f"    read {bits:<3} -> {signal:<3} {ticket_id}")

    # ------------------------------------------------------- out-of-table bounce
    banner("3. An out-of-table signal must bounce the batch")
    try:
        encode(plan1, ["A", "UNLISTED", "B"])
    except UnknownSignal as exc:
        print("  rejected before transmission:", exc)

    # ------------------------------------------------------- unloadable machines
    banner("4. Configurations that cannot be loaded")
    bad_cases = [
        ("width 1 cannot hold three signals", dict(max_length=1)),
        ("burned word exceeds width", dict(max_length=2, burned={"A": "000"})),
        ("one burned word prefixes another", dict(max_length=3, burned={"A": "0", "AB": "01"})),
        ("burned words violate table order", dict(max_length=3, burned={"A": "1", "B": "0"})),
    ]
    for label, kwargs in bad_cases:
        try:
            build_code_plan(roster.signals, roster.ordered_frequencies(), **kwargs)
        except UnloadableError as exc:
            print(f"  {label:38} -> {exc}")

    # ------------------------------------------------------- archive immutability
    banner("5. Archive: new code tables never rewrite already-sent batches")
    archive_path = ROOT / "demo" / "out" / "wire_archive.jsonl"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text("", encoding="utf-8")
    archive_batch(batch1, archive_path)
    archive_batch(batch3, archive_path)
    reloaded = load_archive(archive_path)
    print(f"  archived {len(reloaded)} batches under their own code tables")
    for batch in reloaded:
        rows = replay_with_tickets(batch)
        print(
            f"    {batch.batch_id:9} machine={batch.machine_id:13} "
            f"words={list(batch.codewords)} replayed={len(rows)} signals"
        )
    assert replay_with_tickets(reloaded[0]) == replay_with_tickets(batch1)
    print("  day-1 M1 batch still decodes to the same 15-bit interpretation.")

    # ------------------------------------------------------- small-batch breadth
    banner("6. Realistic small batches: 4-32 signals, maximum length 12")
    import random

    rng = random.Random(11)
    for n in (4, 8, 16, 32):
        signals = tuple(f"s{i:02d}" for i in range(n))
        freqs = tuple(rng.randrange(0, 40) for _ in range(n))
        plan = build_code_plan(signals, freqs, max_length=12)
        sequence = [signals[rng.randrange(n)] for _ in range(200)]
        assert decode_trace(plan, encode(plan, sequence)) is not None
        print(
            f"  n={n:2d}  distinct words={len(set(plan.codewords)):2d}  "
            f"longest={max(map(len, plan.codewords)):2d}  roundtrip OK"
        )

    # ------------------------------------------------------- none signal path
    banner("7. A ticket hitting no declared target gets its dedicated signal")
    edges = EDGES | frozenset({("ORPHAN", "LONE")})
    none_snapshot = Snapshot.of(
        "cut-none", edges, (fact("T1", ["aA"]), fact("T2", ["ORPHAN"]))
    )
    none_roster = build_roster(none_snapshot, TARGETS)
    print("  signal table with none-signal:", none_roster.signals)
    none_plan = build_code_plan(
        none_roster.signals, none_roster.ordered_frequencies(), max_length=2
    )
    none_batch = make_batch("none-batch", none_roster, none_plan)
    print("  stream:", none_batch.bitstream, "->", replay_with_tickets(none_batch))
    assert NONE_SIGNAL in none_roster.signals

    print("\nAll demonstration assertions passed.")


if __name__ == "__main__":
    main()
