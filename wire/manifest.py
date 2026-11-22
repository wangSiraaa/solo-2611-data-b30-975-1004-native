"""Station side: sign off a manifest from one committed directory/fact cut.

A cut is immutable once published.  For every active ticket the station walks
the real directory of that cut and computes the *complete* set of declared
targets it hits.  The whole set, rendered in the manifest's target order, is
one signal; several direct labels or several hit paths never multiply the
ticket.  A ticket that hits no declared target gets a dedicated none-signal.

Frequencies count tickets, never money: a zero-amount ticket is counted just
like any other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

# Dedicated signal for a ticket that hits none of the declared targets.
NONE_SIGNAL = "∅"  # ∅


class ManifestError(ValueError):
    """A roster, active list, or cut violates the signed-off rules."""


@dataclass(frozen=True)
class TicketFact:
    """One active ticket as seen on the committed cut.

    ``classes`` are its direct directory labels; duplicate labels collapse.
    ``amount`` is tracked for completeness, never used for counting.
    """

    ticket_id: str
    classes: frozenset[str]
    amount: int = 0


@dataclass(frozen=True)
class Snapshot:
    """One committed, already-published directory/fact cut.

    ``edges`` point from a child class to a parent class, exactly like the
    classification catalog this station sits on top of.  A class may have
    several parents; the cut is checked to be acyclic at publication time.
    """

    cut_id: str
    edges: frozenset[tuple[str, str]]
    tickets: tuple[TicketFact, ...]

    @classmethod
    def of(cls, cut_id: str, edges: Iterable[tuple[str, str]], tickets: Iterable[TicketFact]) -> "Snapshot":
        edge_set = frozenset(edges)
        _assert_acyclic(edge_set)
        seen: set[str] = set()
        facts: list[TicketFact] = []
        for ticket in tickets:
            if ticket.ticket_id in seen:
                raise ManifestError(f"duplicate ticket on cut {cut_id!r}: {ticket.ticket_id!r}")
            seen.add(ticket.ticket_id)
            facts.append(ticket)
        return cls(cut_id, edge_set, tuple(facts))


@dataclass(frozen=True)
class Roster:
    """The signed-off delivery list for one cut.

    ``targets`` is the ordered list of target node names; ``signals`` is the
    fixed ordered signal table the equipment operator receives.  It contains
    every signal the station may emit, including zero-frequency ones and the
    none-signal, and its order is the lexicographic codeword order that must
    be preserved on the wire.
    """

    cut_id: str
    targets: tuple[str, ...]
    signals: tuple[str, ...]
    tickets: tuple[TicketFact, ...]
    ticket_signals: dict[str, str]
    frequencies: dict[str, int]
    hit_paths: dict[str, tuple[tuple[str, ...], ...]] = field(default_factory=dict)
    none_signal: str = NONE_SIGNAL

    def ordered_frequencies(self) -> tuple[int, ...]:
        return tuple(self.frequencies.get(signal, 0) for signal in self.signals)


def _assert_acyclic(edges: frozenset[tuple[str, str]]) -> None:
    parents: dict[str, set[str]] = {}
    nodes: set[str] = set()
    for child, parent in edges:
        parents.setdefault(child, set()).add(parent)
        nodes.add(child)
        nodes.add(parent)
    state: dict[str, int] = {node: 0 for node in nodes}  # 0 new, 1 open, 2 done
    stack: list[tuple[str, list[str]]] = []
    for start in sorted(nodes):
        if state[start] != 0:
            continue
        state[start] = 1
        stack.append((start, sorted(parents.get(start, ()))))
        while stack:
            node, pending = stack[-1]
            if not pending:
                state[node] = 2
                stack.pop()
                continue
            nxt = pending.pop()
            if state[nxt] == 1:
                raise ManifestError(f"directory cut contains a cycle through {nxt!r}")
            if state[nxt] == 0:
                state[nxt] = 1
                stack.append((nxt, sorted(parents.get(nxt, ()))))


def reachable_targets(
    classes: Iterable[str],
    targets: Sequence[str],
    edges: frozenset[tuple[str, str]],
) -> tuple[frozenset[str], dict[str, tuple[str, ...]]]:
    """Return the hit target set and one shortest witness path per hit.

    A target is always in its own coverage (the single-node path), so a direct
    label equal to a target counts.  Multi-parent diamonds and several direct
    labels are explored together and each target is reported at most once.
    """

    target_set = set(targets)
    parents: dict[str, set[str]] = {}
    for child, parent in edges:
        parents.setdefault(child, set()).add(parent)

    hits: dict[str, tuple[str, ...]] = {}
    visited: set[str] = set()
    # BFS layers from every direct label at once; parent pointers recover one
    # deterministic shortest witness for each target reached.
    frontier: list[str] = []
    parent_of: dict[str, str] = {}
    for label in sorted(set(classes)):
        if label not in visited:
            visited.add(label)
            frontier.append(label)
            if label in target_set and label not in hits:
                hits[label] = (label,)
    while frontier:
        nxt: list[str] = []
        for node in frontier:
            for parent in sorted(parents.get(node, ())):
                if parent in visited:
                    continue
                visited.add(parent)
                parent_of[parent] = node
                nxt.append(parent)
                if parent in target_set and parent not in hits:
                    path = [parent]
                    cur = parent
                    while cur in parent_of:
                        cur = parent_of[cur]
                        path.append(cur)
                    hits[parent] = tuple(reversed(path))
        frontier = nxt

    ordered = [target for target in targets if target in hits]
    return frozenset(ordered), {target: hits[target] for target in ordered}


def signal_key(hit_targets: Sequence[str], none_signal: str = NONE_SIGNAL) -> str:
    """Render a complete hit set as its single signal.

    Targets are joined in manifest order, so the same hit set always produces
    the same key, and distinct hit sets produce distinct keys.
    """

    if not hit_targets:
        return none_signal
    return "".join(hit_targets)


def build_roster(
    snapshot: Snapshot,
    targets: Sequence[str],
    *,
    include_none: bool = True,
    none_signal: str = NONE_SIGNAL,
) -> Roster:
    """Sign off a manifest for ``snapshot`` against ordered ``targets``.

    Every reachable non-empty hit set becomes a signal in table order, plus
    the none-signal when it can occur (or when explicitly retained).  Two
    stations signing the same cut and target order must get identical rosters.
    """

    target_tuple = tuple(targets)
    if len(set(target_tuple)) != len(target_tuple):
        raise ManifestError("duplicate target in delivery list")
    known_classes: set[str] = set()
    for child, parent in snapshot.edges:
        known_classes.add(child)
        known_classes.add(parent)
    for ticket in snapshot.tickets:
        unknown = sorted(set(ticket.classes) - known_classes)
        if unknown:
            raise ManifestError(
                f"ticket {ticket.ticket_id!r} labels unknown classes: {', '.join(unknown)}"
            )

    ticket_signals: dict[str, str] = {}
    frequencies: dict[str, int] = {}
    hit_paths: dict[str, tuple[tuple[str, ...], ...]] = {}
    observed: set[str] = set()
    signal_indices: dict[str, tuple[int, ...]] = {}
    for ticket in snapshot.tickets:
        hits, paths = reachable_targets(ticket.classes, target_tuple, snapshot.edges)
        ordered_hits = [target for target in target_tuple if target in hits]
        key = signal_key(ordered_hits, none_signal)
        ticket_signals[ticket.ticket_id] = key
        frequencies[key] = frequencies.get(key, 0) + 1
        observed.add(key)
        signal_indices.setdefault(
            key, tuple(target_tuple.index(target) for target in ordered_hits)
        )
        # One witness per (ticket, target); multiple paths are not multiplied.
        hit_paths[ticket.ticket_id] = tuple(
            paths[target] for target in target_tuple if target in paths
        )

    ordered = sorted(
        (key for key in observed if key != none_signal),
        key=lambda key: signal_indices[key],
    )
    signals = tuple(ordered + ([none_signal] if include_none and none_signal in observed else []))
    if not signals:
        raise ManifestError("no active tickets and no signals: nothing to sign off")
    for signal in signals:
        frequencies.setdefault(signal, 0)
    return Roster(
        cut_id=snapshot.cut_id,
        targets=target_tuple,
        signals=signals,
        tickets=snapshot.tickets,
        ticket_signals=ticket_signals,
        frequencies=frequencies,
        hit_paths=hit_paths,
        none_signal=none_signal,
    )


def tally(roster: Roster) -> dict[str, int]:
    """Frequency view by signal; counts tickets, zero amounts included."""

    return dict(roster.frequencies)


def verify_active_list(roster: Roster, active_ids: Sequence[str]) -> None:
    """Reject a sign-off that drops or duplicates an active ticket.

    Stations sign off independently, so any station can re-derive the cut and
    compare the claimed active ticket list against the roster.
    """

    claimed = list(active_ids)
    if len(set(claimed)) != len(claimed):
        dupes = sorted({ticket_id for ticket_id in claimed if claimed.count(ticket_id) > 1})
        raise ManifestError(f"duplicate active ticket ids: {', '.join(dupes)}")
    expected = {ticket.ticket_id for ticket in roster.tickets}
    actual = set(claimed)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        raise ManifestError(f"manifest omits active tickets: {', '.join(missing)}")
    if extra:
        raise ManifestError(f"manifest lists tickets absent from the cut: {', '.join(extra)}")
