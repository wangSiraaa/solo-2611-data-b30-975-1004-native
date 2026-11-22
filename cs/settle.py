"""Integer settlement of cabinet shares (whole-cent payments).

Input is the exact rational split produced by :mod:`cs.split` at one cut.
For every ``(ticket, cabinet)`` cell the payment is restricted to the floor or
the ceiling of its exact share; an already-paid cell is fixed to the integer it
was paid.  Every ticket's payments must sum to its amount and every cabinet's
payments must sum to the teller's integer target.  Among all such settlements
we minimise the total fee of cells that are rounded **up**.

The cell fees are per ``(ticket, cabinet)`` pair (the worked example has two
free cells and two cells that cost 3 each), so this is a minimum-cost
exact-degree bipartite selection.  It is solved as a min-cost flow over
non-negative integer costs.  Nothing is ever adjusted on a ticket and no cent
is dropped: if no settlement exists the solver returns
``status == "infeasible"`` with diagnostics and no payments.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable, Mapping

from .split import frac_from, frac_json

STATUS_SETTLED = "settled"
STATUS_INFEASIBLE = "infeasible"


def floor_frac(value: Fraction) -> int:
    return value.numerator // value.denominator


def ceil_frac(value: Fraction) -> int:
    return -((-value.numerator) // value.denominator)


@dataclass
class _Cell:
    ticket_id: str
    cabinet: str
    share: Fraction
    low: int
    high: int
    fee: int
    locked: int | None = None

    @property
    def fractional(self) -> bool:
        return self.high > self.low

    @property
    def dd(self) -> int:
        """How much a ceiling choice adds over the floor (0 or 1)."""
        return self.high - self.low

    @property
    def locked_ceil(self) -> bool:
        return self.locked is not None and self.fractional and self.locked == self.high


class _MinCostFlow:
    """Successive shortest augmenting paths with Johnson potentials."""

    def __init__(self, size: int) -> None:
        self.size = size
        # Each edge: [to, reverse_index, residual_capacity, cost]
        self.graph: list[list[list[int]]] = [[] for _ in range(size)]

    def add_edge(self, u: int, v: int, cap: int, cost: int) -> None:
        self.graph[u].append([v, len(self.graph[v]), cap, cost])
        self.graph[v].append([u, len(self.graph[u]) - 1, 0, -cost])

    def send(self, source: int, sink: int, need: int) -> tuple[int, int]:
        import heapq

        n = self.size
        graph = self.graph
        potential = [0] * n
        flow = 0
        cost = 0
        inf = float("inf")
        while flow < need:
            dist = [inf] * n
            dist[source] = 0
            prev_node = [-1] * n
            prev_edge = [-1] * n
            heap = [(0, source)]
            while heap:
                d, u = heapq.heappop(heap)
                if d > dist[u]:
                    continue
                for index, edge in enumerate(graph[u]):
                    v, _, cap, edge_cost = edge
                    if cap <= 0:
                        continue
                    nd = d + edge_cost + potential[u] - potential[v]
                    if nd < dist[v]:
                        dist[v] = nd
                        prev_node[v] = u
                        prev_edge[v] = index
                        heapq.heappush(heap, (nd, v))
            if dist[sink] == inf:
                break
            for v in range(n):
                if dist[v] < inf:
                    potential[v] += dist[v]
            push = need - flow
            v = sink
            path_cost = 0
            while v != source:
                u = prev_node[v]
                edge = graph[u][prev_edge[v]]
                push = min(push, edge[2])
                path_cost += edge[3]
                v = u
            v = sink
            while v != source:
                u = prev_node[v]
                index = prev_edge[v]
                edge = graph[u][index]
                edge[2] -= push
                graph[v][edge[1]][2] += push
                v = u
            flow += push
            cost += push * path_cost
        return flow, cost


def _coerce_shares(
    shares: Mapping[str, Mapping[str, Any]]
) -> dict[tuple[str, str], Fraction]:
    cells: dict[tuple[str, str], Fraction] = {}
    for ticket_id, per_cabinet in shares.items():
        for cabinet, value in per_cabinet.items():
            share = value if isinstance(value, Fraction) else Fraction(value)
            if share < 0:
                raise ValueError(f"{ticket_id}->{cabinet}: negative share")
            cells[(str(ticket_id), str(cabinet))] = share
    return cells


def _key(ticket_id: str, cabinet: str) -> str:
    return f"{ticket_id}|{cabinet}"


def solve_settlement(
    shares: Mapping[str, Mapping[str, Any]],
    amounts: Mapping[str, int],
    targets: Mapping[str, int],
    *,
    locked: Mapping[tuple[str, str], int] | None = None,
    fees: Mapping[tuple[str, str], int] | None = None,
    cut: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a ``settlement/1`` report for one cut."""

    locked = {(str(t), str(c)): int(v) for (t, c), v in dict(locked or {}).items()}
    fees = {(str(t), str(c)): int(v) for (t, c), v in dict(fees or {}).items()}
    amounts = {str(k): int(v) for k, v in amounts.items()}
    targets = {str(k): int(v) for k, v in targets.items()}
    share_cells = _coerce_shares(shares)

    reasons: list[str] = []
    for ticket_id, amount in amounts.items():
        if amount < 0:
            reasons.append(f"ticket {ticket_id}: amount is negative")
    for cabinet, target in targets.items():
        if target < 0:
            reasons.append(f"cabinet {cabinet}: target is negative")
    for (ticket_id, cabinet), fee in fees.items():
        if fee < 0:
            reasons.append(f"{ticket_id}->{cabinet}: negative upward fee")
    for ticket_id, cabinet in locked:
        if locked[(ticket_id, cabinet)] < 0:
            reasons.append(f"{ticket_id}->{cabinet}: negative already-paid amount")
        share_cells.setdefault((ticket_id, cabinet), Fraction(0))

    ticket_ids = sorted({t for t, _ in share_cells} | set(amounts))
    cabinets = sorted({c for _, c in share_cells} | set(targets))

    # Exact shares must add up to each ticket's amount and to the target total.
    row_share_sum: dict[str, Fraction] = {t: Fraction(0) for t in ticket_ids}
    for (ticket_id, _), share in share_cells.items():
        row_share_sum[ticket_id] += share
    for ticket_id in ticket_ids:
        expected = amounts.get(ticket_id)
        if expected is None:
            reasons.append(f"ticket {ticket_id}: no amount supplied")
        elif row_share_sum[ticket_id] != expected:
            reasons.append(
                f"ticket {ticket_id}: shares total {row_share_sum[ticket_id]} "
                f"but amount is {expected}"
            )
    for cabinet in sorted({c for _, c in share_cells} - set(targets)):
        reasons.append(f"cabinet {cabinet}: share exists but no integer target")

    total_amount = sum(amounts.get(t, 0) for t in ticket_ids)
    total_target = sum(targets.get(c, 0) for c in cabinets)
    if total_amount != total_target:
        reasons.append(
            f"ticket amounts total {total_amount} but cabinet targets total {total_target}"
        )

    # Build one cell per (ticket, cabinet) share.
    cell_list: list[_Cell] = []
    for ticket_id, cabinet in sorted(share_cells):
        share = share_cells[(ticket_id, cabinet)]
        low, high = floor_frac(share), ceil_frac(share)
        cell = _Cell(
            ticket_id=ticket_id,
            cabinet=cabinet,
            share=share,
            low=low,
            high=high,
            fee=int(fees.get((ticket_id, cabinet), 0)),
            locked=locked.get((ticket_id, cabinet)),
        )
        if cell.locked is not None and cell.locked not in (low, high):
            reasons.append(
                f"{ticket_id}->{cabinet}: already paid {cell.locked}, "
                f"but the share {share} rounds only to {low} or {high}"
            )
        cell_list.append(cell)

    by_row: dict[str, list[_Cell]] = {}
    by_col: dict[str, list[_Cell]] = {}
    for cell in cell_list:
        by_row.setdefault(cell.ticket_id, []).append(cell)
        by_col.setdefault(cell.cabinet, []).append(cell)

    def _dimension(
        keys: Sequence[str],
        groups: Mapping[str, list[_Cell]],
        wanted: Mapping[str, int],
        label: str,
    ) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
        required: dict[str, int] = {}
        free_fractional: dict[str, int] = {}
        forced_ceil: dict[str, int] = {}
        for key in keys:
            cells = groups.get(key, [])
            low_sum = sum(c.low for c in cells)
            need = wanted.get(key, 0)
            required[key] = need - low_sum
            free_fractional[key] = sum(
                1 for c in cells if c.fractional and c.locked is None
            )
            forced_ceil[key] = sum(1 for c in cells if c.locked_ceil)
            if required[key] < 0:
                reasons.append(
                    f"{label} {key}: floor payments already sum to {low_sum}, "
                    f"above its total {need}"
                )
            elif required[key] - forced_ceil[key] > free_fractional[key]:
                reasons.append(
                    f"{label} {key}: needs {required[key] - forced_ceil[key]} more "
                    f"ceiling choices but only {free_fractional[key]} free "
                    f"fractional cells exist (already locked up: {forced_ceil[key]})"
                )
            if forced_ceil[key] > required[key]:
                reasons.append(
                    f"{label} {key}: {forced_ceil[key]} cells are locked to the "
                    f"ceiling but only {required[key]} upward roundings are allowed"
                )
        return required, free_fractional, forced_ceil

    row_required, row_free, row_forced = _dimension(
        ticket_ids, by_row, amounts, "ticket"
    )
    col_required, col_free, col_forced = _dimension(
        cabinets, by_col, targets, "cabinet"
    )

    report: dict[str, Any] = {
        "format": "settlement/1",
        "cut": dict(cut or {}),
        "status": STATUS_INFEASIBLE,
        "cells": [],
        "rows": [],
        "columns": [],
        "total_fee": None,
        "reasons": [],
        "inputs": {
            "amounts": {t: amounts.get(t, 0) for t in ticket_ids},
            "targets": {c: targets.get(c, 0) for c in cabinets},
            "locked": {_key(t, c): v for (t, c), v in sorted(locked.items())},
            "fees": {_key(t, c): v for (t, c), v in sorted(fees.items())},
        },
    }

    assignment: dict[tuple[str, str], int] = {}
    if not reasons:
        source = 0
        row_base = 1
        col_base = row_base + len(ticket_ids)
        sink = col_base + len(cabinets)
        network = _MinCostFlow(sink + 1)
        total_residual = 0
        for index, ticket_id in enumerate(ticket_ids):
            budget = row_required[ticket_id] - row_forced[ticket_id]
            if budget > 0:
                network.add_edge(source, row_base + index, budget, 0)
                total_residual += budget
        for index, cabinet in enumerate(cabinets):
            budget = col_required[cabinet] - col_forced[cabinet]
            if budget > 0:
                network.add_edge(col_base + index, sink, budget, 0)
        total_col_residual = sum(
            max(0, col_required[c] - col_forced[c]) for c in cabinets
        )
        if total_col_residual != total_residual:
            reasons.append(
                "row and column upward budgets are inconsistent "
                f"({total_residual} vs {total_col_residual})"
            )

        free_edges: list[tuple[int, int, int, tuple[str, str]]] = []
        for cell in cell_list:
            if cell.locked is not None or not cell.fractional:
                continue
            row_index = row_base + ticket_ids.index(cell.ticket_id)
            col_index = col_base + cabinets.index(cell.cabinet)
            token = len(network.graph[row_index])
            network.add_edge(row_index, col_index, 1, cell.fee)
            free_edges.append((row_index, col_index, token, (cell.ticket_id, cell.cabinet)))

        if not reasons:
            flow, _ = network.send(source, sink, total_residual)
            if flow != total_residual:
                reasons.append(
                    "no exact selection of upward roundings exists "
                    "(row and column targets conflict)"
                )
            else:
                for row_index, col_index, token, key in free_edges:
                    assignment[key] = 0 if network.graph[row_index][token][2] else 1

    if reasons:
        report["reasons"] = reasons
        return _fill_diagnostics(report, cell_list, row_required, row_free, col_required, col_free)

    payments: dict[tuple[str, str], int] = {}
    total_fee = 0
    for cell in cell_list:
        key = (cell.ticket_id, cell.cabinet)
        if cell.locked is not None:
            paid = cell.locked
        elif cell.fractional:
            paid = cell.high if assignment.get(key, 0) else cell.low
        else:
            paid = cell.low
        payments[key] = paid
        if cell.fractional and paid == cell.high:
            total_fee += cell.fee

    row_paid = {t: 0 for t in ticket_ids}
    col_paid = {c: 0 for c in cabinets}
    for cell in cell_list:
        paid = payments[(cell.ticket_id, cell.cabinet)]
        row_paid[cell.ticket_id] += paid
        col_paid[cell.cabinet] += paid

    for ticket_id in ticket_ids:
        if row_paid[ticket_id] != amounts.get(ticket_id, 0):
            reasons.append(
                f"ticket {ticket_id}: paid {row_paid[ticket_id]} != amount "
                f"{amounts.get(ticket_id)}"
            )
    for cabinet in cabinets:
        target = targets.get(cabinet)
        if target is not None and col_paid[cabinet] != target:
            reasons.append(
                f"cabinet {cabinet}: paid {col_paid[cabinet]} != target {target}"
            )
    if reasons:
        report["reasons"] = reasons
        return _fill_diagnostics(report, cell_list, row_required, row_free, col_required, col_free)

    report["status"] = STATUS_SETTLED
    report["total_fee"] = total_fee
    report["cells"] = [
        _serialize_cell(cell, payments[(cell.ticket_id, cell.cabinet)])
        for cell in cell_list
    ]
    report["rows"] = [
        {
            "ticket_id": ticket_id,
            "amount": amounts.get(ticket_id, 0),
            "paid": row_paid[ticket_id],
            "ok": row_paid[ticket_id] == amounts.get(ticket_id, 0),
        }
        for ticket_id in ticket_ids
    ]
    report["columns"] = [
        {
            "cabinet": cabinet,
            "target": targets.get(cabinet),
            "paid": col_paid[cabinet],
            "ok": targets.get(cabinet) is None or col_paid[cabinet] == targets.get(cabinet),
            "upward_roundings": col_required.get(cabinet, 0),
            "fee": sum(
                c.fee
                for c in by_col.get(cabinet, [])
                if c.fractional and payments[(c.ticket_id, c.cabinet)] == c.high
            ),
        }
        for cabinet in cabinets
    ]
    report["locked"] = [
        {
            "ticket_id": t,
            "cabinet": c,
            "paid": v,
            "preserved": payments.get((t, c)) == v,
        }
        for (t, c), v in sorted(locked.items())
    ]
    return report


