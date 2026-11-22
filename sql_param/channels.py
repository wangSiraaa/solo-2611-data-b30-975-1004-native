"""转换通道与模板室逐跳试换。

业务背景：柜台仍按旧修订的命名参数送双栏（名字/值）订单，模板室需要把一张实际旧单
逐站（逐跳）换成目标修订的绑定。每条有向通道固定两端已发布计划，只通往更高修订。

通道合同（见题面）：
- 每条通道为每个**目标参数**指明一个或多个**来源名**；多源时旧单中各值必须
  同类型且相等（typed equality），否则该通道对该单不可用。
- 一个来源名可供多个目标；通道引用到的所有来源名必须都被使用，且每个来源名都必须
  实际存在于来源修订的计划参数里（按实际计划核名，注释里的假参数不算）。
- 目标名必须恰为目标修订计划的全部不同参数名，不多不少；重复参数名只占一个绑定键，
  执行时由执行器按出现顺序展开。
- 字符串、字节、整数、空值按旧绑定合同区分：str 与 bytes 即使内容相同也不相等，
  bool 不是合法值（在解码阶段已拒绝）。
- 同一通道对甲单可用、对乙单可能不可用：可用性取决于该单的实际值。

本模块不连接订单库、不连接 SQLite，只读取 PlanStore 上的已发布计划做名字核对与
纯值转换；模板室独立提供逐跳试换，能取回每一步的值与失败处。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .bindings import decode_value, encode_json_value
from .errors import ChannelError
from .plan_store import PlanStore

MAX_REVISIONS = 12
_CHANNEL_ID = re.compile(r"[A-Za-z0-9_.:-]+\Z")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")


def typed_equal(left, right) -> bool:
    """按旧绑定合同判等：类型与值都相同才算相等。

    None/int/str/bytes 四类互不混同；特别地 str 与同内容 bytes 不相等，
    int 也不与可解析为数字的 str 相等。
    """
    if type(left) is not type(right):
        return False
    return left == right


def _ensure_decoded(value):
    """接受 JSON 风格值或逐跳传播中已经解码过的值（None/int/str/bytes）。

    试换是逐跳链式的：上一跳输出的是 Python 原值（含 bytes），下一跳不能再按
    JSON 表示解码；bool 在任何形态下都拒绝（decode_value 已拦截 JSON 入口）。
    """
    if value is None or isinstance(value, (str, bytes)):
        return value
    return decode_value(value)


@dataclass(frozen=True)
class Channel:
    """一条固定两端已发布计划的有向转换通道。

    mapping 为 ``{目标参数名: (来源参数名, ...)}``；值用元组是为了可哈希、
    顺序稳定（多源相等性与顺序无关，但回执按声明顺序展示）。
    """

    channel_id: str
    source_revision: int
    target_revision: int
    fee: int
    mapping: dict[str, tuple[str, ...]]

    @classmethod
    def from_dict(cls, payload: dict) -> "Channel":
        if not isinstance(payload, dict):
            raise ChannelError("channel must be an object")
        try:
            channel_id = payload["channel_id"]
            source_revision = payload["source_revision"]
            target_revision = payload["target_revision"]
            fee = payload["fee"]
            raw_mapping = payload["mapping"]
        except KeyError as exc:
            raise ChannelError(f"channel missing field {exc.args[0]!r}") from exc
        if not isinstance(channel_id, str) or _CHANNEL_ID.fullmatch(channel_id) is None:
            raise ChannelError("channel_id must be a non-empty safe string")
        for label, revision in (("source_revision", source_revision),
                                ("target_revision", target_revision)):
            if type(revision) is not int or isinstance(revision, bool) or revision < 1:
                raise ChannelError(f"{label} must be a positive integer")
        if type(fee) is not int or isinstance(fee, bool) or fee < 0:
            raise ChannelError("fee must be a non-negative integer")
        if not isinstance(raw_mapping, dict) or not raw_mapping:
            raise ChannelError("mapping must be a non-empty object")
        mapping: dict[str, tuple[str, ...]] = {}
        for target, sources in raw_mapping.items():
            if not isinstance(target, str) or _NAME.fullmatch(target) is None:
                raise ChannelError(f"invalid target parameter name: {target!r}")
            if isinstance(sources, str):
                sources = [sources]
            if not isinstance(sources, (list, tuple)) or not sources:
                raise ChannelError(f"target {target!r} needs one or more source names")
            names = tuple(sources)
            if any(not isinstance(name, str) or _NAME.fullmatch(name) is None
                   for name in names):
                raise ChannelError(f"target {target!r} has an invalid source name")
            if len(set(names)) != len(names):
                raise ChannelError(f"target {target!r} lists a duplicate source name")
            mapping[target] = names
        return cls(channel_id, source_revision, target_revision, fee, mapping)

    def to_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "source_revision": self.source_revision,
            "target_revision": self.target_revision,
            "fee": self.fee,
            "mapping": {target: list(sources) for target, sources in self.mapping.items()},
        }


@dataclass(frozen=True)
class HopResult:
    """逐跳试换中一跳的结果；失败时 error 给出失败处，out_bindings 为 None。"""

    channel_id: str
    source_revision: int
    target_revision: int
    ok: bool
    out_bindings: dict | None
    error: str | None = None
    # 每个目标参数实际取到的值（JSON 表示），成功时随结果带回，便于逐站核对。
    resolved: dict = field(default_factory=dict)


class ChannelCatalog:
    """已按实际计划校验过的通道集合（一个模板室一张目录）。

    构造时逐条对照 PlanStore 中两端已发布计划核名；限额：至多 16 条可购通道、
    涉及至多 12 个修订。只通往更高修订，因此通道图天然是 DAG。
    """

    def __init__(self, store: PlanStore, template_id: str, channels: list[Channel]):
        if not isinstance(channels, list) or not channels:
            raise ChannelError("at least one channel is required")
        if len(channels) > 16:
            raise ChannelError("at most 16 purchasable channels are supported")
        ids = [channel.channel_id for channel in channels]
        if len(set(ids)) != len(ids):
            raise ChannelError("channel_id must be unique")

        self.store = store
        self.template_id = template_id
        self.channels: list[Channel] = []
        revisions: set[int] = set()
        for channel in channels:
            self._validate_against_plans(channel)
            revisions.update((channel.source_revision, channel.target_revision))
            self.channels.append(channel)
        if len(revisions) > MAX_REVISIONS:
            raise ChannelError(f"at most {MAX_REVISIONS} revisions are supported")

        # 稳定顺序：通道按 channel_id 排序后编号位，保证穷举与回执跨进程可复现。
        self.channels.sort(key=lambda c: c.channel_id)
        self.by_id: dict[str, Channel] = {c.channel_id: c for c in self.channels}
        self._out: dict[int, list[Channel]] = {}
        for channel in self.channels:
            self._out.setdefault(channel.source_revision, []).append(channel)
        for outgoing in self._out.values():
            outgoing.sort(key=lambda c: (c.target_revision, c.channel_id))

    def _validate_against_plans(self, channel: Channel) -> None:
        if channel.source_revision >= channel.target_revision:
            raise ChannelError(
                f"channel {channel.channel_id!r}: only moves to a higher revision are allowed"
            )
        source_plan = self.store.load(self.template_id, channel.source_revision)
        target_plan = self.store.load(self.template_id, channel.target_revision)
        source_names = set(self.store.distinct_names(source_plan))
        target_names = set(self.store.distinct_names(target_plan))

        if set(channel.mapping) != target_names:
            missing = sorted(target_names - set(channel.mapping))
            extra = sorted(set(channel.mapping) - target_names)
            details = []
            if missing:
                details.append(f"unmapped target params: {', '.join(missing)}")
            if extra:
                details.append(f"unknown target params: {', '.join(extra)}")
            raise ChannelError(
                f"channel {channel.channel_id!r} vs r{channel.target_revision} plan: "
                + "; ".join(details)
            )
        used_sources = {name for names in channel.mapping.values() for name in names}
        unknown = sorted(used_sources - source_names)
        if unknown:
            raise ChannelError(
                f"channel {channel.channel_id!r} references source names absent from "
                f"r{channel.source_revision} plan: {', '.join(unknown)}"
            )
        # “所有来源名必须被使用”：来源计划里的每个不同参数名都至少被一个目标引用。
        unused = sorted(source_names - used_sources)
        if unused:
            raise ChannelError(
                f"channel {channel.channel_id!r} leaves source params unused: "
                f"{', '.join(unused)}"
            )

    # ---- 模板室：逐跳试换（纯函数，不连库） --------------------------------
    def try_hop(self, channel: Channel, bindings: dict) -> HopResult:
        """对一张实际订单的当前绑定试走一条通道，取回目标绑定或带失败处的结果。

        绑定按旧绑定合同解码（null/int64/str/bytes，bool 等拒绝）；多源值必须
        同类型且相等；输出绑定键恰为目标修订的不同参数名。
        """
        if not isinstance(bindings, dict):
            raise ChannelError("bindings must be an object")
        source_plan = self.store.load(self.template_id, channel.source_revision)
        expected = set(self.store.distinct_names(source_plan))
        if set(bindings) != expected:
            missing = sorted(expected - set(bindings))
            extra = sorted(set(bindings) - expected)
            details = []
            if missing:
                details.append(f"missing keys: {', '.join(missing)}")
            if extra:
                details.append(f"extra keys: {', '.join(extra)}")
            return HopResult(channel.channel_id, channel.source_revision,
                             channel.target_revision, False, None,
                             "binding keys do not match source plan: " + "; ".join(details))
        try:
            decoded = {name: _ensure_decoded(bindings[name]) for name in expected}
        except ValueError as exc:
            return HopResult(channel.channel_id, channel.source_revision,
                             channel.target_revision, False, None,
                             f"invalid binding value: {exc}")

        out: dict = {}
        resolved: dict = {}
        for target in sorted(channel.mapping):
            sources = channel.mapping[target]
            values = [decoded[name] for name in sources]
            first = values[0]
            for name, value in zip(sources[1:], values[1:]):
                if not typed_equal(first, value):
                    return HopResult(
                        channel.channel_id, channel.source_revision,
                        channel.target_revision, False, None,
                        f"target {target!r}: sources {sources} disagree "
                        f"({type(first).__name__} {first!r} vs "
                        f"{type(value).__name__} {value!r})",
                    )
            out[target] = first
            resolved[target] = encode_json_value(first)
        return HopResult(channel.channel_id, channel.source_revision,
                         channel.target_revision, True, out, None, resolved)

    def enumerate_paths(self, source_revision: int, bindings: dict,
                        target_revision: int, checks: dict | None = None) -> list[dict]:
        """枚举从来源修订到目标修订全部试换成功的完整路径。

        返回列表按（跳数, 通道 id 序列）排序，元素含 hops（通道 id）、每跳解析值与
        最终绑定。checks 非空时，仅保留最终绑定与随单声明逐字段 typed-equal 的路径
        （柜台只允许交付随单声明的目标核对字段和值）。不连接订单库。
        """
        if type(source_revision) is not int or source_revision < 1:
            raise ChannelError("source_revision must be a positive integer")
        if type(target_revision) is not int or target_revision < 1:
            raise ChannelError("target_revision must be a positive integer")
        results: list[dict] = []

        def visit(revision: int, current: dict, hop_results: list[HopResult]) -> None:
            if revision == target_revision:
                if self._passes_checks(current, checks):
                    results.append({
                        "source_revision": source_revision,
                        "target_revision": target_revision,
                        "hops": [h.channel_id for h in hop_results],
                        "hop_results": [self._hop_to_dict(h) for h in hop_results],
                        "final_bindings": {k: encode_json_value(v) for k, v in
                                           sorted(current.items())},
                    })
                return
            for channel in self._out.get(revision, ()):
                # 只通往更高修订；越过目标修订的边不可能再绕回来（DAG），直接剪枝。
                if channel.target_revision > target_revision:
                    continue
                hop = self.try_hop(channel, current)
                if hop.ok:
                    visit(channel.target_revision, hop.out_bindings,
                          hop_results + [hop])

        if source_revision == target_revision:
            # 零跳：订单本来就是目标修订；仍须通过随单核对字段。
            if self._passes_checks(bindings, checks):
                results.append({
                    "source_revision": source_revision,
                    "target_revision": target_revision,
                    "hops": [],
                    "hop_results": [],
                    "final_bindings": {k: encode_json_value(v) for k, v in
                                       sorted(bindings.items())},
                })
            return results

        visit(source_revision, bindings, [])
        results.sort(key=lambda p: (len(p["hops"]), p["hops"]))
        return results

    @staticmethod
    def _passes_checks(bindings: dict, checks: dict | None) -> bool:
        if not checks:
            return True
        try:
            decoded_checks = {k: _ensure_decoded(v) for k, v in checks.items()}
        except ValueError:
            return False
        for name, expected in decoded_checks.items():
            if name not in bindings or not typed_equal(bindings[name], expected):
                return False
        return True

    @staticmethod
    def _hop_to_dict(hop: HopResult) -> dict:
        return {
            "channel_id": hop.channel_id,
            "from_revision": hop.source_revision,
            "to_revision": hop.target_revision,
            "ok": hop.ok,
            "resolved": hop.resolved,
            "error": hop.error,
        }
