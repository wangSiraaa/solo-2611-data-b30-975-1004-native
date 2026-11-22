"""Agenda search: the cheapest legal knockout schedule that leaves a proposal.

The committee holds the proposals ``A, B, C, ...``.  Repeatedly it picks a
pair of *surviving* proposals, votes on it, and the loser leaves; the winner
stays.  A proposal is formally tabled (sent to the session) when only it
remains.  No proposal may be added and no round may be skipped.

The pair table supplies two things per pair:

* ``winner`` - the side that wins the vote when both proposals survive
  (``None`` means the vote is a tie that only the chair order can resolve);
* ``cost`` - the staging fee, or ``None`` if the venue forbids the pair.

The chair order is *only* a tie-break for a vote whose sides are equal.  It
never ranks the whole field, and it only ever picks between the two surviving
proposals, so a schedule therefore never reads a global vote ranking out of
it.

For a fixed target ``T`` this is exact: every state is the set of survived
non-``T`` proposals, each step removes exactly one of them, and no proposal is
ever added.  The implementation is a forward subset DP over a bitmask of the
eliminated non-``T`` proposals (one bit each, at most 17 bits for 18
proposals), so it visits each state once instead of exploring every ordering.
Costs are non-negative, so minimising over the DAG of states is exact.
"""

from __future__ import annotations

from typing import Mapping, Sequence


class AgendaError(Exception):
    """A schedule request that cannot be honoured (always explicit)."""


def _resolve(
    pair: Mapping,
    *,
    order_index: Mapping[str, int] | None,
) -> str | None:
    """Winner of a pair when both survive, or ``None`` if unresolvable."""

    winner = pair.get("winner")
    if winner is not None:
        return winner
    if order_index is not None:
        left, right = pair["left"], pair["right"]
        lo = order_index.get(left)
        ro = order_index.get(right)
        if lo is not None and ro is not None and lo != ro:
            return left if lo < ro else right
    return None


def cheapest_agenda(
    target: str,
    proposals: Sequence[str],
    pairs: Mapping[tuple[str, str], Mapping],
    *,
    order: Sequence[str] | None = None,
) -> dict:
    """Return the minimum-fee legal agenda that leaves ``target`` tabled.

    ``pairs`` must contain one entry for every unordered pair of proposals,
    keyed by ``(min, max)``; each entry has ``left``, ``right``, ``winner``
    (distance-vote winner or ``None`` for a tie), ``cost`` (non-negative int or
    ``None`` if forbidden) and optional vote detail.  ``order`` is the selected
    chair order; it is consulted only for a tied pair.
    """

    proposal_list = list(dict.fromkeys(str(p) for p in proposals))
    if target not in proposal_list:
        raise AgendaError(f"target {target!r} is not one of the proposals")
    n = len(proposal_list)
    if n < 2:
        return {
            "target": target,
            "proposals": proposal_list,
            "feasible": True,
            "total_cost": 0,
            "rounds": [],
            "survivors_after": [target],
            "reason": "single proposal; nothing to vote on",
        }

    order_index = (
        {node: i for i, node in enumerate(order)} if order is not None else None
    )

    # Resolve the winner of each pair, or None if a tie cannot be separated.
    win: dict[tuple[str, str], str] = {}
    forbidden: set[tuple[str, str]] = set()
    for key, pair in pairs.items():
        left, right = pair["left"], pair["right"]
        if {left, right} != set(key):
            raise AgendaError(f"pair table key {key!r} does not match its sides")
        if pair.get("cost") is None:
            forbidden.add(key)
            continue
        resolved = _resolve(pair, order_index=order_index)
        if resolved is not None:
            win[key] = resolved

    # Bit per non-target proposal.
    bit_of: dict[str, int] = {}
    for proposal in proposal_list:
        if proposal != target:
            bit_of[proposal] = 1 << len(bit_of)
    full = (1 << len(bit_of)) - 1

    # Pre-resolve usable transitions.  ``need_zero`` is the pair's survivor
    # requirement expressed as "these eliminated bits must still be 0".
    transitions: list[tuple[int, int, int, Mapping, str, str]] = []
    for i in range(n):
        a = proposal_list[i]
        for j in range(i + 1, n):
            b = proposal_list[j]
            key = (a, b) if a < b else (b, a)
            if key in forbidden or key not in win:
                continue
            winner = win[key]
            loser = b if winner == a else a
            if loser == target:
                continue
            need_zero = bit_of[loser]
            if winner != target:
                need_zero |= bit_of[winner]
            transitions.append(
                (need_zero, bit_of[loser], int(pairs[key]["cost"]), key, winner, loser)
            )

    # Deterministic relaxation order.
    transitions.sort(key=lambda t: (t[3], t[4]))
    if not transitions:
        return _infeasible_result(target, proposal_list, pairs, forbidden, win)

    infinity = float("inf")
    dp = [infinity] * (full + 1)
    dp[0] = 0
    parent: list[tuple[int, int] | None] = [None] * (full + 1)

    for mask in range(full + 1):
        base = dp[mask]
        if base == infinity:
            continue
        for index, (need_zero, loser_bit, cost, _key, _winner, _loser) in enumerate(
            transitions
        ):
            if mask & need_zero:
                continue
            nxt = mask | loser_bit
            candidate = base + cost
            if candidate < dp[nxt]:
                dp[nxt] = candidate
                parent[nxt] = (mask, index)

    if dp[full] == infinity:
        return _infeasible_result(target, proposal_list, pairs, forbidden, win)

    # Reconstruct rounds from the full mask back to zero.
    rounds_rev: list[dict] = []
    mask = full
    while mask:
        prev, index = parent[mask]  # type: ignore[misc]
        need_zero, loser_bit, cost, key, winner, loser = transitions[index]
        rounds_rev.append(
            {
                "left": pairs[key]["left"],
                "right": pairs[key]["right"],
                "winner": winner,
                "loser": loser,
                "cost": cost,
            }
        )
        mask = prev
    rounds = list(reversed(rounds_rev))

    survivors = set(proposal_list)
    for rnd in rounds:
        survivors.discard(rnd["loser"])
        rnd["survivors_after"] = sorted(survivors)

    return {
        "target": target,
        "proposals": proposal_list,
        "feasible": True,
        "total_cost": dp[full],
        "rounds": rounds,
        "survivors_after": [target],
        "reason": "cheapest legal schedule",
    }


