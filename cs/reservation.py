"""Pure rules for the orphan-shop reservation waiting queue.

The qualification office publishes committed *cuts*.  A cut records, for every
ticket, the ticket's direct classes plus the catalog edge set at that cut.  A
branch manages a set of classes; a ticket is a *candidate* for the branch at a
cut when any of its direct classes can reach a managed class through
child->parent edges.

Ordering rule
-------------
A ticket is ranked by the shift in which its **current continuous stay** began,
then by its stable ticket id::

    sort_key = (entered_shift, ticket_id)

* stay continuously in the branch's waiting set  -> the shift of first entry is
  kept, even if the path by which it reaches a managed class changes.
* disappear from one committed cut and return later -> it queues from the new
  shift, i.e. it sorts after every ticket whose current stay began earlier.
* within one committed batch a ticket may drop one path and gain another and
  remain reachable throughout: it never shows an absent cut, so its stay is
  continuous and its age is preserved.

The projection takes the candidate set *before* the current cut as ``previous``.
That makes "absent at the previous committed cut" exactly the re-entry test, and
it makes a mid-batch path swap invisible (the ticket is reachable on both
sides), which is what the age-preservation rule asks for.

Gate events
-----------
An area may permit queue-gate changes between submitted cuts (a supervisor
admits or removes someone before the next qualification batch).  Because those
are timestamped and immutable, replay is deterministic: a cut's projection is
taken after every gate due at or before that cut, and each gate projects from
the cut in force at the gate's instant.  A ticket that a gate removes is absent
from the next projection, so if it later returns it starts a fresh stay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from . import catalog

Edge = tuple[str, str]

# previous[ticket_id] = (entered_shift, entered_seq) of its current stay
Stay = tuple[str, int]


@dataclass(frozen=True)
class Match:
    """Evidence that one ticket is a candidate for a branch."""

    ticket_id: str
    class_id: str
    path: list[str]

    def as_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "class_id": self.class_id,
            "path": list(self.path),
        }


@dataclass(frozen=True)
class Candidate:
    ticket_id: str
    entered_shift: str
    entered_seq: int
    match: Match

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.entered_shift, self.ticket_id)

    def as_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "entered_shift": self.entered_shift,
            "match": self.match.as_dict(),
        }


@dataclass
class Projection:
    cut_id: str
    branch_id: str
    managed_classes: tuple[str, ...]
    candidates: list[Candidate] = field(default_factory=list)
    leavers: list[str] = field(default_factory=list)

    @property
    def waiting(self) -> list[Candidate]:
        return list(self.candidates)

    @property
    def eligible(self) -> list[str]:
        return [c.ticket_id for c in self.candidates]

    def stay_map(self) -> dict[str, Stay]:
        return {c.ticket_id: (c.entered_shift, c.entered_seq) for c in self.candidates}

    def as_dict(self) -> dict:
        return {
            "cut_id": self.cut_id,
            "branch_id": self.branch_id,
            "managed_classes": list(self.managed_classes),
            "waiting": [c.as_dict() for c in self.candidates],
            "eligible": self.eligible,
            "leavers": list(self.leavers),
        }


def managed_reachability(
    managed_classes: Sequence[str], edges: Iterable[Edge]
) -> dict[str, tuple[dict[str, int], dict[str, str]]]:
    """Per managed class, the reverse-BFS distance/next-hop maps.

    Computed once per cut and shared by every ticket, so projecting the whole
    branch is O(M*(V+E) + T*C*M) rather than a BFS per ticket.
    """

    edge_tuple = tuple(edges)
    return {
        target: catalog.distances_to_target(target, edge_tuple)
        for target in sorted(set(managed_classes))
    }


def best_match(
    ticket_id: str,
    ticket_classes: Sequence[str],
    reach: Mapping[str, tuple[Mapping[str, int], Mapping[str, str]]],
) -> Match | None:
    """Pick the deterministic shortest witness that the ticket reaches a
    managed class.  Ties break by (path length, direct class, path, target)."""

    best: tuple[int, str, tuple[str, ...], str] | None = None
    for cls in sorted(set(ticket_classes)):
        for target in sorted(reach):
            dist, next_hop = reach[target]
            if cls not in dist:
                continue
            path = catalog.path_from(cls, target, dist, next_hop)
            assert path is not None
            candidate = (len(path), cls, tuple(path), target)
            if best is None or candidate < best:
                best = candidate
    if best is None:
        return None
    _, cls, path, _target = best
    return Match(ticket_id=ticket_id, class_id=cls, path=list(path))


def compute_projection(
    *,
    cut_id: str,
    branch_id: str,
    managed_classes: Sequence[str],
    edges: Iterable[Edge],
    tickets: Mapping[str, Sequence[str]],
    previous: Mapping[str, Stay],
    entered_shift: str,
    entered_seq: int,
    initial_stays: Mapping[str, Stay] | None = None,
) -> Projection:
    """Project the branch's waiting set for one cut.

    ``tickets`` maps ticket id to its direct classes at this cut.  ``previous``
    is the candidate stay map left by the preceding processed state.  A ticket
    present in ``previous`` keeps its ``entered_shift``; a ticket absent from it
    (whether brand new or returning after an absent cut) enters now -- unless
    ``initial_stays`` carries its historical arrival order from the seeded old
    hall, which is used only when it has not entered before.
    """

    reach = managed_reachability(managed_classes, edges)
    candidates: list[Candidate] = []
    for ticket_id in sorted(tickets):
        match = best_match(ticket_id, tickets[ticket_id], reach)
        if match is None:
            continue
        stay = previous.get(ticket_id)
        if stay is None:
            stay = (initial_stays or {}).get(ticket_id)
        if stay is None:
            entered, seq = entered_shift, entered_seq
        else:
            entered, seq = stay
        candidates.append(
            Candidate(
                ticket_id=ticket_id,
                entered_shift=entered,
                entered_seq=seq,
                match=match,
            )
        )
    candidates.sort(key=lambda c: c.sort_key)
    present = {c.ticket_id for c in candidates}
    leavers = sorted(set(previous) - present)
    return Projection(
        cut_id=cut_id,
        branch_id=branch_id,
        managed_classes=tuple(sorted(set(managed_classes))),
        candidates=candidates,
        leavers=leavers,
    )
