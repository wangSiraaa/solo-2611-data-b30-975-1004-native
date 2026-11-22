"""离线柜台对账条（sql-param-slip/v1）的出具与校验。

柜台在本机离线执行批次、拿到 committed 回执后，用 :func:`export_slip` 出具对账条：
每条对账条固定一个不可拆批次，逐项声明哪些 SELECT 结果列对外可见、哪些写操作的
rowcount 已盖章；其余结果一律保密，不进入对账条。

导出方从原回执逐项核下标、列位与实际类型：

- 回执必须 committed 且每项 ok；item 下标必须连续完整；
- 公开列位必须在回执结果列范围内且不重复，投影逐格复制，JSON 类型原样保留；
- 零行、一行 NULL、两行相同记录是三种不同事实，投影原样保留、绝不折叠；
- 盖章 rowcount 必须是非负整数；绑定值仍按绑定域（null/int64/str/$bytes）校验。

回单室一侧用 :func:`validate_slip` 对收到的对账条做独立结构校验，
两边校验规则一致，任何一项不符都拒绝。
"""
from __future__ import annotations

import math
import re
from typing import Any

from .bindings import decode_value
from .errors import PlanStoreError, SlipError
from .plan_store import validate_plan

SLIP_FORMAT = "sql-param-slip/v1"
_COUNTER_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_WRITE_KINDS = ("insert", "update")


def _check_counter_id(counter_id: Any) -> str:
    if not isinstance(counter_id, str) or _COUNTER_ID.fullmatch(counter_id) is None:
        raise SlipError(f"invalid counter_id: {counter_id!r}")
    return counter_id


def _check_sequence(sequence: Any) -> int:
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise SlipError("sequence must be a positive integer")
    return sequence


def check_result_value(value: Any, where: str) -> None:
    """回执/对账条结果值的合法 JSON 域：null/int/str/float/$bytes；拒绝 bool 等。"""
    if value is None or isinstance(value, str):
        return
    if isinstance(value, bool):
        raise SlipError(f"{where}: boolean is not a legal result value")
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SlipError(f"{where}: non-finite float is not allowed")
        return
    if isinstance(value, dict) and set(value) == {"$bytes"}:
        try:
            decode_value(value)
        except ValueError as exc:
            raise SlipError(f"{where}: {exc}") from exc
        return
    raise SlipError(f"{where}: unsupported value type {type(value).__name__}")


def _check_plan_and_values(pos: int, item: dict) -> dict:
    """核对内嵌计划、项身份与绑定值；返回校验过的 plan。"""
    plan = item.get("plan")
    try:
        validate_plan(plan)
    except PlanStoreError as exc:
        raise SlipError(f"item {pos}: embedded plan invalid: {exc}") from exc
    if plan["template_id"] != item.get("template_id") or plan["revision"] != item.get("revision"):
        raise SlipError(f"item {pos}: item identity does not match its embedded plan")
    ordered = item.get("ordered_values")
    if not isinstance(ordered, list) or len(ordered) != len(plan["parameters"]):
        raise SlipError(f"item {pos}: ordered_values does not match plan parameters")
    for j, value in enumerate(ordered):
        try:
            decode_value(value)
        except ValueError as exc:
            raise SlipError(f"item {pos} ordered_values[{j}]: {exc}") from exc
    return plan


def _export_select(pos: int, result: dict, spec: dict) -> dict:
    columns = result.get("columns")
    rows = result.get("rows")
    if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        raise SlipError(f"item {pos}: select result must list column names")
    if not isinstance(rows, list):
        raise SlipError(f"item {pos}: select result must list rows")
    for r_i, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(columns):
            raise SlipError(f"item {pos}: row {r_i} width does not match its columns")
        for c_i, cell in enumerate(row):
            check_result_value(cell, f"item {pos} row {r_i} column {c_i}")

    unknown = set(spec) - {"columns"}
    if unknown:
        raise SlipError(
            f"item {pos}: a select item only accepts a 'columns' visibility, got {sorted(unknown)}"
        )
    if "columns" not in spec:
        return {"kind": "select", "public": None}  # 完全封存

    positions = spec["columns"]
    if not isinstance(positions, list) or not positions:
        raise SlipError(f"item {pos}: public columns must be a non-empty list of positions")
    seen: set[int] = set()
    projected_names: list[str] = []
    for p in positions:
        if not isinstance(p, int) or isinstance(p, bool) or not (0 <= p < len(columns)):
            raise SlipError(f"item {pos}: public column position {p!r} out of range")
        if p in seen:
            raise SlipError(f"item {pos}: duplicate public column position {p}")
        seen.add(p)
        projected_names.append(columns[p])
    # 投影逐格复制：零行、一行 NULL、两行相同记录原样保留，绝不折叠。
    projected_rows = [[row[p] for p in positions] for row in rows]
    return {
        "kind": "select",
        "public": {
            "positions": list(positions),
            "columns": projected_names,
            "rows": projected_rows,
        },
    }


def _export_write(pos: int, kind: str, result: dict, spec: dict) -> dict:
    unknown = set(spec) - {"rowcount"}
    if unknown:
        raise SlipError(
            f"item {pos}: a write item only accepts a 'rowcount' visibility, got {sorted(unknown)}"
        )
    rowcount = result.get("rowcount")
    if not isinstance(rowcount, int) or isinstance(rowcount, bool) or rowcount < 0:
        raise SlipError(f"item {pos}: write result must carry a non-negative integer rowcount")
    if "rowcount" not in spec:
        return {"kind": kind}  # 行数保密
    if spec["rowcount"] is not True:
        raise SlipError(f"item {pos}: rowcount visibility must be true when present")
    return {"kind": kind, "stamped_rowcount": rowcount}


