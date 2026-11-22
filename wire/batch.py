"""Batch envelope: roster + code plan + bit stream, archived together.

A batch is frozen the moment it is sent.  Its archive record carries the full
signal table and the exact code table used, so re-decoding years later does
not depend on any later machine configuration: publishing a new code table
never rewrites an already-sent batch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from wire.codebook import CodePlan, DecodeError, UnknownSignal, decode, decode_trace, encode
from wire.manifest import Roster


class BatchError(ValueError):
    """A batch envelope fails its own sign-off checks."""


@dataclass(frozen=True)
class WireBatch:
    """One transmitted batch and everything needed to verify it later."""

    batch_id: str
    cut_id: str
    machine_id: str | None
    signals: tuple[str, ...]
    frequencies: tuple[int, ...]
    codewords: tuple[str, ...]
    ticket_order: tuple[str, ...]
    ticket_signals: Mapping[str, str]
    bitstream: str
    total_bits: int

    @property
    def plan(self) -> CodePlan:
        return CodePlan(
            signals=self.signals,
            frequencies=self.frequencies,
            codewords=self.codewords,
            max_length=max(len(word) for word in self.codewords),
            burned={},
            total_bits=self.total_bits,
            machine_id=self.machine_id,
        )

    def to_record(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "cut_id": self.cut_id,
            "machine_id": self.machine_id,
            "signal_table": list(self.signals),
            "frequencies": list(self.frequencies),
            "codewords": list(self.codewords),
            "ticket_order": list(self.ticket_order),
            "ticket_signals": dict(self.ticket_signals),
            "bitstream": self.bitstream,
            "total_bits": self.total_bits,
        }

    @classmethod
    def from_record(cls, record: Mapping) -> "WireBatch":
        return cls(
            batch_id=record["batch_id"],
            cut_id=record["cut_id"],
            machine_id=record.get("machine_id"),
            signals=tuple(record["signal_table"]),
            frequencies=tuple(record["frequencies"]),
            codewords=tuple(record["codewords"]),
            ticket_order=tuple(record["ticket_order"]),
            ticket_signals=dict(record["ticket_signals"]),
            bitstream=record["bitstream"],
            total_bits=record["total_bits"],
        )


def make_batch(
    batch_id: str,
    roster: Roster,
    plan: CodePlan,
    *,
    ticket_order: Sequence[str] | None = None,
) -> WireBatch:
    """Seal one batch from a signed roster and a machine-specific code plan."""

    if plan.signals != roster.signals:
        raise BatchError("code plan signal table does not match the signed roster")
    expected_freq = roster.ordered_frequencies()
    if plan.frequencies != expected_freq:
        raise BatchError("code plan frequencies do not match the signed roster")

    order = tuple(ticket_order or [ticket.ticket_id for ticket in roster.tickets])
    roster_ids = {ticket.ticket_id for ticket in roster.tickets}
    if set(order) != roster_ids or len(order) != len(roster_ids):
        raise BatchError("ticket order must list every roster ticket exactly once")

    signal_sequence = [roster.ticket_signals[ticket_id] for ticket_id in order]
    bitstream = encode(plan, signal_sequence)
    return WireBatch(
        batch_id=batch_id,
        cut_id=roster.cut_id,
        machine_id=plan.machine_id,
        signals=roster.signals,
        frequencies=expected_freq,
        codewords=plan.codewords,
        ticket_order=order,
        ticket_signals=dict(roster.ticket_signals),
        bitstream=bitstream,
        total_bits=len(bitstream),
    )


def replay_batch(batch: WireBatch) -> list[str]:
    """Decode the archived bit stream back to the ordered signal list."""

    return decode(batch.plan, batch.bitstream)


def replay_with_tickets(batch: WireBatch) -> list[tuple[str, str, str]]:
    """Decode to ``(codeword bits, signal, ticket id)`` and check ownership."""

    steps = decode_trace(batch.plan, batch.bitstream)
    if len(steps) != len(batch.ticket_order):
        raise BatchError("decoded signal count does not match the archived ticket count")
    rows: list[tuple[str, str, str]] = []
    for (bits, signal), ticket_id in zip(steps, batch.ticket_order):
        archived_signal = batch.ticket_signals[ticket_id]
        if signal != archived_signal:
            raise BatchError(
                f"ticket {ticket_id!r} decodes to {signal!r}, archive says {archived_signal!r}"
            )
        rows.append((bits, signal, ticket_id))
    return rows


def archive_batch(batch: WireBatch, path: str | Path) -> Path:
    """Append the batch as one immutable JSON line; existing files stay intact."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(batch.to_record(), ensure_ascii=False, sort_keys=True) + "\n")
    return path


def load_archive(path: str | Path) -> list[WireBatch]:
    path = Path(path)
    batches: list[WireBatch] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            batches.append(WireBatch.from_record(json.loads(line)))
    return batches


__all__ = [
    "BatchError",
    "WireBatch",
    "UnknownSignal",
    "DecodeError",
    "make_batch",
    "replay_batch",
    "replay_with_tickets",
    "archive_batch",
    "load_archive",
]
