"""Precise per-ticket flow of an amount across receiving cabinets.

The split is defined exactly as the business rule states:

1. The ticket amount is divided equally among the ticket's **deduplicated
   direct classes**.
2. From a class the share is divided equally among its **direct parents**.
3. Walking upward stops *immediately* at a node registered as a receiving
   cabinet (``cabinets``).  Several paths that reach the same cabinet are
   merged by adding their fractions.
4. A node that has no parent and is not a cabinet drains into the synthetic
   public cabinet (``PUBLIC_CABINET``).

All arithmetic uses :class:`fractions.Fraction`; nothing is rounded here.
Every cabinet share keeps the list of concrete paths that produced it, so the
flow can be re-checked by another operator without trusting this module.

Nothing in this module knows about revisions or storage; callers hand in the
facts / edge set of one cut so that a ledger can only ever describe one cut.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable, Mapping, Sequence

from . import catalog

#: Synthetic sink for shares that walk off a root that is not a cabinet.
PUBLIC_CABINET = "@public"


@dataclass(frozen=True)
class Flow:
    """One concrete upward walk contributing a fraction to one cabinet."""

    cabinet: str
    path: tuple[str, ...]
    share: Fraction


@dataclass(frozen=True)
class TicketSplit:
    ticket_id: str
    revision: int
    amount: int
    direct_classes: tuple[str, ...]
    flows: tuple[Flow, ...]

    @property
    def total(self) -> Fraction:
        return sum((f.share for f in self.flows), Fraction(0))

    def shares(self) -> dict[str, Fraction]:
        merged: dict[str, Fraction] = {}
        for flow in self.flows:
            merged[flow.cabinet] = merged.get(flow.cabinet, Fraction(0)) + flow.share
        return merged


def frac_json(value: Fraction) -> dict[str, Any]:
    """Serialise a fraction without losing precision."""

    return {
        "num": value.numerator,
        "den": value.denominator,
        "text": f"{value.numerator}/{value.denominator}",
    }


def frac_from(obj: Mapping[str, Any]) -> Fraction:
    if "num" in obj and "den" in obj:
        return Fraction(int(obj["num"]), int(obj["den"]))
    if "text" in obj:
        return Fraction(str(obj["text"]))
    raise ValueError(f"cannot read a fraction from {obj!r}")


def _walk(
    node: str,
    share: Fraction,
    path: tuple[str, ...],
    parents: Mapping[str, Iterable[str]],
    cabinets: frozenset[str],
    public: str,
    out: list[Flow],
) -> None:
    if node in cabinets:
        out.append(Flow(cabinet=node, path=path, share=share))
        return
    direct_parents = sorted(set(parents.get(node, ())))
    if not direct_parents:
        out.append(Flow(cabinet=public, path=path, share=share))
        return
    part = share / len(direct_parents)
    for parent in direct_parents:
        if parent in path:  # defensive; the catalog is guaranteed acyclic
            raise ValueError("cycle while splitting: " + " -> ".join(path + (parent,)))
        _walk(parent, part, path + (parent,), parents, cabinets, public, out)


def compute_ticket_split(
    ticket_id: str,
    revision: int,
    amount: int,
    classes: Sequence[str],
    parents: Mapping[str, Iterable[str]],
    cabinets: Iterable[str],
    public: str = PUBLIC_CABINET,
) -> TicketSplit:
    """Split one ticket into exact cabinet shares plus their witness flows."""

    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
        raise ValueError("amount must be a non-negative integer")
    direct = tuple(sorted(set(classes)))
    if not direct:
        raise ValueError("a ticket needs at least one direct class")

    cabinet_set = frozenset(cabinets)
    part = Fraction(amount, len(direct))
    flows: list[Flow] = []
    for cls in direct:
        _walk(cls, part, (cls,), parents, cabinet_set, public, flows)

    total = sum((flow.share for flow in flows), Fraction(0))
    if total != amount:
        # Exact rational arithmetic makes this unreachable; fail loud if the
        # caller handed us an inconsistent graph instead of losing a cent.
        raise AssertionError(
            f"ticket {ticket_id}: split totals {total}, expected {amount}"
        )
    flows.sort(key=lambda f: (f.cabinet, f.path, f.share))
    return TicketSplit(
        ticket_id=ticket_id,
        revision=revision,
        amount=amount,
        direct_classes=direct,
        flows=tuple(flows),
    )


def serialize_ticket(split: TicketSplit) -> dict[str, Any]:
    grouped: dict[str, list[Flow]] = {}
    for flow in split.flows:
        grouped.setdefault(flow.cabinet, []).append(flow)
    shares = []
    for cabinet in sorted(grouped):
        flows = sorted(grouped[cabinet], key=lambda f: f.path)
        total = sum((f.share for f in flows), Fraction(0))
        shares.append(
            {
                "cabinet": cabinet,
                "share": frac_json(total),
                "flows": [
                    {"path": list(f.path), "share": frac_json(f.share)} for f in flows
                ],
            }
        )
    return {
        "ticket_id": split.ticket_id,
        "revision": split.revision,
        "amount": split.amount,
        "direct_classes": list(split.direct_classes),
        "shares": shares,
        "total": frac_json(split.total),
    }


def build_ledger(
    facts: Iterable[Any],
    edges: Iterable[tuple[str, str]],
    cabinets: Iterable[str],
    cut: Mapping[str, Any],
    public: str = PUBLIC_CABINET,
) -> dict[str, Any]:
    """Build a ``split-ledger/1`` payload for one cut.

    ``facts`` is any iterable whose items expose ``ticket_id``, ``revision``,
    ``amount`` and ``classes`` (the store's ``Fact`` fits).
    """

    parents = catalog.build_parents(edges)
    cabinet_set = frozenset(cabinets)
    tickets: list[TicketSplit] = []
    totals: dict[str, Fraction] = {}
    for fact in sorted(facts, key=lambda f: f.ticket_id):
        split = compute_ticket_split(
            fact.ticket_id,
            fact.revision,
            fact.amount,
            fact.classes,
            parents,
            cabinet_set,
            public,
        )
        tickets.append(split)
        for cabinet, share in split.shares().items():
            totals[cabinet] = totals.get(cabinet, Fraction(0)) + share
    return {
        "format": "split-ledger/1",
        "cut": dict(cut),
        "public_cabinet": public,
        "cabinets": sorted(cabinet_set),
        "tickets": [serialize_ticket(t) for t in tickets],
        "totals": {cab: frac_json(value) for cab, value in sorted(totals.items())},
    }


def verify_ledger(
    ledger: Mapping[str, Any],
    *,
    edges: Iterable[tuple[str, str]] | None = None,
    cabinets: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Independent re-check of a ledger.

    When ``edges`` / ``cabinets`` are supplied every witness path is walked
    again: consecutive nodes must be real edges, the walk must start at a
    declared direct class, may not pass through a cabinet, and must finish at
    the cabinet it credits (or at a parent-less non-cabinet for the public
    cabinet).  The checker never trusts the aggregated totals it is checking.
    """

    public = ledger.get("public_cabinet", PUBLIC_CABINET)
    edge_set = set(map(tuple, edges)) if edges is not None else None
    parents = catalog.build_parents(edge_set) if edge_set is not None else {}
    cabinet_set = set(cabinets) if cabinets is not None else None
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = None) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    for ticket in ledger.get("tickets", []):
        tid = ticket["ticket_id"]
        amount = int(ticket["amount"])
        total = frac_from(ticket["total"])
        check(f"{tid}: total equals amount", total == amount, frac_json(total))
        share_sum = Fraction(0)
        for share in ticket.get("shares", []):
            cabinet = share["cabinet"]
            value = frac_from(share["share"])
            share_sum += value
            flow_sum = sum(
                (frac_from(f["share"]) for f in share.get("flows", [])), Fraction(0)
            )
            check(
                f"{tid}->{cabinet}: flows sum to share",
                flow_sum == value,
                {"flows": frac_json(flow_sum), "share": frac_json(value)},
            )
            for flow in share.get("flows", []):
                path = tuple(flow["path"])
                ok = bool(path) and path[0] in ticket.get("direct_classes", [])
                check(f"{tid}->{cabinet}: path starts at direct class", ok, list(path))
                if edge_set is not None and path:
                    for a, b in zip(path, path[1:]):
                        if (a, b) not in edge_set:
                            check(
                                f"{tid}->{cabinet}: edge {a}->{b} exists", False, list(path)
                            )
                            ok = False
                    for mid in path[:-1]:
                        if cabinet_set is not None and mid in cabinet_set:
                            check(
                                f"{tid}->{cabinet}: no pass-through cabinet",
                                False,
                                {"node": mid, "path": list(path)},
                            )
                            ok = False
                    if cabinet == public:
                        tail_ok = not parents.get(path[-1], ())
                        if cabinet_set is not None and path[-1] in cabinet_set:
                            tail_ok = False
                        check(
                            f"{tid}->{cabinet}: public path ends at a root",
                            tail_ok,
                            list(path),
                        )
                        ok = ok and tail_ok
                    else:
                        tail_ok = path[-1] == cabinet
                        if cabinet_set is not None:
                            tail_ok = tail_ok and cabinet in cabinet_set
                        check(
                            f"{tid}->{cabinet}: path ends at the credited cabinet",
                            tail_ok,
                            list(path),
                        )
                        ok = ok and tail_ok
        check(
            f"{tid}: shares sum to amount",
            share_sum == amount,
            {"shares": frac_json(share_sum), "amount": amount},
        )

    overall = all(item["ok"] for item in checks)
    return {"ok": overall, "checks": checks, "format": "split-verification/1"}
