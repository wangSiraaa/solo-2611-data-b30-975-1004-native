"""回单室合账：从起始快照核对多台离线柜台的对账条。

设定：仓库保存一张起始 SQLite 快照；每台柜台只保证本机批次的先后（对账条上的
``sequence``），柜台之间没有统一时钟。回单室要判断：是否存在一条交错顺序，使得
从快照真实重放全部批次后，每条对账条上的公开投影与盖章 rowcount 都逐项吻合。
只有批次真实执行成功且所有公开投影与盖章结果相同，这批回单才算被解释。

模板限制（合账合同，逐项强制）：

- 单表：一条语句只允许引用合账配置的那一张表（INSERT INTO / UPDATE / FROM / JOIN
  任一引用到其它表即拒绝）；
- 该表必须有且仅有一个 INTEGER PRIMARY KEY 列（由快照 PRAGMA 核实）；
- 只允许参数化 SELECT / INSERT / UPDATE；正常区域不得出现函数调用（VALUES 与
  INSERT 列名括号除外），从根本上排除非确定性/外部函数；
- 带公开投影的 SELECT 必须显式 ORDER BY 且排序键包含主键（公开多行结果的全序）。

搜索：把快照复制进内存库，以 SAVEPOINT 深搜全部交错顺序，按 (各柜台进度, 整表
状态) 去重；找到的全部可行终局各自保留一条见证顺序，并用 verify_order 独立重放
生成逐项核对证据。没有任何交错顺序能解释全部批次时，整批隔离（quarantined），
一个批次也不入账。每轮至多 20 个批次。
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from .bindings import decode_value, encode_json_value
from .compiler import normal_view
from .errors import ReconcileError, SlipError, TemplateCompileError
from .slips import validate_slip

RECONCILE_FORMAT = "sql-param-reconcile/v1"
MAX_BATCHES_PER_ROUND = 20
DEFAULT_NODE_BUDGET = 1_000_000

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_TABLE_NAME = rf'"?(?:{_IDENT})"?'
_REF_PATTERNS = (
    re.compile(rf"\bINSERT\s+INTO\s+(?P<t>{_TABLE_NAME})", re.IGNORECASE),
    re.compile(rf"\bUPDATE\s+(?P<t>{_TABLE_NAME})", re.IGNORECASE),
    re.compile(rf"\bFROM\s+(?P<t>{_TABLE_NAME})", re.IGNORECASE),
    re.compile(rf"\bJOIN\s+(?P<t>{_TABLE_NAME})", re.IGNORECASE),
)
_FUNCTION_CALL = re.compile(rf"(?P<n>{_IDENT})\s*\(", re.IGNORECASE)
_LEADING = re.compile(r"\s*(SELECT|INSERT|UPDATE)\b", re.IGNORECASE)
_ORDER_BY = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)
_TABLE_SETTING = re.compile(rf"{_IDENT}\Z")


# ---- 输入准备 -------------------------------------------------------------
def _decode_result_value(value: Any, where: str) -> Any:
    """把对账条结果域的 JSON 值解码为 Python 值（允许 float，拒绝 bool 等）。"""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        raise ReconcileError(f"{where}: boolean is not a legal result value")
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, dict) and set(value) == {"$bytes"}:
        try:
            return decode_value(value)
        except ValueError as exc:
            raise ReconcileError(f"{where}: {exc}") from exc
    raise ReconcileError(f"{where}: unsupported value type {type(value).__name__}")


def _integer_pk(conn: sqlite3.Connection, table: str) -> str:
    """核实配置表存在且有且仅有一个 INTEGER PRIMARY KEY 列，返回列名。"""
    if not isinstance(table, str) or _TABLE_SETTING.fullmatch(table) is None:
        raise ReconcileError(f"unsafe table name: {table!r}")
    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    if not info:
        raise ReconcileError(f"table not found in snapshot: {table!r}")
    pks = [row for row in info if row[5] > 0]
    if len(pks) != 1:
        raise ReconcileError("reconciled table must have exactly one primary key column")
    pk = pks[0]
    if str(pk[2]).upper() != "INTEGER":
        raise ReconcileError("primary key must be declared INTEGER (rowid alias)")
    return pk[1]


def _check_item_contract(label: str, item: dict, *, table: str, pk_column: str) -> None:
    """对单个对账条项强制模板限制（单表、无函数、公开查询全序）。"""
    plan = item["plan"]
    try:
        normal = normal_view(plan["sql"])
    except TemplateCompileError as exc:
        raise ReconcileError(f"{label}: {exc}") from exc
    if _LEADING.match(normal) is None:
        raise ReconcileError(f"{label}: only SELECT/INSERT/UPDATE statements are allowed")

    refs: list[str] = []
    for pattern in _REF_PATTERNS:
        refs.extend(m.group("t") for m in pattern.finditer(normal))
    names = {ref.strip('"').lower() for ref in refs}
    if names != {table.lower()}:
        raise ReconcileError(
            f"{label}: statements must reference only the reconciled table {table!r}"
        )
    for m in _FUNCTION_CALL.finditer(normal):
        name = m.group("n")
        if name.upper() == "VALUES" or name.lower() == table.lower():
            continue  # INSERT 的 VALUES(...) 与列名括号不是函数调用
        raise ReconcileError(f"{label}: function-like call {name}(...) is not allowed")

    result = item["result"]
    if result["kind"] == "select" and result.get("public") is not None:
        order_match = _ORDER_BY.search(normal)
        if order_match is None:
            raise ReconcileError(f"{label}: a public SELECT must declare ORDER BY")
        segment = normal[order_match.end():].replace('"', " ")
        if re.search(rf"\b{re.escape(pk_column)}\b", segment, re.IGNORECASE) is None:
            raise ReconcileError(
                f"{label}: ORDER BY must include primary key {pk_column!r} for a total order"
            )


def _prepare_slip(slip: dict, *, table: str, pk_column: str) -> dict:
    """校验并预解码一条对账条：绑定值、公开投影都转成 Python 值。"""
    items = []
    for raw in slip["items"]:
        label = f"batch {slip['batch_id']!r} item {raw['index']}"
        _check_item_contract(label, raw, table=table, pk_column=pk_column)
        result = raw["result"]
        try:
            args = tuple(decode_value(v) for v in raw["ordered_values"])
        except ValueError as exc:
            raise ReconcileError(f"{label}: {exc}") from exc
        public = None
        if result["kind"] == "select" and result.get("public") is not None:
            pub = result["public"]
            public = {
                "positions": list(pub["positions"]),
                "columns": list(pub["columns"]),
                "rows": [[_decode_result_value(v, label) for v in row] for row in pub["rows"]],
            }
        items.append(
            {
                "index": raw["index"],
                "sql": raw["plan"]["sql"],
                "args": args,
                "kind": result["kind"],
                "stamped_rowcount": result.get("stamped_rowcount"),
                "public": public,
            }
        )
    return {
        "counter_id": slip["counter_id"],
        "batch_id": slip["batch_id"],
        "sequence": slip["sequence"],
        "items": items,
    }


def _prepare_counters(
    counters: list[dict], *, table: str, pk_column: str, max_batches: int
) -> list[dict]:
    if not isinstance(counters, list) or not counters:
        raise ReconcileError("counters must be a non-empty list")
    seen_counters: set[str] = set()
    seen_batches: set[str] = set()
    total = 0
    prepared = []
    for entry in counters:
        if not isinstance(entry, dict):
            raise ReconcileError("each counter entry must be an object")
        counter_id = entry.get("counter_id")
        if not isinstance(counter_id, str) or not counter_id:
            raise ReconcileError("each counter must name a non-empty counter_id")
        if counter_id in seen_counters:
            raise ReconcileError(f"duplicate counter_id {counter_id!r}")
        seen_counters.add(counter_id)
        slips = entry.get("slips")
        if not isinstance(slips, list) or not slips:
            raise ReconcileError(f"counter {counter_id!r}: slips must be a non-empty list")
        total += len(slips)
        sequences = []
        prepared_slips = []
        for slip in slips:
            try:
                validate_slip(slip)
            except SlipError as exc:
                raise ReconcileError(f"counter {counter_id!r}: invalid slip: {exc}") from exc
            if slip["counter_id"] != counter_id:
                raise ReconcileError(
                    f"counter {counter_id!r}: slip batch {slip['batch_id']!r} "
                    f"names counter {slip['counter_id']!r}"
                )
            if slip["batch_id"] in seen_batches:
                raise ReconcileError(f"duplicate batch_id {slip['batch_id']!r}")
            seen_batches.add(slip["batch_id"])
            sequences.append(slip["sequence"])
            prepared_slips.append(_prepare_slip(slip, table=table, pk_column=pk_column))
        if len(set(sequences)) != len(sequences) or sequences != sorted(sequences):
            raise ReconcileError(
                f"counter {counter_id!r}: sequences must be strictly increasing in local order"
            )
        prepared.append({"counter_id": counter_id, "slips": prepared_slips})
    if total > max_batches:
        raise ReconcileError(f"too many batches in one round ({total} > {max_batches})")
    return prepared


def _open_memory_copy(snapshot_path: Path) -> sqlite3.Connection:
    if not snapshot_path.exists():
        raise ReconcileError(f"snapshot not found: {snapshot_path}")
    source = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True)
    memory = sqlite3.connect(":memory:")
    memory.isolation_level = None  # 自己用 SAVEPOINT 管理事务
    try:
        source.backup(memory)
    finally:
        source.close()
    return memory


def _prepare_round(
    snapshot_path: str | Path, table: str, counters: list[dict], pk_column: str | None,
    max_batches: int,
) -> tuple[sqlite3.Connection, str, list[dict]]:
    memory = _open_memory_copy(Path(snapshot_path))
    try:
        detected = _integer_pk(memory, table)
        if pk_column is None:
            pk_column = detected
        elif pk_column != detected:
            raise ReconcileError(
                f"pk_column {pk_column!r} does not match snapshot primary key {detected!r}"
            )
        prepared = _prepare_counters(
            counters, table=table, pk_column=pk_column, max_batches=max_batches
        )
    except BaseException:
        memory.close()
        raise
    return memory, pk_column, prepared


# ---- 重放与核对 -----------------------------------------------------------
def _values_match(actual: Any, expected: Any) -> bool:
    """严格类型相等：NULL/整数/文本/字节/浮点互不混同。"""
    if type(actual) is not type(expected):
        return False
    return actual == expected


def _projection_matches(public: dict, columns: list[str], rows: list[tuple]) -> bool:
    positions = public["positions"]
    if any(p >= len(columns) for p in positions):
        return False
    if [columns[p] for p in positions] != public["columns"]:
        return False
    expected = public["rows"]
    if len(rows) != len(expected):
        return False
    for actual_row, expected_row in zip(rows, expected):
        for p, expected_cell in zip(positions, expected_row):
            if not _values_match(actual_row[p], expected_cell):
                return False
    return True


def _apply_batch(conn: sqlite3.Connection, prepared: dict) -> bool:
    """执行一个不可拆批次；全部公开投影与盖章行数吻合才返回 True。"""
    for item in prepared["items"]:
        try:
            cursor = conn.execute(item["sql"], item["args"])
        except sqlite3.Error:
            return False
        if item["kind"] == "select":
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            if item["public"] is not None and not _projection_matches(
                item["public"], columns, rows
            ):
                return False
        else:
            if item["stamped_rowcount"] is not None and cursor.rowcount != item["stamped_rowcount"]:
                return False
    return True


def _dump_state(conn: sqlite3.Connection, table: str, pk_column: str):
    cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY "{pk_column}"')
    columns = [desc[0] for desc in cursor.description]
    return columns, cursor.fetchall()


# ---- 合账主入口 -----------------------------------------------------------
def reconcile_round(
    snapshot_path: str | Path,
    *,
    table: str,
    counters: list[dict],
    pk_column: str | None = None,
    max_batches: int = MAX_BATCHES_PER_ROUND,
    node_budget: int = DEFAULT_NODE_BUDGET,
) -> dict:
    """核对一轮对账条，返回 sql-param-reconcile/v1 报告。

    存在解释时 status 为 ``explained``，``terminals`` 列出全部可行终局（各自带
    一条见证顺序与逐项核对证据）；无解时 status 为 ``quarantined``，全部批次
    整批隔离。
    """
    memory, pk_column, prepared = _prepare_round(
        snapshot_path, table, counters, pk_column, max_batches
    )
    try:
        total_batches = sum(len(c["slips"]) for c in prepared)
        progress = [0] * len(prepared)
        order: list[tuple[int, dict]] = []
        terminals: dict = {}
        visited: set = set()
        stats = {"nodes_visited": 0, "branches_pruned": 0}

        def dfs() -> None:
            if stats["nodes_visited"] >= node_budget:
                raise ReconcileError(f"search budget exceeded ({node_budget} nodes)")
            stats["nodes_visited"] += 1
            depth = sum(progress)
            if depth == total_batches:
                columns, rows = _dump_state(memory, table, pk_column)
                key = (tuple(columns), tuple(rows))
                if key not in terminals:  # 同一终局只保留第一条见证顺序
                    terminals[key] = {"columns": columns, "rows": rows, "witness": list(order)}
                return
            columns, rows = _dump_state(memory, table, pk_column)
            key = (tuple(progress), tuple(columns), tuple(rows))
            if key in visited:
                return
            visited.add(key)
            for ci, counter in enumerate(prepared):
                if progress[ci] >= len(counter["slips"]):
                    continue
                slip = counter["slips"][progress[ci]]
                memory.execute(f"SAVEPOINT sp{depth}")
                if _apply_batch(memory, slip):
                    progress[ci] += 1
                    order.append((ci, slip))
                    dfs()
                    order.pop()
                    progress[ci] -= 1
                else:
                    stats["branches_pruned"] += 1
                memory.execute(f"ROLLBACK TO sp{depth}")
                memory.execute(f"RELEASE sp{depth}")

        dfs()
    finally:
        memory.close()

    terminal_list = []
    for number, record in enumerate(terminals.values(), start=1):
        witness_order = [
            {
                "counter_id": prepared[ci]["counter_id"],
                "batch_id": slip["batch_id"],
                "sequence": slip["sequence"],
            }
            for ci, slip in record["witness"]
        ]
        verification = verify_order(
            snapshot_path,
            table=table,
            counters=counters,
            order=witness_order,
            pk_column=pk_column,
        )
        terminal_list.append(
            {
                "terminal_id": f"t{number}",
                "columns": list(record["columns"]),
                "rows": [[encode_json_value(v) for v in row] for row in record["rows"]],
                "witness_order": witness_order,
                "evidence": verification["evidence"],
            }
        )

    explained = bool(terminal_list)
    report = {
        "format": RECONCILE_FORMAT,
        "table": table,
        "pk_column": pk_column,
        "status": "explained" if explained else "quarantined",
        "terminals": terminal_list,
        "batches": [
            {
                "counter_id": counter["counter_id"],
                "batch_id": slip["batch_id"],
                "sequence": slip["sequence"],
                "status": "explained" if explained else "quarantined",
            }
            for counter in prepared
            for slip in counter["slips"]
        ],
        "search": {**stats, "node_budget": node_budget},
    }
    if not explained:
        report["reason"] = (
            "no interleaving reproduces every public projection and stamped rowcount; "
            "all batches quarantined"
        )
    return report


def verify_order(
    snapshot_path: str | Path,
    *,
    table: str,
    counters: list[dict],
    order: list[dict],
    pk_column: str | None = None,
    max_batches: int = MAX_BATCHES_PER_ROUND,
) -> dict:
    """独立复核一条声称可行的交错顺序：从快照重新真实执行，返回逐项核对证据。

    ``order`` 必须恰好覆盖本轮全部批次各一次，且尊重每台柜台的本机先后。
    返回 ``{"status": "explained"|"failed", "evidence": [...], ...}``。
    """
    memory, pk_column, prepared = _prepare_round(
        snapshot_path, table, counters, pk_column, max_batches
    )
    try:
        lookup = {
            (counter["counter_id"], slip["batch_id"]): slip
            for counter in prepared
            for slip in counter["slips"]
        }
        if not isinstance(order, list):
            raise ReconcileError("order must be a list")
        claimed = []
        for entry in order:
            if not isinstance(entry, dict):
                raise ReconcileError("each order entry must be an object")
            claimed.append((entry.get("counter_id"), entry.get("batch_id")))
        if len(claimed) != len(lookup) or set(claimed) != set(lookup):
            raise ReconcileError("order must list every batch of the round exactly once")
        # 本机前后关系：每台柜台在交错中的相对顺序必须等于其本地顺序。
        for counter in prepared:
            local = [s["batch_id"] for s in counter["slips"]]
            interleaved = [b for c, b in claimed if c == counter["counter_id"]]
            if interleaved != local:
                raise ReconcileError(
                    f"order violates the local order of counter {counter['counter_id']!r}"
                )

        evidence = []
        status = "explained"
        failed_at = None
        for counter_id, batch_id in claimed:
            slip = lookup[(counter_id, batch_id)]
            batch_evidence = {
                "counter_id": counter_id,
                "batch_id": batch_id,
                "sequence": slip["sequence"],
                "items": [],
            }
            memory.execute("BEGIN")
            failed_item = None
            for item in slip["items"]:
                item_evidence: dict[str, Any] = {"index": item["index"]}
                try:
                    cursor = memory.execute(item["sql"], item["args"])
                except sqlite3.Error as exc:
                    item_evidence.update({"executed": False, "error": str(exc)})
                    batch_evidence["items"].append(item_evidence)
                    failed_item = item["index"]
                    break
                item_evidence["executed"] = True
                if item["kind"] == "select":
                    columns = [desc[0] for desc in cursor.description]
                    rows = cursor.fetchall()
                    if item["public"] is not None:
                        positions = item["public"]["positions"]
                        in_range = all(p < len(columns) for p in positions)
                        actual_rows = (
                            [[encode_json_value(row[p]) for p in positions] for row in rows]
                            if in_range
                            else None
                        )
                        match = _projection_matches(item["public"], columns, rows)
                        item_evidence.update(
                            {
                                "check": "projection",
                                "positions": positions,
                                "expected_rows": item["public"]["rows"],
                                "actual_rows": actual_rows,
                                "match": match,
                            }
                        )
                        if not match:
                            batch_evidence["items"].append(item_evidence)
                            failed_item = item["index"]
                            break
                    else:
                        item_evidence["check"] = "sealed"
                else:
                    if item["stamped_rowcount"] is not None:
                        match = cursor.rowcount == item["stamped_rowcount"]
                        item_evidence.update(
                            {
                                "check": "rowcount",
                                "expected": item["stamped_rowcount"],
                                "actual": cursor.rowcount,
                                "match": match,
                            }
                        )
                        if not match:
                            batch_evidence["items"].append(item_evidence)
                            failed_item = item["index"]
                            break
                    else:
                        item_evidence["check"] = "sealed"
                batch_evidence["items"].append(item_evidence)

            if failed_item is None:
                memory.execute("COMMIT")
                batch_evidence["batch_status"] = "committed"
            else:
                memory.execute("ROLLBACK")
                batch_evidence["batch_status"] = "failed"
                status = "failed"
                failed_at = {"batch_id": batch_id, "item_index": failed_item}
                evidence.append(batch_evidence)
                break
            evidence.append(batch_evidence)

        final_state = None
        if status == "explained":
            columns, rows = _dump_state(memory, table, pk_column)
            final_state = {
                "columns": columns,
                "rows": [[encode_json_value(v) for v in row] for row in rows],
            }
        return {
            "status": status,
            "evidence": evidence,
            "final_state": final_state,
            "failed_at": failed_at,
        }
    finally:
        memory.close()
