"""Clearing seat: settle every node's net amount over directed remittance lines.

Each directed line has an integer batch capacity and a one-time activation
fee. Transshipment is allowed (a node may relay funds), and activation fees
are paid out of a separate fee account, so they never enter the node balances.

Finding the minimum total activation fee that supports a full settlement is a
fixed-charge network problem. With at most 14 lines the search space is small
enough for exhaustive enumeration:

1. enumerate line subsets by total fee (ties by mask order);
2. test each subset with one max-flow run on the lower-bound circulation
   network;
3. for the cheapest feasible subset, pin each line's amount one at a time to
   the smallest feasible integer, giving one deterministic, replay-stable
   instruction per line.

If no subset settles every node, no half-batch instructions are produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .errors import ValidationError
from .rules import require_int, require_text

MAX_NODES = 8
MAX_LINES = 14


@dataclass(frozen=True)
class Route:
    route_id: str
    source: str
    target: str
    capacity: int
    activation_fee: int


@dataclass(frozen=True)
class Instruction:
    route_id: str
    source: str
    target: str
    amount: int
    activation_fee: int


@dataclass(frozen=True)
class SettlementResult:
    feasible: bool
    total_activation_fee: int
    instructions: tuple[Instruction, ...]
    reason: Optional[str]


class _Dinic:
    """Integer max flow on a small directed graph (stdlib only)."""

    def __init__(self, n: int):
        self.n = n
        self.graph: list[list[dict]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: int) -> dict:
        forward = {"to": v, "cap": cap, "rev": len(self.graph[v])}
        backward = {"to": u, "cap": 0, "rev": len(self.graph[u])}
        self.graph[u].append(forward)
        self.graph[v].append(backward)
        return forward

    def add_bounded_edge(
        self, u: int, v: int, lower: int, upper: int
    ) -> tuple[dict, dict]:
        """Residual pair for an edge carrying flow in ``[lower, upper]``.

        The forward residual starts at ``upper - lower`` and the reverse
        residual at ``lower`` (the mandatory flow that may be cancelled).
        """
        if upper < lower:
            raise ValueError("lower bound exceeds upper bound")
        forward = {"to": v, "cap": upper - lower, "rev": len(self.graph[v])}
        backward = {"to": u, "cap": lower, "rev": len(self.graph[u])}
        self.graph[u].append(forward)
        self.graph[v].append(backward)
        return forward, backward

    def max_flow(self, source: int, sink: int, limit: int) -> int:
        flow = 0
        while flow < limit:
            level = self._bfs(source, sink)
            if level is None:
                break
            pointer = [0] * self.n
            while flow < limit:
                pushed = self._dfs(source, sink, limit - flow, level, pointer)
                if pushed == 0:
                    break
                flow += pushed
        return flow

    def _bfs(self, source: int, sink: int) -> Optional[list[int]]:
        level = [-1] * self.n
        level[source] = 0
        queue = [source]
        head = 0
        while head < len(queue):
            u = queue[head]
            head += 1
            for edge in self.graph[u]:
                if edge["cap"] > 0 and level[edge["to"]] < 0:
                    level[edge["to"]] = level[u] + 1
                    queue.append(edge["to"])
        return level if level[sink] >= 0 else None

    def _dfs(
        self, u: int, sink: int, pushed: int, level: list[int], pointer: list[int]
    ) -> int:
        if u == sink:
            return pushed
        while pointer[u] < len(self.graph[u]):
            edge = self.graph[u][pointer[u]]
            if edge["cap"] > 0 and level[edge["to"]] == level[u] + 1:
                amount = self._dfs(
                    edge["to"], sink, min(pushed, edge["cap"]), level, pointer
                )
                if amount:
                    edge["cap"] -= amount
                    self.graph[edge["to"]][edge["rev"]]["cap"] += amount
                    return amount
            pointer[u] += 1
        return 0


def settle(
    balances: dict[str, int], routes: Sequence[Route]
) -> SettlementResult:
    """Find minimum-fee full settlement and the exact per-line amounts."""
    _validate(balances, routes)
    nodes = sorted(balances)
    index = {node: i for i, node in enumerate(nodes)}
    demands = {node: int(balances[node]) for node in nodes}
    total_demand = sum(v for v in demands.values() if v > 0)
    if total_demand == 0:
        return SettlementResult(True, 0, tuple(), None)
    routes = list(routes)
    m = len(routes)

    def feasible(fixed: dict[int, int]) -> Optional[dict[int, int]]:
        return _lower_bound_feasible(nodes, index, demands, routes, fixed)

    # Subsets in (total fee asc, mask asc); mask order makes ties deterministic.
    candidates = sorted(
        range(1, 1 << m),
        key=lambda mask: (
            sum(routes[i].activation_fee for i in range(m) if mask & (1 << i)),
            mask,
        ),
    )
    best_mask: Optional[int] = None
    best_fee: Optional[int] = None
    for mask in candidates:
        fee = sum(routes[i].activation_fee for i in range(m) if mask & (1 << i))
        if best_fee is not None and fee > best_fee:
            break
        fixed = {i: 0 for i in range(m) if not (mask & (1 << i))}
        if feasible(fixed) is not None:
            best_mask, best_fee = mask, fee
            break

    if best_mask is None:
        return SettlementResult(
            False,
            0,
            tuple(),
            "no route subset can clear every node within its batch capacities",
        )

    # Pin each included line to the smallest feasible amount in route order.
    # Every entry is a cumulative lower bound, so earlier pins stay mandatory.
    fixed = {i: 0 for i in range(m) if not (best_mask & (1 << i))}
    final_flow: Optional[dict[int, int]] = None
    for i in range(m):
        if not (best_mask & (1 << i)):
            continue
        lo, hi = 0, routes[i].capacity
        while lo < hi:
            mid = (lo + hi) // 2
            trial = dict(fixed)
            trial[i] = mid
            if feasible(trial) is not None:
                hi = mid
            else:
                lo = mid + 1
        fixed[i] = lo
    final_flow = feasible(fixed)
    if final_flow is None:
        raise RuntimeError("internal error: pinned flow became infeasible")

    instructions = tuple(
        Instruction(
            route_id=routes[i].route_id,
            source=routes[i].source,
            target=routes[i].target,
            amount=final_flow[i],
            activation_fee=routes[i].activation_fee,
        )
        for i in range(m)
        if final_flow[i] > 0
    )
    _assert_cleared(nodes, demands, routes, final_flow)
    return SettlementResult(True, best_fee, instructions, None)


def _lower_bound_feasible(
    nodes: list[str],
    index: dict[str, int],
    demands: dict[str, int],
    routes: Sequence[Route],
    fixed: dict[int, int],
) -> Optional[dict[int, int]]:
    """Feasibility of a full settlement, with some routes pinned.

    The node demands are plain super edges in the residual graph:

    * a payer (``demand < 0``) gets ``SS -> node`` of ``-demand``;
    * a receiver (``demand > 0``) gets ``node -> TT`` of ``demand``;
    * a free ``TT -> SS`` edge closes them into a circulation.

    An unpinned payment line is an ordinary capacity edge. A line pinned to
    exactly ``L`` is an exact edge (a residual pair with zero forward
    capacity and reverse capacity ``L``) whose mandatory flow is booked into
    a balance array ``d``. Auxiliary edges discharge the balances:
    ``d[v] > 0`` gets ``SS' -> v`` and ``d[u] < 0`` gets ``u -> TT'``. The
    check runs in two phases on the same residual graph:

    1. the auxiliary max flow must saturate every outgoing auxiliary edge,
       proving the pinned mandatory flows can be rerouted consistently;
    2. a max flow ``SS -> TT`` must move the full payable volume, proving the
       node demands themselves can be met over the remaining capacities.
    """
    n = len(nodes)
    super_source = n
    super_sink = n + 1
    aux_source = n + 2
    aux_sink = n + 3
    dinic = _Dinic(n + 4)
    balance = [0] * n  # mandatory net inflow booked by pinned lines
    # per route: (lower, forward residual edge, initial forward capacity)
    route_edges: list[tuple[int, dict, int]] = []

    for i, route in enumerate(routes):
        u = index[route.source]
        v = index[route.target]
        if i in fixed:
            lower = fixed[i]
            if lower > route.capacity:
                return None
            # Exact pin: no forward residual, so the line cannot carry more
            # than the pinned amount; its reverse residual (== lower) still
            # lets the auxiliary flow reroute the mandatory funds.
            forward, _ = dinic.add_bounded_edge(u, v, lower, lower)
            balance[u] -= lower
            balance[v] += lower
            route_edges.append((lower, forward, 0))
        else:
            forward = dinic.add_edge(u, v, route.capacity)
            route_edges.append((0, forward, route.capacity))

    total_demand = 0
    for node in nodes:
        i = index[node]
        demand = demands[node]
        if demand > 0:
            dinic.add_edge(i, super_sink, demand)
            total_demand += demand
        elif demand < 0:
            dinic.add_edge(super_source, i, -demand)
    dinic.add_edge(super_sink, super_source, total_demand + 1)

    # First discharge any lower-bound balances through the auxiliary
    # source/sink, then prove the node demands themselves can be satisfied:
    # a max flow SS -> TT must move the full payable volume.
    need = 0
    for i, residual in enumerate(balance):
        if residual > 0:
            need += residual
            dinic.add_edge(aux_source, i, residual)
        elif residual < 0:
            dinic.add_edge(i, aux_sink, -residual)
    if need > 0 and dinic.max_flow(aux_source, aux_sink, need) != need:
        return None
    if dinic.max_flow(super_source, super_sink, total_demand) != total_demand:
        return None

    flows: dict[int, int] = {}
    for i, (lower, forward, initial) in enumerate(route_edges):
        extra = initial - forward["cap"]
        flow = lower + extra
        if flow < lower or flow > routes[i].capacity:
            # A well-formed reduction never produces an out-of-range line.
            return None
        flows[i] = flow
    return flows




def _assert_cleared(
    nodes: list[str],
    demands: dict[str, int],
    routes: Sequence[Route],
    fixed: dict[int, int],
) -> None:
    net = {node: 0 for node in nodes}
    for i, route in enumerate(routes):
        amount = fixed.get(i, 0)
        if amount < 0 or amount > route.capacity:
            raise RuntimeError("internal error: instruction outside batch capacity")
        net[route.source] -= amount
        net[route.target] += amount
    for node in nodes:
        if net[node] != demands[node]:
            raise RuntimeError(
                f"internal error: node {node!r} net {net[node]} != demand "
                f"{demands[node]}"
            )


def _validate(balances: dict[str, int], routes: Sequence[Route]) -> None:
    if not balances:
        raise ValidationError("settlement needs at least one node")
    if len(balances) > MAX_NODES:
        raise ValidationError(
            f"at most {MAX_NODES} clearing nodes, got {len(balances)}"
        )
    if sum(balances.values()) != 0:
        raise ValidationError("counter net amounts must sum to zero")
    seen_routes: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    for route in routes:
        if not isinstance(route, Route):
            raise ValidationError("routes must be Route objects")
        require_text(route.route_id, "route_id")
        require_text(route.source, "source")
        require_text(route.target, "target")
        require_int(route.capacity, "capacity", minimum=1)
        require_int(route.activation_fee, "activation_fee", minimum=0)
        if route.route_id in seen_routes:
            raise ValidationError(f"duplicate route id {route.route_id!r}")
        seen_routes.add(route.route_id)
        pair = (route.source, route.target)
        if pair in seen_pairs:
            raise ValidationError(f"duplicate directed line {pair}")
        seen_pairs.add(pair)
        for node in pair:
            if node not in balances:
                raise ValidationError(f"route {route.route_id!r}: unknown node {node!r}")
    if len(routes) > MAX_LINES:
        raise ValidationError(f"at most {MAX_LINES} routes, got {len(routes)}")
