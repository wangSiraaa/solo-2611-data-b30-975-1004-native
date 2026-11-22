"""读取周度请求 JSON，做选道/选单规划并落盘，可再用回执核对重送订单。

周度请求文件格式::

    {
      "week_id": "w1",
      "template_id": "ord",
      "budget": 2,
      "channels": [
        {"channel_id": "c12", "source_revision": 1, "target_revision": 2,
         "fee": 1, "mapping": {"k": ["a", "b"]}},
        ...
      ],
      "orders": [
        {"order_id": "S-1", "source_revision": 1, "target_revision": 3,
         "priority": 2, "bindings": {"a": 3, "b": 3},
         "checks": {"x": 3}}
      ]
    }

用法::

    python -m cli.plan_week <store_dir> <out_dir> <weekly_request.json>
    python -m cli.plan_week --verify <ledger.json> <week_id> <resend_order.json>

规划回执写到 <out_dir>/<week_id>.json，台账写到 <out_dir>/weekly-ledger.json；
退出码 0 表示规划完成（可能仍有订单被拒，看回执 orders[*].status），输入不合法为 1。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from sql_param.channels import Channel, ChannelCatalog
from sql_param.errors import ChannelError, PlanningError
from sql_param.plan_store import PlanStore
from sql_param.planner import WeeklyLedger, WeeklyPlanner


def _plan(store_dir: str, out_dir: str, request_file: str) -> int:
    request = json.loads(Path(request_file).read_text(encoding="utf-8"))
    try:
        week_id = request["week_id"]
        template_id = request["template_id"]
        budget = request["budget"]
        channels = [Channel.from_dict(item) for item in request["channels"]]
        orders = request["orders"]
    except KeyError as exc:
        print(f"weekly request missing field {exc.args[0]!r}", file=sys.stderr)
        return 1
    except ChannelError as exc:
        print(f"invalid channel: {exc}", file=sys.stderr)
        return 1

    store = PlanStore(store_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    plan_path = out / f"{week_id}.json"
    if plan_path.exists():
        print(f"weekly plan {week_id!r} already exists at {plan_path}", file=sys.stderr)
        return 1

    try:
        catalog = ChannelCatalog(store, template_id, channels)
        plan = WeeklyPlanner(catalog).plan_week(week_id, budget, orders)
        WeeklyLedger(out / "weekly-ledger.json").record_week(plan)
    except (ChannelError, PlanningError) as exc:
        print(f"planning rejected: {exc}", file=sys.stderr)
        return 1

    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "week_id": plan["week_id"],
        "purchased_channel_ids": plan["purchased_channel_ids"],
        "channel_cost": plan["channel_cost"],
        "budget": plan["budget"],
        "total_score": plan["total_score"],
        "considered_subsets": plan["considered_subsets"],
        "accepted": [o["order_id"] for o in plan["orders"] if o["status"] == "accepted"],
        "rejected": [
            {"order_id": o["order_id"], "reason": o["reason"]}
            for o in plan["orders"] if o["status"] == "rejected"
        ],
        "receipt": str(plan_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _verify(ledger_file: str, week_id: str, order_file: str) -> int:
    order = json.loads(Path(order_file).read_text(encoding="utf-8"))
    result = WeeklyLedger(ledger_file).verify_resend(week_id, order)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("same_payload") else 3


def main(argv: list[str]) -> int:
    if len(argv) == 5 and argv[1] == "--verify":
        return _verify(argv[2], argv[3], argv[4])
    if len(argv) == 4:
        return _plan(argv[1], argv[2], argv[3])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
