"""授信席 CLI：评估一轮新申请，可选正式签发最佳集合。

配置文件格式：
    bins.json:         {"bins": {"X": 1, "Y": 2}, "quantity_column": "qty",
                        "pk_column": "id"}          # pk_column 可选
    applications.json: {"applications": [{"application_id": "P", "quantity": 5,
                                          "bins": ["X"], "value": 8}, ...]}
用法：
    python -m cli.credit_round <ledger.json> <report.json> <bins.json> \
        <applications.json> [--sign]
不加 --sign 只打印评估结果（最佳集合与单验结果）；加 --sign 正式签发最佳集合
并落账（占用额度，同一申请不能签两次）。输入不合合同退出码 1。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from sql_param.credit import CreditDesk, capacities_from_report
from sql_param.errors import CreditError


def main(argv: list[str]) -> int:
    args = argv[1:]
    sign = "--sign" in args
    args = [a for a in args if a != "--sign"]
    if len(args) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    ledger_path, report_file, bins_file, applications_file = args
    report = json.loads(Path(report_file).read_text(encoding="utf-8"))
    bins_config = json.loads(Path(bins_file).read_text(encoding="utf-8"))
    applications = json.loads(Path(applications_file).read_text(encoding="utf-8"))["applications"]
    try:
        terminals = capacities_from_report(
            report,
            bins=bins_config["bins"],
            quantity_column=bins_config["quantity_column"],
            pk_column=bins_config.get("pk_column"),
        )
        desk = CreditDesk(ledger_path, terminals)
        evaluation = desk.evaluate(applications)
        result = {"evaluation": evaluation, "signed": desk.signed()}
        if sign:
            result["newly_signed"] = desk.sign(applications)
    except (CreditError, KeyError) as exc:
        print(f"credit rejected: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
