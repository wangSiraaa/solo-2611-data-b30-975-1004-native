"""回单室合账 CLI：从起始快照核对一轮离线柜台对账条。

轮次文件格式：
    {"counters": [{"counter_id": "c1", "slips": [<对账条 JSON 对象>, ...]}, ...],
     "pk_column": "id"            # 可选，默认取快照主键
    }
用法：
    python -m cli.reconcile_round <snapshot.db> <table> <round.json>
explained 退出码 0，quarantined 退出码 3，输入不合合同退出码 1。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from sql_param.errors import ReconcileError
from sql_param.reconcile import reconcile_round


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    snapshot, table, round_file = argv[1:]
    payload = json.loads(Path(round_file).read_text(encoding="utf-8"))
    try:
        report = reconcile_round(
            snapshot,
            table=table,
            counters=payload["counters"],
            pk_column=payload.get("pk_column"),
        )
    except (ReconcileError, KeyError) as exc:
        print(f"reconcile rejected: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "explained" else 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
