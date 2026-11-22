"""授信席：在全部可行终局下都能兑现的最大价值签单。

授信席持有自己已经签出的领货承诺（持久化账簿 sql-param-ledger/v1）和一批新
申请。每张申请规定要几件、可从哪些货位提、整单价值，允许跨所列货位拆领。
签后必须保证：在每一个可行终局中，都存在全体已签和新接订单同时兑现的分配；
实际拣货可在终局确定后再选（每个终局各自跑最大流）。

- :meth:`CreditDesk.evaluate` 给出价值最大的可签新单集合（同分取申请号字典序
  最小者，保证确定性）；各自单验成功不等于能同时签，联合可行性按全集判定。
- :meth:`CreditDesk.sign` 正式签发并占用额度；同一申请不能签两次；下一位授信员
  重开同一账簿即可见仍生效的旧承诺。
- :meth:`CreditDesk.allocate` 在指定终局取出逐单逐货位的配给。
- :meth:`CreditDesk.gap_witness` 在申请不能加入时给出供量缺口见证（Hall 型：
  某终局下一组订单的需求量超过其可用货位的总供给）。

每轮至多 12 张申请、6 个货位。没有可行终局（回单被隔离）时拒绝建立授信席。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import CreditError
from .reconcile import RECONCILE_FORMAT

LEDGER_FORMAT = "sql-param-ledger/v1"
MAX_APPLICATIONS_PER_ROUND = 12
MAX_BINS = 6
_INF = 1 << 62


# ---- 最大流（Dinic，图极小：源→订单→货位→汇） ------------------------------
class _FlowNetwork:
    def __init__(self) -> None:
        self.adj: dict[Any, list[list]] = {}

    def add_edge(self, u: Any, v: Any, cap: int) -> list:
        forward = [v, cap, 0, None]
        backward = [u, 0, 0, None]
        forward[3] = backward
        backward[3] = forward
        self.adj.setdefault(u, []).append(forward)
        self.adj.setdefault(v, []).append(backward)
        return forward

    def _push(self, u: Any, sink: Any, level: dict, next_edge: dict, amount: int) -> int:
        if u == sink:
            return amount
        edges = self.adj.get(u, [])
        while next_edge[u] < len(edges):
            edge = edges[next_edge[u]]
            v, cap, flow, backward = edge
            if cap - flow > 0 and level.get(v) == level[u] + 1:
                pushed = self._push(v, sink, level, next_edge, min(amount, cap - flow))
                if pushed:
                    edge[2] += pushed
                    backward[2] -= pushed
                    return pushed
            next_edge[u] += 1
        return 0

    def maxflow(self, source: Any, sink: Any) -> int:
        total = 0
        while True:
            level = {source: 0}
            queue = [source]
            for u in queue:
                for v, cap, flow, _ in self.adj.get(u, []):
                    if cap - flow > 0 and v not in level:
                        level[v] = level[u] + 1
                        queue.append(v)
            if sink not in level:
                return total
            next_edge = dict.fromkeys(level, 0)
            while True:
                pushed = self._push(source, sink, level, next_edge, _INF)
                if not pushed:
                    break
                total += pushed

    def reachable_from(self, source: Any) -> set:
        seen = {source}
        queue = [source]
        for u in queue:
            for v, cap, flow, _ in self.adj.get(u, []):
                if cap - flow > 0 and v not in seen:
                    seen.add(v)
                    queue.append(v)
        return seen


def _build_network(orders: list[tuple], capacities: dict[str, int]):
    """orders: [(order_id, quantity, bins)]；返回 (网络, 订单→货位边, 总需求)。"""
    network = _FlowNetwork()
    total = 0
    order_edges = []
    for index, (_order_id, quantity, bins) in enumerate(orders):
        network.add_edge("source", ("order", index), quantity)
        edges = {b: network.add_edge(("order", index), ("bin", b), quantity) for b in bins}
        order_edges.append(edges)
        total += quantity
    for name, capacity in capacities.items():
        network.add_edge(("bin", name), "sink", capacity)
    return network, order_edges, total


def _fulfillable(orders: list[tuple], capacities: dict[str, int]) -> bool:
    network, _, total = _build_network(orders, capacities)
    return network.maxflow("source", "sink") == total


# ---- 从合账报告提取各终局货位可用量 ---------------------------------------
def capacities_from_report(
    report: dict,
    *,
    bins: dict[str, int],
    quantity_column: str,
    pk_column: str | None = None,
) -> list[dict]:
    """把 sql-param-reconcile/v1 报告转成授信席用的终局可用量列表。

    ``bins`` 为 {货位名: 主键值}；回单被隔离（无可行终局）时拒绝转换。
    """
    if not isinstance(report, dict) or report.get("format") != RECONCILE_FORMAT:
        raise CreditError("expected a sql-param-reconcile/v1 report")
    if report.get("status") != "explained" or not report.get("terminals"):
        raise CreditError("receipts are quarantined; no trustworthy terminal state")
    if not isinstance(bins, dict) or not bins:
        raise CreditError("bins must be a non-empty object mapping names to primary keys")
    if len(bins) > MAX_BINS:
        raise CreditError(f"bin limit exceeded ({MAX_BINS})")
    for name, pk_value in bins.items():
        if not isinstance(name, str) or not name:
            raise CreditError("bin names must be non-empty strings")
        if not isinstance(pk_value, int) or isinstance(pk_value, bool):
            raise CreditError(f"bin {name!r}: primary key must be an integer")
    pk = pk_column or report["pk_column"]

    terminals = []
    for terminal in report["terminals"]:
        columns, rows = terminal["columns"], terminal["rows"]
        if pk not in columns or quantity_column not in columns:
            raise CreditError(f"terminal {terminal['terminal_id']}: missing required columns")
        pk_i, qty_i = columns.index(pk), columns.index(quantity_column)
        capacities = {}
        for name, pk_value in bins.items():
            row = next((r for r in rows if r[pk_i] == pk_value), None)
            if row is None:
                raise CreditError(
                    f"bin {name!r} (pk={pk_value}) missing in terminal {terminal['terminal_id']}"
                )
            quantity = row[qty_i]
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
                raise CreditError(
                    f"bin {name!r} in terminal {terminal['terminal_id']}: "
                    "quantity must be a non-negative integer"
                )
            capacities[name] = quantity
        terminals.append({"terminal_id": terminal["terminal_id"], "capacities": capacities})
    return terminals


# ---- 授信席 ---------------------------------------------------------------
class CreditDesk:
    """一台授信席：账簿 + 一组可行终局；签发占用额度，账簿持久化。"""

    def __init__(
        self,
        ledger_path: str | os.PathLike,
        terminals: list[dict],
        *,
        max_applications: int = MAX_APPLICATIONS_PER_ROUND,
        max_bins: int = MAX_BINS,
    ):
        if not isinstance(terminals, list) or not terminals:
            raise CreditError(
                "no feasible terminal state; nothing can be signed (quarantined receipts?)"
            )
        self.terminals = []
        bin_names: frozenset | None = None
        for terminal in terminals:
            if not isinstance(terminal, dict):
                raise CreditError("each terminal must be an object")
            terminal_id = terminal.get("terminal_id")
            capacities = terminal.get("capacities")
            if not isinstance(terminal_id, str) or not terminal_id:
                raise CreditError("each terminal must name its terminal_id")
            if not isinstance(capacities, dict) or not capacities:
                raise CreditError("terminal capacities must be a non-empty object")
            for name, quantity in capacities.items():
                if not isinstance(name, str) or not name:
                    raise CreditError("bin names must be non-empty strings")
                if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
                    raise CreditError(f"bin {name!r}: quantity must be a non-negative integer")
            if bin_names is None:
                bin_names = frozenset(capacities)
            elif frozenset(capacities) != bin_names:
                raise CreditError("all terminals must cover the same bins")
            self.terminals.append(
                {"terminal_id": terminal_id, "capacities": dict(capacities)}
            )
        if len(bin_names) > max_bins:
            raise CreditError(f"bin limit exceeded ({max_bins})")
        self.bins = sorted(bin_names)
        self.max_applications = max_applications
        self.ledger_path = Path(ledger_path)
        self._signed = self._load_ledger()
        # 仍生效的旧承诺必须在每一个可行终局都可兑现，否则账簿与实物库存矛盾。
        if not self._feasible(self._orders()):
            raise CreditError(
                "existing commitments cannot be fulfilled in every feasible terminal"
            )

    # ---- 账簿 -------------------------------------------------------------
    def _load_ledger(self) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        with self.ledger_path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict) or payload.get("format") != LEDGER_FORMAT:
            raise CreditError("unsupported ledger format")
        signed = payload.get("signed")
        if not isinstance(signed, list):
            raise CreditError("ledger must contain a signed list")
        seen: set[str] = set()
        for entry in signed:
            self._check_order_entry(entry)
            if entry["order_id"] in seen:
                raise CreditError(f"duplicate order_id in ledger: {entry['order_id']!r}")
            seen.add(entry["order_id"])
        return [dict(entry) for entry in signed]

    def _check_order_entry(self, entry: Any) -> None:
        if not isinstance(entry, dict):
            raise CreditError("ledger entries must be objects")
        if not isinstance(entry.get("order_id"), str) or not entry["order_id"]:
            raise CreditError("ledger entry must name a non-empty order_id")
        if entry.get("application_id") is not None and not isinstance(entry["application_id"], str):
            raise CreditError("application_id must be a string or null")
        quantity = entry.get("quantity")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
            raise CreditError("ledger entry quantity must be a positive integer")
        bins = entry.get("bins")
        if (
            not isinstance(bins, list)
            or not bins
            or len(set(bins)) != len(bins)
            or any(b not in self.bins for b in bins)
        ):
            raise CreditError(f"ledger entry bins must be a non-empty subset of {self.bins}")
        value = entry.get("value")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise CreditError("ledger entry value must be a non-negative integer")

    def _persist(self) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"format": LEDGER_FORMAT, "signed": self._signed}
        fd, tmp_name = tempfile.mkstemp(
            prefix=self.ledger_path.name + ".", suffix=".tmp", dir=self.ledger_path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.ledger_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    def signed(self) -> list[dict]:
        """当前账簿上仍生效的全部承诺（含历史承诺与已签申请）。"""
        return [dict(entry) for entry in self._signed]

    # ---- 可行性 -----------------------------------------------------------
    def _orders(self) -> list[tuple]:
        return [(e["order_id"], e["quantity"], list(e["bins"])) for e in self._signed]

    def _feasible(self, orders: list[tuple]) -> bool:
        return all(
            _fulfillable(orders, terminal["capacities"]) for terminal in self.terminals
        )

    # ---- 申请 -------------------------------------------------------------
    def _validate_applications(self, applications: Any) -> list[dict]:
        if not isinstance(applications, list):
            raise CreditError("applications must be a list")
        if len(applications) > self.max_applications:
            raise CreditError(f"application limit exceeded ({self.max_applications})")
        known_bins = set(self.bins)
        signed_ids = {e["order_id"] for e in self._signed} | {
            e["application_id"] for e in self._signed if e.get("application_id") is not None
        }
        seen: set[str] = set()
        normalized = []
        for application in applications:
            if not isinstance(application, dict):
                raise CreditError("each application must be an object")
            application_id = application.get("application_id")
            if not isinstance(application_id, str) or not application_id:
                raise CreditError("application_id must be a non-empty string")
            if application_id in seen:
                raise CreditError(f"duplicate application_id {application_id!r}")
            seen.add(application_id)
            if application_id in signed_ids:
                raise CreditError(
                    f"application {application_id!r} is already signed; "
                    "the same application cannot be signed twice"
                )
            quantity = application.get("quantity")
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
                raise CreditError(f"application {application_id!r}: quantity must be a positive integer")
            bins = application.get("bins")
            if (
                not isinstance(bins, list)
                or not bins
                or len(set(bins)) != len(bins)
                or any(b not in known_bins for b in bins)
            ):
                raise CreditError(
                    f"application {application_id!r}: bins must be a non-empty subset of {self.bins}"
                )
            value = application.get("value")
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise CreditError(f"application {application_id!r}: value must be a non-negative integer")
            normalized.append(
                {
                    "application_id": application_id,
                    "quantity": quantity,
                    "bins": list(bins),
                    "value": value,
                }
            )
        return normalized

    def evaluate(self, applications: list[dict]) -> dict:
        """给出价值最大的可签新单集合；同分取申请号字典序最小者。"""
        apps = self._validate_applications(applications)
        base = self._orders()
        n = len(apps)
        feasible_sets: list[tuple[int, tuple]] = []
        solo: dict[str, bool] = {}
        for mask in range(1 << n):
            chosen = [apps[i] for i in range(n) if mask & (1 << i)]
            extra = [(a["application_id"], a["quantity"], a["bins"]) for a in chosen]
            if not self._feasible(base + extra):
                continue
            if len(chosen) == 1:
                solo[chosen[0]["application_id"]] = True
            value = sum(a["value"] for a in chosen)
            ids = tuple(sorted(a["application_id"] for a in chosen))
            feasible_sets.append((value, ids))
        for app in apps:
            solo.setdefault(app["application_id"], False)
        best_value = max(value for value, _ in feasible_sets)
        best_ids = min(ids for value, ids in feasible_sets if value == best_value)
        return {
            "best": {"application_ids": list(best_ids), "total_value": best_value},
            "solo_signable": solo,
        }

    def sign(self, applications: list[dict], application_ids: list[str] | None = None) -> list[dict]:
        """正式签发：默认签 evaluate 的最佳集合；签发占用额度并落账。"""
        apps = self._validate_applications(applications)
        by_id = {a["application_id"]: a for a in apps}
        if application_ids is None:
            chosen_ids = self.evaluate(applications)["best"]["application_ids"]
        else:
            chosen_ids = list(application_ids)
            unknown = [i for i in chosen_ids if i not in by_id]
            if unknown:
                raise CreditError(f"unknown application_ids: {unknown}")
            extra = [
                (a["application_id"], a["quantity"], a["bins"])
                for a in (by_id[i] for i in chosen_ids)
            ]
            if not self._feasible(self._orders() + extra):
                raise CreditError(
                    "chosen applications cannot be fulfilled jointly in every feasible terminal"
                )
        new_entries = [
            {
                "order_id": application_id,
                "application_id": application_id,
                "quantity": by_id[application_id]["quantity"],
                "bins": list(by_id[application_id]["bins"]),
                "value": by_id[application_id]["value"],
            }
            for application_id in chosen_ids
        ]
        self._signed.extend(new_entries)
        self._persist()
        return [dict(entry) for entry in new_entries]

    def register_commitment(
        self, order_id: str, quantity: int, bins: list[str], value: int = 0
    ) -> dict:
        """登记一条仍生效的旧承诺（占用额度）；须在每个终局可兑现才允许入账。"""
        entry = {
            "order_id": order_id,
            "application_id": None,
            "quantity": quantity,
            "bins": list(bins) if isinstance(bins, list) else bins,
            "value": value,
        }
        self._check_order_entry(entry)
        if order_id in {e["order_id"] for e in self._signed}:
            raise CreditError(f"duplicate order_id {order_id!r}")
        if not self._feasible(self._orders() + [(order_id, quantity, entry["bins"])]):
            raise CreditError("commitment cannot be fulfilled in every feasible terminal")
        self._signed.append(entry)
        self._persist()
        return dict(entry)

    # ---- 配给与见证 -------------------------------------------------------
    def allocate(self, terminal_id: str) -> dict:
        """在指定终局取出逐单逐货位的配给（该终局确定后的实际拣货方案）。"""
        terminal = next(
            (t for t in self.terminals if t["terminal_id"] == terminal_id), None
        )
        if terminal is None:
            raise CreditError(f"unknown terminal_id {terminal_id!r}")
        orders = self._orders()
        network, order_edges, total = _build_network(orders, terminal["capacities"])
        if network.maxflow("source", "sink") != total:
            raise CreditError("signed orders cannot be fulfilled in this terminal")
        allocation = {}
        for (order_id, _quantity, _bins), edges in zip(orders, order_edges):
            allocation[order_id] = {b: edge[2] for b, edge in edges.items() if edge[2] > 0}
        return {
            "terminal_id": terminal_id,
            "capacities": dict(terminal["capacities"]),
            "orders": allocation,
            "total_demand": total,
        }

    def gap_witness(self, application: dict) -> dict | None:
        """申请不能加入时给出供量缺口见证；可以加入时返回 None。"""
        (app,) = self._validate_applications([application])
        orders = self._orders() + [(app["application_id"], app["quantity"], app["bins"])]
        for terminal in self.terminals:
            network, _, total = _build_network(orders, terminal["capacities"])
            if network.maxflow("source", "sink") == total:
                continue
            reachable = network.reachable_from("source")
            deficient = [
                orders[i]
                for i in range(len(orders))
                if ("order", i) in reachable
            ]
            demand = sum(quantity for _, quantity, _ in deficient)
            allowed = sorted({b for _, _, bins in deficient for b in bins})
            available = sum(terminal["capacities"][b] for b in allowed)
            return {
                "terminal_id": terminal["terminal_id"],
                "deficient_orders": [order_id for order_id, _, _ in deficient],
                "demand": demand,
                "bins": allowed,
                "available": available,
                "deficit": demand - available,
            }
        return None
