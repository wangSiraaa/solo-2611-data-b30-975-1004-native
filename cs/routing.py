"""Pure routing algorithms: which registered business desks can take a ticket.

A *desk* (业务席) is registered on one catalog node.  A ticket carries one or
more direct classes.  A desk can reach the ticket when any of the ticket's
direct classes equals the desk's node or can reach it through the
child -> parent DAG edges (the same coverage rule as the summary store).

Two desks reachable by the same ticket are then reconciled:

* if desk 甲's node is a **strict ancestor** of desk 乙's node, the more
  specific desk 乙 wins and 甲 is pruned;
* desks on incomparable nodes are parallel and both survive;
* several desks registered on the *same* node are never ancestors of one
  another, so same-node desks (which may share a display name) always survive
  together.

Everything here is a pure function over edges, nodes and a registry snapshot;
persistence and cut pinning live in :mod:`cs.store`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from . import catalog


@dataclass(frozen=True)
class Desk:
    """A registered business desk. Identity is ``desk_id``; node carries it."""

    desk_id: str
    node_id: str
    name: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "Desk":
        return cls(
            desk_id=str(raw["desk_id"]),
            node_id=str(raw["node_id"]),
            name=str(raw.get("name", raw["desk_id"])),
        )

    def as_dict(self) -> dict[str, str]:
        return {"desk_id": self.desk_id, "node_id": self.node_id, "name": self.name}


def normalise_desks(raw_desks: Iterable[Mapping[str, object]]) -> list[Desk]:
    desks: list[Desk] = []
    seen: set[str] = set()
    for raw in raw_desks:
        desk = Desk.from_dict(raw)
        if not desk.desk_id:
            raise ValueError("desk_id must be a non-empty string")
        if desk.desk_id in seen:
            raise ValueError(f"duplicate desk_id {desk.desk_id!r}")
        if not desk.node_id:
            raise ValueError(f"desk {desk.desk_id!r}: node_id must be a non-empty string")
        seen.add(desk.desk_id)
        desks.append(desk)
    return desks


def _reachable_nodes(direct_classes: Sequence[str], edges) -> set[str]:
    """Union of nodes that any direct class can reach (the class itself included)."""

    reachable: set[str] = set()
    for cls in set(direct_classes):
        reachable |= catalog.ancestors(cls, edges, include_self=True)
    return reachable


def reachable_desks(
    direct_classes: Sequence[str], desks: Sequence[Desk], edges
) -> dict[str, set[str]]:
    """Map every reachable desk id to the set of direct classes that reach it."""

    reachable_nodes = _reachable_nodes(direct_classes, edges)
    hits: dict[str, set[str]] = {}
    for desk in desks:
        if desk.node_id not in reachable_nodes:
            continue
        covering = {
            cls
            for cls in set(direct_classes)
            if desk.node_id in catalog.ancestors(cls, edges, include_self=True)
        }
        hits[desk.desk_id] = covering
    return hits


def _witness_path(direct_classes: Sequence[str], node_id: str, edges) -> list[str]:
    dist, next_hop = catalog.distances_to_target(node_id, edges)
    path = catalog.choose_path(direct_classes, node_id, dist, next_hop)
    assert path is not None  # the desk is reachable, so a witness must exist
    return path


def route_ticket(
    direct_classes: Sequence[str], desks: Sequence[Desk], edges, nodes: Mapping[str, str]
) -> dict:
    """Resolve the full routing verdict for one ticket at one catalog shape.

    The returned document keeps both the raw reachable set (with a witness path
    per desk) and the pruned set of desks that may actually take the ticket, so
    every inclusion or exclusion is auditable.
    """

    desk_by_id = {desk.desk_id: desk for desk in desks}
    hits = reachable_desks(direct_classes, desks, edges)

    # Strict-ancestor relation over the *nodes* of reachable desks. Desks on one
    # node can never be strict ancestors of each other.
    reachable_node_set = {desk_by_id[did].node_id for did in hits}
    pruned: list[dict] = []
    retained_ids: set[str] = set()
    for desk_id in sorted(hits):
        node_id = desk_by_id[desk_id].node_id
        more_specific = sorted(
            other
            for other in hits
            if other != desk_id
            and desk_by_id[other].node_id != node_id
            and node_id
            in catalog.ancestors(desk_by_id[other].node_id, edges, include_self=False)
        )
        if more_specific:
            pruned.append(
                {
                    "desk_id": desk_id,
                    "node_id": node_id,
                    "node_name": nodes.get(node_id, node_id),
                    "reason": "strict-ancestor",
                    "kept_descendants": [
                        {
                            "desk_id": other,
                            "node_id": desk_by_id[other].node_id,
                        }
                        for other in more_specific
                    ],
                }
            )
        else:
            retained_ids.add(desk_id)

    reachable_entries = []
    for desk_id in sorted(hits):
        desk = desk_by_id[desk_id]
        reachable_entries.append(
            {
                "desk_id": desk.desk_id,
                "node_id": desk.node_id,
                "node_name": nodes.get(desk.node_id, desk.node_id),
                "desk_name": desk.name,
                "covered_by": sorted(hits[desk_id]),
                "witness_path": _witness_path(direct_classes, desk.node_id, edges),
                "retained": desk_id in retained_ids,
            }
        )

    retained = [entry for entry in reachable_entries if entry["retained"]]
    return {
        "direct_classes": sorted(set(direct_classes)),
        "reachable_desks": reachable_entries,
        "retained_desks": [entry["desk_id"] for entry in retained],
        "pruned_desks": pruned,
        "empty": not retained_ids,
    }


def checkout(
    tickets: Sequence[Mapping[str, object]],
    desks: Sequence[Desk],
    edges,
    nodes: Mapping[str, str],
) -> dict:
    """Sign out every ticket: its retainable desks plus the full evidence.

    Withdrawn (inactive) tickets are reported as ``not_processed`` and never
    enter the actionable list; tickets whose pruned set is empty are returned
    for re-registration.
    """

    actionable: list[dict] = []
    returned: list[dict] = []
    skipped: list[dict] = []
    for ticket in sorted(tickets, key=lambda t: str(t["ticket_id"])):
        ticket_id = str(ticket["ticket_id"])
        if not bool(ticket.get("active", True)):
            skipped.append(
                {
                    "ticket_id": ticket_id,
                    "reason": "withdrawn",
                    "detail": "withdrawn tickets are not handled at handover",
                }
            )
            continue
        classes = list(ticket["classes"])  # type: ignore[arg-type]
        verdict = route_ticket(classes, desks, edges, nodes)
        entry = {
            "ticket_id": ticket_id,
            "direct_classes": verdict["direct_classes"],
            "retained_desks": verdict["retained_desks"],
            "reachable_desks": verdict["reachable_desks"],
            "pruned_desks": verdict["pruned_desks"],
        }
        if verdict["empty"]:
            returned.append(
                {
                    "ticket_id": ticket_id,
                    "direct_classes": verdict["direct_classes"],
                    "reason": "no-retainable-desk",
                    "detail": (
                        "no registered desk is reachable from any direct class "
                        "after strict-ancestor pruning; return for registration"
                    ),
                    "reachable_desks": verdict["reachable_desks"],
                }
            )
        else:
            actionable.append(entry)
    return {
        "format": "desk-routing/1",
        "actionable": actionable,
        "returned": returned,
        "not_processed": skipped,
    }


def explain_dispute(
    direct_classes: Sequence[str],
    desks: Sequence[Desk],
    edges,
    nodes: Mapping[str, str],
    *,
    focus_desk_ids: Sequence[str] | None = None,
) -> dict:
    """Explain, for one contested ticket, why each desk is in or out.

    Answers both dispute flavours: why two same-named desks both survive, and
    why a particular desk was excluded (unreachable, or pruned as an ancestor).
    """

    verdict = route_ticket(direct_classes, desks, edges, nodes)
    desk_by_id = {desk.desk_id: desk for desk in desks}
    explanations: list[dict] = []
    focus = set(focus_desk_ids) if focus_desk_ids else {d.desk_id for d in desks}
    for desk in desks:
        if desk.desk_id not in focus:
            continue
        entry = next(
            (e for e in verdict["reachable_desks"] if e["desk_id"] == desk.desk_id), None
        )
        prune = next(
            (p for p in verdict["pruned_desks"] if p["desk_id"] == desk.desk_id), None
        )
        if entry is None:
            status = "excluded-unreachable"
            detail = "no direct class can reach this desk's node"
        elif prune is not None:
            status = "excluded-ancestor"
            detail = (
                "a strictly more specific desk is reachable; this ancestor desk "
                "is pruned in favour of "
                + ", ".join(p["desk_id"] for p in prune["kept_descendants"])
            )
        else:
            status = "retained"
            same_node = sorted(
                other.desk_id
                for other in desks
                if other.node_id == desk.node_id and other.desk_id != desk.desk_id
            )
            detail = (
                "parallel desks on incomparable nodes are both retained"
                if not same_node
                else "shares its node with "
                + ", ".join(same_node)
                + "; same-node desks are never strict ancestors and both survive"
            )
        explanations.append(
            {
                "desk_id": desk.desk_id,
                "desk_name": desk.name,
                "node_id": desk.node_id,
                "node_name": nodes.get(desk.node_id, desk.node_id),
                "status": status,
                "detail": detail,
                "witness_path": entry["witness_path"] if entry else None,
                "covered_by": entry["covered_by"] if entry else [],
            }
        )
    return {
        "format": "desk-dispute/1",
        "direct_classes": sorted(set(direct_classes)),
        "retained_desks": verdict["retained_desks"],
        "explanations": sorted(explanations, key=lambda e: e["desk_id"]),
    }