def _infeasible_result(
    target: str,
    proposals: Sequence[str],
    pairs: Mapping[tuple[str, str], Mapping],
    forbidden: set[tuple[str, str]],
    win: Mapping[tuple[str, str], str],
) -> dict:
    return {
        "target": target,
        "proposals": list(proposals),
        "feasible": False,
        "total_cost": None,
        "rounds": [],
        "survivors_after": [],
        "reason": _infeasible_reason(target, proposals, pairs, forbidden, win),
    }


def _infeasible_reason(
    target: str,
    proposals: Sequence[str],
    pairs: Mapping[tuple[str, str], Mapping],
    forbidden: set[tuple[str, str]],
    win: Mapping[tuple[str, str], str],
) -> str:
    """A short, *sound* explanation of why no schedule seats ``target``.

    The DP already decided infeasibility; this only names a necessary
    condition that failed, never an unproven "the cycle is bad" claim.
    """

    missing: list[tuple[str, str]] = []
    tied: list[tuple[str, str]] = []
    usable: list[tuple[str, str, str]] = []
    for other in proposals:
        if other == target:
            continue
        key = (target, other) if target < other else (other, target)
        if key not in pairs:
            missing.append(key)
            continue
        if key in forbidden:
            continue
        if key not in win:
            tied.append(key)
            continue
        usable.append((key[0], key[1], win[key]))

    if missing:
        return f"no pair table entry for ({missing[0][0]}, {missing[0][1]})"
    if not usable and tied:
        key = tied[0]
        return (
            f"pair ({key[0]}, {key[1]}) is tied and the chair order does "
            "not separate it"
        )
    if not usable:
        return f"every legal pair including {target} is forbidden by the venue"
    if all(winner != target for _a, _b, winner in usable):
        return (
            f"{target} loses every pair it can enter, so it can never be "
            "the last survivor"
        )
    return (
        f"no legal elimination schedule leaves {target}; every ordering "
        "eliminates it or leaves another proposal"
    )
