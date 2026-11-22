"""Pure matching engine: assign at most one voucher to each true-usage claim.

A voucher may offset at most one claim and a claim is offset by at most one
voucher. The optimizer has two lexicographic objectives:

1. minimize the cash the supplier receives this period, i.e. maximize the
   total offset ``sum(min(list_price, voucher_cap))``;
2. among all optimal assignments, take the lexicographically smallest voucher
   sequence when claims are sorted by ``(reservation_id, seq)``; an unused
   claim sorts after every real voucher id.

Both objectives are encoded in one cost cell so a single rectangular
Hungarian run finds the optimum. The secondary key is the base-``B``
encoding of the voucher rank vector; ``B`` is strictly larger than every rank,
so no carry can trade one earlier position against later ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

UNUSED = ""  # sorts after every non-empty voucher id in output ordering


@dataclass(frozen=True)
class Claim:
    """One true-usage credential, identified per portion of a reservation."""

    reservation_id: str
    seq: int
    counter_id: str
    bottle_id: str
    settle_day: int
    list_price: int

    @property
    def key(self) -> tuple[str, int]:
        return (self.reservation_id, self.seq)


@dataclass(frozen=True)
class Voucher:
    voucher_id: str
    owner_counter: str
    paid_price: int
    cap_amount: int
    valid_from_day: int
    valid_until_day: int
    bottles: frozenset[str]
    counters: frozenset[str]

    def usable_for(self, claim: Claim) -> bool:
        """Day window is half-open ``[from, until)``; sets must cover the claim."""
        if not (self.valid_from_day <= claim.settle_day < self.valid_until_day):
            return False
        if claim.bottle_id not in self.bottles:
            return False
        if claim.counter_id not in self.counters:
            return False
        return True

    def offset_for(self, claim: Claim) -> int:
        return min(claim.list_price, self.cap_amount)


def hungarian(cost: list[list[int]]) -> list[int]:
    """Minimum-cost assignment of every row to a distinct column.

    Rectangular variant (rows <= columns), O(n^2 * m), all entries finite.
    Returns ``col_for_row[i]``: the column assigned to row ``i``.
    """
    n = len(cost)
    if n == 0:
        return []
    m = len(cost[0])
    # 1-indexed potentials and mates; row 0 / column 0 are sentinels.
    u = [0] * (n + 1)
    v = [0] * (m + 1)
    p = [0] * (m + 1)  # p[j]: row assigned to column j (0 = none)
    way = [0] * (m + 1)
    INF = None
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (m + 1)
        used = [False] * (m + 1)
        while p[j0] != 0:
            used[j0] = True
            i0 = p[j0]
            delta = None
            j1 = 0
            row = cost[i0 - 1]
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = row[j - 1] - u[i0] - v[j]
                if minv[j] is None or cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if delta is None or minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(0, m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    col_for_row = [0] * n
    for j in range(1, m + 1):
        if p[j] != 0:
            col_for_row[p[j] - 1] = j - 1
    return col_for_row


def optimize_redemptions(
    claims: Sequence[Claim], vouchers: Sequence[Voucher]
) -> list[Optional[str]]:
    """Return the voucher id per claim (``None`` = unused), rule-optimal.

    Claims are sorted by ``(reservation_id, seq)`` exactly as the tie rule
    requires; voucher columns are sorted by id so the rank order matches the
    lexicographic order, with one extra dummy column per claim meaning
    "no voucher" and sorting after all real voucher ids.
    """
    ordered = sorted(claims, key=lambda c: c.key)
    n = len(ordered)
    if n == 0:
        return []
    sorted_vouchers = sorted(vouchers, key=lambda x: x.voucher_id)
    k = len(sorted_vouchers)
    dummies = n  # one "unused" column per claim, so columns cover all rows
    # Ranks 0..k-1 are real vouchers; every dummy encodes the sentinel rank k
    # (sorts after every real id). The base only needs to exceed that rank.
    base = k + 1
    weight = base ** n  # strictly above every lexicographic secondary key
    # Pre-computed place value of each sorted claim position.
    place = [base ** (n - 1 - position) for position in range(n)]

    def cell(row: Claim, position: int, rank: int) -> int:
        if rank >= k:  # dummy column: no offset, sentinel lexicographic rank
            return weight * row.list_price + place[position] * k
        voucher = sorted_vouchers[rank]
        lex = place[position] * rank
        if not voucher.usable_for(row):
            # Inapplicable pairing: force it above every feasible cost.
            return weight * row.list_price + lex + weight
        offset = voucher.offset_for(row)
        cash = row.list_price - offset
        return weight * cash + lex

    cost = [
        [cell(claim, position, rank) for rank in range(k + dummies)]
        for position, claim in enumerate(ordered)
    ]
    chosen = hungarian(cost)
    result: list[Optional[str]] = []
    for i, col in enumerate(chosen):
        if col >= k:
            result.append(None)
        else:
            voucher = sorted_vouchers[col]
            # The big cost must never have selected an inapplicable cell.
            if not voucher.usable_for(ordered[i]):
                raise RuntimeError("internal error: matched an inapplicable voucher")
            result.append(voucher.voucher_id)
    return result