def _serialize_cell(cell: _Cell, paid: int) -> dict[str, Any]:
    if cell.locked is not None:
        rounding = "locked"
    elif not cell.fractional:
        rounding = "exact"
    elif paid == cell.high:
        rounding = "ceil"
    else:
        rounding = "floor"
    return {
        "ticket_id": cell.ticket_id,
        "cabinet": cell.cabinet,
        "share": frac_json(cell.share),
        "floor": cell.low,
        "ceil": cell.high,
        "locked": cell.locked,
        "paid": paid,
        "rounding": rounding,
        "fee": cell.fee if cell.fractional and paid == cell.high else 0,
    }


def _fill_diagnostics(
    report: dict[str, Any],
    cells: Iterable[_Cell],
    row_required: Mapping[str, int],
    row_free: Mapping[str, int],
    col_required: Mapping[str, int],
    col_free: Mapping[str, int],
) -> dict[str, Any]:
    report["cells"] = [
        {
            "ticket_id": c.ticket_id,
            "cabinet": c.cabinet,
            "share": frac_json(c.share),
            "floor": c.low,
            "ceil": c.high,
            "locked": c.locked,
            "paid": None,
        }
        for c in cells
    ]
    report["rows"] = [
        {
            "ticket_id": t,
            "required_upward_roundings": row_required[t],
            "free_fractional_cells": row_free[t],
        }
        for t in sorted(row_required)
    ]
    report["columns"] = [
        {
            "cabinet": c,
            "required_upward_roundings": col_required.get(c, 0),
            "free_fractional_cells": col_free.get(c, 0),
        }
        for c in sorted(col_required)
    ]
    return report