def export_slip(
    receipt: dict,
    *,
    counter_id: str,
    sequence: int,
    visibility: dict[int, dict] | None = None,
) -> dict:
    """从 committed 回执出具对账条。

    ``visibility`` 按 item 下标声明公开面：SELECT 项 ``{"columns": [列位...]}``，
    写操作项 ``{"rowcount": True}``；未列出的项完全保密。
    """
    _check_counter_id(counter_id)
    _check_sequence(sequence)
    visibility = visibility or {}
    if not isinstance(receipt, dict):
        raise SlipError("receipt must be a JSON object")
    if receipt.get("status") != "committed":
        raise SlipError("slips can only be exported from committed receipts")
    batch_id = receipt.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id:
        raise SlipError("receipt must name its batch_id")
    items = receipt.get("items")
    if not isinstance(items, list) or not items:
        raise SlipError("receipt must contain a non-empty items list")

    # 逐项核下标：回执 item 下标必须连续完整且全部成功。
    for pos, item in enumerate(items):
        if not isinstance(item, dict) or item.get("index") != pos:
            raise SlipError(f"receipt item index mismatch at position {pos}")
        if item.get("status") != "ok":
            raise SlipError(f"receipt item {pos} is not ok; cannot issue a slip")

    vis_by_index: dict[int, dict] = {}
    for key, spec in visibility.items():
        if not isinstance(key, int) or isinstance(key, bool) or not (0 <= key < len(items)):
            raise SlipError(f"visibility refers to missing item index: {key!r}")
        if not isinstance(spec, dict):
            raise SlipError(f"visibility spec for item {key} must be an object")
        vis_by_index[key] = spec

    slip_items = []
    for pos, item in enumerate(items):
        plan = _check_plan_and_values(pos, item)
        result = item.get("result")
        if not isinstance(result, dict):
            raise SlipError(f"item {pos}: missing result")
        kind = result.get("kind")
        spec = vis_by_index.get(pos, {})
        if kind == "select":
            exported = _export_select(pos, result, spec)
        elif kind in _WRITE_KINDS:
            exported = _export_write(pos, kind, result, spec)
        else:
            raise SlipError(f"item {pos}: unsupported result kind {kind!r}")
        slip_items.append(
            {
                "index": pos,
                "template_id": plan["template_id"],
                "revision": plan["revision"],
                "plan": plan,
                "ordered_values": list(item["ordered_values"]),
                "result": exported,
            }
        )

    return {
        "format": SLIP_FORMAT,
        "counter_id": counter_id,
        "sequence": sequence,
        "batch_id": batch_id,
        "status": "committed",
        "items": slip_items,
    }


def _validate_public(pos: int, public: Any) -> None:
    if public is None:
        return
    if not isinstance(public, dict) or set(public) != {"positions", "columns", "rows"}:
        raise SlipError(f"item {pos}: malformed public projection")
    positions, columns, rows = public["positions"], public["columns"], public["rows"]
    if (
        not isinstance(positions, list)
        or not positions
        or any(not isinstance(p, int) or isinstance(p, bool) or p < 0 for p in positions)
        or len(set(positions)) != len(positions)
    ):
        raise SlipError(f"item {pos}: invalid public column positions")
    if (
        not isinstance(columns, list)
        or len(columns) != len(positions)
        or not all(isinstance(c, str) for c in columns)
    ):
        raise SlipError(f"item {pos}: public columns do not match positions")
    if not isinstance(rows, list):
        raise SlipError(f"item {pos}: public rows must be a list")
    for r_i, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(positions):
            raise SlipError(f"item {pos}: public row {r_i} width does not match positions")
        for c_i, cell in enumerate(row):
            check_result_value(cell, f"item {pos} public row {r_i} column {c_i}")


def validate_slip(slip: object) -> dict:
    """按 sql-param-slip/v1 公开合同独立校验一条对账条（回单室一侧）。"""
    if not isinstance(slip, dict):
        raise SlipError("slip must be a JSON object")
    if slip.get("format") != SLIP_FORMAT:
        raise SlipError("unsupported slip format")
    _check_counter_id(slip.get("counter_id"))
    _check_sequence(slip.get("sequence"))
    if not isinstance(slip.get("batch_id"), str) or not slip["batch_id"]:
        raise SlipError("invalid batch_id")
    if slip.get("status") != "committed":
        raise SlipError("slip status must be committed")
    items = slip.get("items")
    if not isinstance(items, list) or not items:
        raise SlipError("slip must contain a non-empty items list")

    for pos, item in enumerate(items):
        if not isinstance(item, dict) or item.get("index") != pos:
            raise SlipError(f"slip item index mismatch at position {pos}")
        plan = _check_plan_and_values(pos, item)
        result = item.get("result")
        if not isinstance(result, dict):
            raise SlipError(f"item {pos}: missing result")
        kind = result.get("kind")
        if kind == "select":
            if set(result) - {"kind", "public"}:
                raise SlipError(f"item {pos}: unexpected fields in select result")
            _validate_public(pos, result.get("public"))
        elif kind in _WRITE_KINDS:
            if set(result) - {"kind", "stamped_rowcount"}:
                raise SlipError(f"item {pos}: unexpected fields in write result")
            stamped = result.get("stamped_rowcount")
            if stamped is not None and (
                not isinstance(stamped, int) or isinstance(stamped, bool) or stamped < 0
            ):
                raise SlipError(f"item {pos}: stamped rowcount must be a non-negative integer")
        else:
            raise SlipError(f"item {pos}: unsupported result kind {kind!r}")
        if item.get("template_id") != plan["template_id"] or item.get("revision") != plan["revision"]:
            raise SlipError(f"item {pos}: item identity does not match its embedded plan")
    return slip
