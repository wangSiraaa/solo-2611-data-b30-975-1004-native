"""Pure ballot-register algorithms over a classification DAG and one fact cut.

The chamber votes on exhibition proposals (catalog targets).  Every in-force
ticket holds exactly one vote; its weight is always one and zero-amount
tickets still vote.  For a ticket with several direct classes there is still
only one ballot: its distance for a proposal is the shortest number of upward
edges from *any* of its direct classes to that proposal.

A two-proposal vote
-------------------
``dist_t`` is the ticket's shortest distance to proposal ``t`` (``None`` when
no direct class can reach ``t``).

* both reachable: the strictly shorter distance wins; an equal distance is an
  abstention.  Such a ballot is *contested* because it flips to a vote under
  the smallest catalog change.
* exactly one reachable: that proposal wins the ballot (reachable beats
  unreachable).
* neither reachable: abstention.

Abstention is never a vote.  The vote at the proposal level is the majority of
ballots.  When the two sides are equal the vote is a *tie*; the selected chair
``order`` then seats the proposal that comes first (it is a tie-break between
the two remaining proposals, not a per-ticket vote).  Without an order a tie
stays a draw.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Protocol, Sequence

from . import catalog

Edge = tuple[str, str]

SIDE_LEFT = "left"
SIDE_RIGHT = "right"
SIDE_ABSTAIN = "abstain"


class TicketLike(Protocol):
    ticket_id: str
    amount: int
    classes: Sequence[str]


def class_distance(
    classes: Iterable[str], dist: Mapping[str, int]
) -> int | None:
    """Shortest edge count from any direct class to the proposal.

    Two direct classes and two routes from one class never add up; the minimum
    is taken.  ``None`` means no direct class can reach the proposal.
    """

    best: int | None = None
    for cls in set(classes):
        d = dist.get(cls)
        if d is None:
            continue
        if best is None or d < best:
            best = d
    return best


def side_of(
    left_class: int | None, right_class: int | None
) -> str:
    if left_class is not None and right_class is not None:
        if left_class < right_class:
            return SIDE_LEFT
        if right_class < left_class:
            return SIDE_RIGHT
        return SIDE_ABSTAIN
    if left_class is not None:
        return SIDE_LEFT
    if right_class is not None:
        return SIDE_RIGHT
    return SIDE_ABSTAIN


def _winner_from_counts(
    left: str,
    right: str,
    left_votes: int,
    right_votes: int,
    order: Mapping[str, int] | None,
) -> tuple[str | None, str | None, str]:
    if left_votes > right_votes:
        return left, right, "votes"
    if right_votes > left_votes:
        return right, left, "votes"
    if order is not None:
        lo, ro = order.get(left), order.get(right)
        if lo is not None and ro is not None and lo != ro:
            if lo < ro:
                return left, right, "order"
            return right, left, "order"
    return None, None, "tie"


def match_ballots(
    left: str,
    right: str,
    *,
    edges: Iterable[Edge],
    tickets: Iterable[TicketLike],
) -> list[dict]:
    """Per-ticket distance table for a pair, with deterministic witness paths."""

    edge_set = frozenset(edges)
    left_dist, left_hop = catalog.distances_to_target(left, edge_set)
    right_dist, right_hop = catalog.distances_to_target(right, edge_set)
    rows: list[dict] = []
    for ticket in tickets:
        classes = tuple(ticket.classes)
        lpath = catalog.choose_path(classes, left, left_dist, left_hop)
        rpath = catalog.choose_path(classes, right, right_dist, right_hop)
        lc = len(lpath) - 1 if lpath is not None else None
        rc = len(rpath) - 1 if rpath is not None else None
        side = side_of(lc, rc)
        rows.append(
            {
                "ticket_id": ticket.ticket_id,
                "amount": ticket.amount,
                "classes": list(classes),
                "to_left": {"distance": lc, "path": lpath},
                "to_right": {"distance": rc, "path": rpath},
                "side": side,
                "contested": lc is not None and rc is not None and lc == rc,
            }
        )
    return sorted(rows, key=lambda r: r["ticket_id"])


def tally_pair(
    left: str,
    right: str,
    *,
    edges: Iterable[Edge],
    tickets: Iterable[TicketLike],
    order: Sequence[str] | None = None,
) -> dict:
    """Full record of one pair: vote counts, abstainers, contested paths.

    ``order`` only breaks a proposal-level tie (equal side counts).  Equal
    per-ticket distances remain abstentions and are reported as contested.
    """

    edge_set = frozenset(edges)
    rows = match_ballots(left, right, edges=edge_set, tickets=tickets)
    left_votes = [r for r in rows if r["side"] == SIDE_LEFT]
    right_votes = [r for r in rows if r["side"] == SIDE_RIGHT]
    abstain = [r for r in rows if r["side"] == SIDE_ABSTAIN]
    contested = [r for r in abstain if r["contested"]]

    order_index = (
        {node: i for i, node in enumerate(order)} if order is not None else None
    )
    winner, loser, resolved = _winner_from_counts(
        left, right, len(left_votes), len(right_votes), order_index
    )
    return {
        "left": left,
        "right": right,
        "left_votes": len(left_votes),
        "right_votes": len(right_votes),
        "abstentions": len(abstain),
        "contested": len(contested),
        "left_ticket_ids": [r["ticket_id"] for r in left_votes],
        "right_ticket_ids": [r["ticket_id"] for r in right_votes],
        "abstain_ticket_ids": [r["ticket_id"] for r in abstain],
        "contested_ticket_ids": [r["ticket_id"] for r in contested],
        "winner": winner,
        "loser": loser,
        "resolved_by": resolved,
        "tie_broken_by_order": resolved == "order",
        "ballots": {
            "left": left_votes,
            "right": right_votes,
            "abstain": abstain,
        },
    }