def verify_settlement(
    report: Mapping[str, Any],
    *,
    shares: Mapping[str, Mapping[str, Any]] | None = None,
    amounts: Mapping[str, int] | None = None,
    targets: Mapping[str, int] | None = None,
    locked: Mapping[tuple[str, str], int] | None = None,
    fees: Mapping[tuple[str, str], int] | None = None,
    re_solve: bool = False,
) -> dict[str, Any]:
    """Independent checklist for a settlement report.

    The verifier trusts only the exact shares, amounts, targets, locked cells
    and fees (taken from the arguments when supplied, otherwise from the
    report's embedded ``inputs``).  It recomputes floors/ceilings, every row
    and column sum, preserved locked cells and the total fee.  With
    ``re_solve`` it re-runs the solver and confirms the reported fee is
    optimal.  It never uses the report's own totals as evidence.
    """

    embedded = report.get("inputs", {})
    if shares is None:
        shares = {}
        for cell in report.get("cells", []):
            shares.setdefault(cell["ticket_id"], {})[cell["cabinet"]] = frac_from(
                cell["share"]
            )
    if amounts is None:
        amounts = embedded.get("amounts", {})
    if targets is None:
        targets = embedded.get("targets", {})
    if locked is None:
        locked = {
            tuple(key.split("|", 1)): value  # type: ignore[misc]
            for key, value in embedded.get("locked", {}).items()
        }
    if fees is None:
        fees = {
            tuple(key.split("|", 1)): value  # type: ignore[misc]
            for key, value in embedded.get("fees", {}).items()
        }

    share_cells = _coerce_shares(shares)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = None) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    status = report.get("status")
    if status == STATUS_INFEASIBLE:
        expected = solve_settlement(shares, amounts, targets, locked=locked, fees=fees)
        check(
            "infeasible verdict reproduces",
            expected["status"] == STATUS_INFEASIBLE,
            expected.get("reasons", [])[:8],
        )
        return {
            "ok": all(c["ok"] for c in checks),
            "checks": checks,
            "format": "settlement-verification/1",
        }

    payments: dict[tuple[str, str], int] = {}
    for cell in report.get("cells", []):
        key = (cell["ticket_id"], cell["cabinet"])
        payments[key] = int(cell["paid"])
        share = frac_from(cell["share"])
        low, high = floor_frac(share), ceil_frac(share)
        if key in locked:
            check(
                f"{key[0]}->{key[1]}: locked value preserved",
                payments[key] == int(locked[key]),
                {"paid": payments[key], "locked": int(locked[key])},
            )
        check(
            f"{key[0]}->{key[1]}: paid is floor or ceiling",
            payments[key] in (low, high),
            {"paid": payments[key], "floor": low, "ceil": high},
        )

    for ticket_id, amount in amounts.items():
        paid = sum(v for (t, _), v in payments.items() if t == ticket_id)
        check(
            f"row {ticket_id}: paid equals amount",
            paid == int(amount),
            {"paid": paid, "amount": int(amount)},
        )
    for cabinet, target in targets.items():
        paid = sum(v for (_, c), v in payments.items() if c == cabinet)
        check(
            f"column {cabinet}: paid equals target",
            paid == int(target),
            {"paid": paid, "target": int(target)},
        )

    for key, value in locked.items():
        check(
            f"locked {key[0]}->{key[1]}: present and unchanged",
            payments.get(key) == int(value),
            {"paid": payments.get(key), "locked": int(value)},
        )

    recomputed_fee = 0
    for (ticket_id, cabinet), paid in payments.items():
        share = share_cells.get((ticket_id, cabinet), Fraction(0))
        low, high = floor_frac(share), ceil_frac(share)
        if high > low and paid == high:
            recomputed_fee += int(fees.get((ticket_id, cabinet), 0))
    check(
        "total fee matches recomputation",
        recomputed_fee == report.get("total_fee"),
        {"recomputed": recomputed_fee, "reported": report.get("total_fee")},
    )

    if re_solve:
        expected = solve_settlement(shares, amounts, targets, locked=locked, fees=fees)
        check(
            "re-solve is feasible with the same minimal fee",
            expected["status"] == STATUS_SETTLED
            and expected["total_fee"] == report.get("total_fee"),
            {
                "status": expected.get("status"),
                "fee": expected.get("total_fee"),
                "reported": report.get("total_fee"),
            },
        )

    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "format": "settlement-verification/1",
    }
