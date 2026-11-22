"""运行中心的周度选道/选单规划与周内重送核对。

输入：带编号、优先分的订单（每张固定来源修订、实际绑定、目标修订与随单目标核对
字段），至多 16 条可购通道与固定预算。通道开通费按不同通道只付一次，不按订单重复。

选择通道和经过它们的完整订单，使可执行订单总分最高；同分取费用最低者。

实现（限额内可精确穷举，不做启发式近似）：
- 通道按 channel_id 排序后各占一个二进制位，通道子集即一个位掩码，共 2^K 个，
  K <= 16，即至多 65536 个子集。
- 每张订单先用 ChannelCatalog 枚举全部成功路径（已含随单核对字段过滤），每条路径
  对应一个“最小通道掩码”；订单可由子集 S 执行 ⇔ 存在最小掩码 m ⊆ S。
- 费用做子集 DP：cost[S] = cost[S 去掉最低位] + fee[最低位通道]，一次开通处处复用。
- 可执行分用最小掩码的超集标记累加（只在可达掩码处加分，与订单数线性相关），
  再按掩码升序扫描预算内子集，取总分最高、同分费用最低、再同则掩码最小者，
  选择结果跨进程可复现。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .bindings import decode_value, encode_json_value
from .channels import ChannelCatalog
from .errors import PlanningError

_MAX_CHANNELS = 16


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _canonical_fingerprint(payload: dict) -> str:
    """订单实质内容的稳定指纹：同形重送（编号相同、内容相同）必然一致。

    规划输入订单与台账结果条目都含这四个实质字段，绑定/核对值均为 JSON 表示。
    """
    material = {
        "source_revision": payload["source_revision"],
        "bindings": payload.get("bindings", payload.get("source_bindings")),
        "target_revision": payload["target_revision"],
        "checks": payload.get("checks", {}),
    }
    return json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class OrderEvaluation:
    """一张订单在通道图上的试换评估（规划中间产物）。"""

    order: dict
    paths: tuple[dict, ...]
    minimal_masks: frozenset[int]
    infeasible_reason: str | None


class WeeklyPlanner:
    def __init__(self, catalog: ChannelCatalog):
        self.catalog = catalog
        self.channel_index = {c.channel_id: i for i, c in enumerate(catalog.channels)}

    def _mask_for_path(self, path: dict) -> int:
        mask = 0
        for channel_id in path["hops"]:
            mask |= 1 << self.channel_index[channel_id]
        return mask

    def evaluate_order(self, order: dict) -> OrderEvaluation:
        """对一张订单枚举全部可用完整路径并归纳最小通道掩码；不连接订单库。"""
        for field_name in ("order_id", "source_revision", "target_revision",
                           "priority", "bindings"):
            if field_name not in order:
                raise PlanningError(f"order missing field {field_name!r}")
        if not isinstance(order["order_id"], str) or not order["order_id"]:
            raise PlanningError("order_id must be a non-empty string")
        priority = order["priority"]
        if type(priority) is not int or isinstance(priority, bool) or priority < 0:
            raise PlanningError("priority must be a non-negative integer")
        checks = order.get("checks") or {}
        if not isinstance(checks, dict):
            raise PlanningError("checks must be an object of target field/value pairs")
        # 解码一次：非法值在规划阶段就拒绝，不进入后续子集评分。
        for holder, label in ((order["bindings"], "bindings"), (checks, "checks")):
            if not isinstance(holder, dict):
                raise PlanningError(f"{label} must be an object")
            for name, value in holder.items():
                try:
                    decode_value(value)
                except ValueError as exc:
                    raise PlanningError(
                        f"order {order['order_id']!r} {label}[{name!r}]: {exc}"
                    ) from exc

        paths = tuple(self.catalog.enumerate_paths(
            order["source_revision"], dict(order["bindings"]),
            order["target_revision"], checks,
        ))
        if not paths:
            # 区分“图上根本无路/核对不过”与“有路但本周没买”，便于回执解释。
            unfiltered = self.catalog.enumerate_paths(
                order["source_revision"], dict(order["bindings"]),
                order["target_revision"], None,
            )
            if unfiltered:
                reason = "no path ends with the declared target check fields/values"
            else:
                reason = (
                    f"no usable channel path from r{order['source_revision']} to "
                    f"r{order['target_revision']} for this order's actual values"
                )
            return OrderEvaluation(order, (), frozenset(), reason)

        masks = frozenset(self._mask_for_path(path) for path in paths)
        return OrderEvaluation(order, paths, masks, None)

    def plan_week(self, week_id: str, budget: int, orders: list[dict]) -> dict:
        if not isinstance(week_id, str) or not week_id:
            raise PlanningError("week_id must be a non-empty string")
        if type(budget) is not int or isinstance(budget, bool) or budget < 0:
            raise PlanningError("budget must be a non-negative integer")
        if not isinstance(orders, list) or not orders:
            raise PlanningError("at least one order is required")
        ids = [order["order_id"] for order in orders if isinstance(order, dict)]
        if len(ids) != len(orders) or len(set(ids)) != len(ids):
            raise PlanningError("order_id must be present and unique")

        evaluations = [self.evaluate_order(order) for order in orders]
        channels = self.catalog.channels
        size = 1 << len(channels)

        # 费用子集 DP：每条通道只付一次，子集费用等于各成员费用之和。
        fees = [channel.fee for channel in channels]
        costs = [0] * size
        for mask in range(1, size):
            low = mask & -mask
            costs[mask] = costs[mask ^ low] + fees[low.bit_length() - 1]

        # 可执行总分：订单可由子集 S 执行 ⇔ 存在最小掩码 m ⊆ S。
        # 先按最小掩码集合把订单合并为同形组（同形千单只算一次闭包，权重为组内
        # 优先分之和）；再对每组用大整数位运算做超集 zeta 闭包：一个 size 位整数的
        # 第 S 位表示子集 S，逐通道把“含该通道的超集”并进闭包，pattern_i 只放行
        # 第 i 位为 0 的位置以避免移位进位污染。全部为 C 层整数运算。
        groups: dict[frozenset, int] = {}
        for evaluation in evaluations:
            key = evaluation.minimal_masks
            groups[key] = groups.get(key, 0) + evaluation.order["priority"]

        channel_count = len(channels)
        position_mask = (1 << size) - 1
        zero_bit_patterns = []
        for i in range(channel_count):
            step = 1 << i
            ones = (1 << step) - 1
            pattern = ones
            while pattern.bit_length() < size:
                pattern |= pattern << (2 * step)
            zero_bit_patterns.append(pattern & position_mask)

        scores = [0] * size
        word_bits = 30
        word_mask = (1 << word_bits) - 1
        for minimal_masks, weight in groups.items():
            if not minimal_masks:
                continue
            closure = 0
            for minimal in minimal_masks:
                closure |= 1 << minimal
            for i in range(channel_count):
                step = 1 << i
                closure |= (closure & zero_bit_patterns[i]) << step
            closure &= position_mask

            chunk_index = 0
            while closure:
                word = closure & word_mask
                closure >>= word_bits
                base = chunk_index << word_bits
                bit = 0
                while word:
                    index = base + bit
                    if index < size and (word & 1):
                        scores[index] += weight
                    word >>= 1
                    bit += 1
                chunk_index += 1

        # 掩码升序扫描：总分最高 → 费用最低 → 掩码最小，结果确定可复现。
        best_mask = 0
        best_score = -1
        best_cost = 0
        for mask in range(size):
            if costs[mask] > budget:
                continue
            if scores[mask] > best_score or (
                scores[mask] == best_score and costs[mask] < best_cost
            ):
                best_mask, best_score, best_cost = mask, scores[mask], costs[mask]

        purchased = [channels[i] for i in range(len(channels)) if best_mask & (1 << i)]
        order_results = []
        for evaluation in evaluations:
            order_results.append(
                self._order_result(evaluation, best_mask)
            )

        return {
            "format": "sql-param-weekly-plan/v1",
            "week_id": week_id,
            "template_id": self.catalog.template_id,
            "budget": budget,
            "channels": [channel.to_dict() for channel in channels],
            "purchased_channel_ids": [c.channel_id for c in purchased],
            "channel_cost": best_cost,
            "total_score": max(best_score, 0),
            "considered_subsets": size,
            "orders": order_results,
        }

    def _order_result(self, evaluation: OrderEvaluation, purchased_mask: int) -> dict:
        order = evaluation.order
        base = {
            "order_id": order["order_id"],
            "source_revision": order["source_revision"],
            "target_revision": order["target_revision"],
            "priority": order["priority"],
            "checks": order.get("checks", {}),
            "source_bindings": order["bindings"],
        }
        if evaluation.infeasible_reason is not None:
            return {**base, "status": "rejected", "reason": evaluation.infeasible_reason}

        chosen_path = None
        for path in evaluation.paths:  # enumerate_paths 已按跳数、通道 id 排序
            if self._mask_for_path(path) & ~purchased_mask == 0:
                chosen_path = path
                break
        if chosen_path is None:
            return {
                **base,
                "status": "rejected",
                "reason": "a feasible path exists but none of its channels was purchased "
                          "this week",
            }
        return {
            **base,
            "status": "accepted",
            "route": {
                "hops": chosen_path["hops"],
                "hop_results": chosen_path["hop_results"],
                "final_bindings": chosen_path["final_bindings"],
            },
        }

    def build_executor_items(self, plan: dict) -> list[dict]:
        """把周计划里中选订单翻译成现有 SQLite 批次的条目（固定目标修订+实际绑定）。"""
        items = []
        for result in plan["orders"]:
            if result["status"] != "accepted":
                continue
            items.append({
                "template_id": plan["template_id"],
                "revision": result["target_revision"],
                "bindings": result["route"]["final_bindings"],
            })
        return items


class WeeklyLedger:
    """周内旧订单重送核对台账（纯 JSON 文件，原子写）。

    记录每周规划时每张编号订单的实质内容指纹与裁决；重送同编号订单时可核原结果，
    内容被改动会明确报 changed，而不是悄悄按新单重算。
    """

    def __init__(self, ledger_path: str | os.PathLike):
        self.ledger_path = Path(ledger_path)
        if self.ledger_path.exists():
            self.data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        else:
            self.data = {"weeks": {}}

    def record_week(self, plan: dict) -> None:
        week_id = plan["week_id"]
        if week_id in self.data["weeks"]:
            raise PlanningError(f"week {week_id!r} is already recorded; weekly plans are immutable")
        entries = {}
        for result in plan["orders"]:
            entries[result["order_id"]] = {
                "fingerprint": _canonical_fingerprint(result),
                "status": result["status"],
                "reason": result.get("reason"),
                "route": result.get("route"),
                "target_revision": result["target_revision"],
            }
        self.data["weeks"][week_id] = {
            "template_id": plan["template_id"],
            "purchased_channel_ids": plan["purchased_channel_ids"],
            "channel_cost": plan["channel_cost"],
            "total_score": plan["total_score"],
            "orders": entries,
        }
        _atomic_write_json(self.ledger_path, self.data)

    def verify_resend(self, week_id: str, order: dict) -> dict:
        """核对一张重送订单：是否本周见过、内容指纹是否一致、原裁决是什么。"""
        week = self.data["weeks"].get(week_id)
        if week is None:
            return {"known_week": False, "known_order": False, "same_payload": False}
        stored = week["orders"].get(order["order_id"])
        if stored is None:
            return {"known_week": True, "known_order": False, "same_payload": False}
        same = stored["fingerprint"] == _canonical_fingerprint(order)
        return {
            "known_week": True,
            "known_order": True,
            "same_payload": same,
            "original_status": stored["status"],
            "original_reason": stored.get("reason"),
            "original_route": stored.get("route"),
        }
