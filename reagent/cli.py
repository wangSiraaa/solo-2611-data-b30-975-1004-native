"""Independent CLI entry points for inventory and run orchestration."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading

from .errors import ReagentError
from .inventory import InventoryService
from .billing import BillingService
from .clearing import Route, settle
from .runs import RunService
from .schema import connect, initialize


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _emit(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


# ---------------------------------------------------------------------------
# command handlers
# ---------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    initialize(conn)
    document = _load_json(args.inventory)
    result = InventoryService(conn).initialize(document)
    conn.close()
    return {"initialized": result}


def cmd_query(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = InventoryService(conn).query(args.bottle, args.day)
    conn.close()
    return result


def cmd_snapshot(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = InventoryService(conn).snapshot(args.day)
    conn.close()
    return {"day": args.day, "bottles": result}


def cmd_conservation(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = InventoryService(conn).conservation()
    conn.close()
    return {"conservation": result}


def cmd_open(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = InventoryService(conn).open_bottle(args.bottle, args.day)
    conn.close()
    return result


def cmd_hold(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = InventoryService(conn).hold(
        args.holder_id, _parse_selection(args.bottle), args.day
    )
    conn.close()
    return result


def cmd_consume_hold(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = InventoryService(conn).consume_held(args.holder_id, args.day)
    conn.close()
    return {"holder_id": args.holder_id, "consumed": result}


def cmd_release_hold(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    released = InventoryService(conn).release_held(
        args.holder_id, args.day, args.reason
    )
    conn.close()
    return {"holder_id": args.holder_id, "released_reservations": released}


def cmd_get_hold(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = InventoryService(conn).holder_view(args.holder_id, args.day)
    conn.close()
    return result


def cmd_create(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    service = RunService(conn)
    result = service.create_run(
        args.run_id,
        args.method_id,
        args.method_revision,
        args.day,
        _parse_selection(args.bottle),
    )
    conn.close()
    return result


def cmd_start(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = RunService(conn).start_run(args.run_id, args.day)
    conn.close()
    return result


def cmd_release(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = RunService(conn).release_run(args.run_id, args.day, args.reason)
    conn.close()
    return result


def cmd_revise(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = RunService(conn).revise_method(
        args.method_id, args.revision, _parse_requirements(args.requirement), args.day
    )
    conn.close()
    return result


def cmd_get_run(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = RunService(conn).get_run(args.run_id)
    conn.close()
    return result


def cmd_run_ops(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    initialize(conn)
    document = _load_json(args.inventory)
    InventoryService(conn).initialize(document)
    results = execute_operations(conn, _load_json(args.operations))
    conn.close()
    return {"results": results}


# ---------------------------------------------------------------------------
# billing command handlers
# ---------------------------------------------------------------------------
def cmd_period_open(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = BillingService(conn).open_period(args.period_id, args.day)
    conn.close()
    return result


def cmd_price(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = BillingService(conn).add_price(
        args.period_id, args.bottle, args.price, args.on_day
    )
    conn.close()
    return result


def cmd_voucher(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    document = _load_json(args.document)
    result = BillingService(conn).issue_voucher(document)
    conn.close()
    return result


def cmd_vouchers(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = BillingService(conn).voucher_status()
    conn.close()
    return {"vouchers": result}


def cmd_credentials(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    claims = BillingService(conn).load_credentials(args.period_id)
    conn.close()
    return {
        "credentials": [
            {
                "reservation_id": c.reservation_id,
                "seq": c.seq,
                "counter_id": c.counter_id,
                "bottle_id": c.bottle_id,
                "settle_day": c.settle_day,
                "list_price": c.list_price,
            }
            for c in claims
        ]
    }


def cmd_bill(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = BillingService(conn).issue_bill(args.period_id, args.day)
    conn.close()
    return result


def cmd_bill_get(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = BillingService(conn).get_bill(args.period_id)
    conn.close()
    return result


def cmd_bill_close(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = BillingService(conn).close_period(args.period_id)
    conn.close()
    return result


def cmd_period_reopen(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = BillingService(conn).reopen_period(args.period_id)
    conn.close()
    return result


def cmd_settlement(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = BillingService(conn).settlement(args.period_id)
    conn.close()
    return result


def cmd_clear(args: argparse.Namespace) -> dict:
    document = _load_json(args.document)
    routes = [
        Route(
            route_id=item["route_id"],
            source=item["source"],
            target=item["target"],
            capacity=int(item["capacity"]),
            activation_fee=int(item["activation_fee"]),
        )
        for item in document.get("routes", [])
    ]
    result = settle(document["balances"], routes)
    return {
        "feasible": result.feasible,
        "total_activation_fee": result.total_activation_fee,
        "reason": result.reason,
        "instructions": [
            {
                "route_id": ins.route_id,
                "source": ins.source,
                "target": ins.target,
                "amount": ins.amount,
                "activation_fee": ins.activation_fee,
            }
            for ins in result.instructions
        ],
    }


# ---------------------------------------------------------------------------
# operation file executor (also used by tests)
# ---------------------------------------------------------------------------
def _parse_selection(raw: list[str]) -> list[dict]:
    selection = []
    for item in raw:
        bottle_id, _, portions = item.partition("=")
        if not bottle_id or not portions:
            raise ValueError(f"selection must be BOTTLE=PORTIONS, got {item!r}")
        selection.append({"bottle_id": bottle_id, "portions": int(portions)})
    return selection


def _parse_requirements(raw: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in raw:
        kind, _, portions = item.partition("=")
        if not kind or not portions:
            raise ValueError(f"requirement must be KIND=PORTIONS, got {item!r}")
        out[kind] = int(portions)
    return out


def execute_operations(conn: sqlite3.Connection, document: dict) -> list[dict]:
    inventory = InventoryService(conn)
    runs = RunService(conn)
    results: list[dict] = []
    for op in document.get("operations", []):
        results.append(_execute_one(conn, inventory, runs, op))
    return results


def _execute_one(
    conn: sqlite3.Connection,
    inventory: InventoryService,
    runs: RunService,
    op: dict,
) -> dict:
    name = op.get("op")
    if name == "create":
        result = runs.create_run(
            op["run_id"],
            op["method_id"],
            op["method_revision"],
            op["day"],
            op["selection"],
        )
        return {"op": name, "run_id": op["run_id"], "result": result}
    if name == "start" or name == "repeat_start":
        result = runs.start_run(op["run_id"], op["day"])
        return {"op": name, "run_id": op["run_id"], "result": result}
    if name == "release":
        result = runs.release_run(op["run_id"], op["day"], op["reason"])
        return {"op": name, "run_id": op["run_id"], "result": result}
    if name == "revise_method":
        result = runs.revise_method(
            op["method_id"], op["revision"], op["requirements"], op["day"]
        )
        return {"op": name, "result": result}
    if name == "query":
        result = inventory.query(op["bottle_id"], op["day"])
        return {"op": name, "result": result}
    if name == "open":
        result = inventory.open_bottle(op["bottle_id"], op["day"])
        return {"op": name, "result": result}
    if name == "reserve_competition":
        return {
            "op": name,
            "result": _reserve_competition(conn, op),
        }
    if name == "conservation":
        return {"op": name, "result": inventory.conservation()}
    raise ValueError(f"unknown operation {name!r}")


def _reserve_competition(conn: sqlite3.Connection, op: dict) -> dict:
    """Two independent connections race to reserve the same last portions."""
    db_path = _database_path(conn)
    run_ids = list(op["run_ids"])
    if len(run_ids) != 2:
        raise ValueError("reserve_competition needs exactly two run_ids")
    barrier = threading.Barrier(len(run_ids))
    outcomes: dict[str, dict] = {}
    lock = threading.Lock()

    def worker(run_id: str) -> None:
        worker_conn = connect(db_path)
        try:
            barrier.wait(timeout=5)
            result = RunService(worker_conn).create_run(
                run_id,
                op["method_id"],
                op["method_revision"],
                op["day"],
                op["selection"],
            )
            outcome = {"ok": True, "run_id": run_id, "result": result}
        except ReagentError as exc:
            outcome = {
                "ok": False,
                "run_id": run_id,
                "error_type": exc.error_type,
                "error": str(exc),
            }
        except sqlite3.Error as exc:
            outcome = {
                "ok": False,
                "run_id": run_id,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        finally:
            worker_conn.close()
        with lock:
            outcomes[run_id] = outcome

    threads = [threading.Thread(target=worker, args=(rid,)) for rid in run_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    winners = [rid for rid, out in outcomes.items() if out.get("ok")]
    losers = [rid for rid, out in outcomes.items() if not out.get("ok")]
    if len(winners) != 1 or len(losers) != 1:
        raise AssertionError(
            f"expected exactly one winner, got outcomes={outcomes}"
        )
    return {
        "winners": winners,
        "losers": losers,
        "outcomes": outcomes,
    }


def _database_path(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    path = row["file"] if row is not None else None
    if not path:
        raise ValueError("reserve_competition requires a file-backed database")
    return path


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reagent", description="Reagent inventory and run service"
    )
    parser.add_argument("--db", required=True, help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="initialize an empty database from a document")
    p.add_argument("--inventory", required=True)
    p.set_defaults(handler=cmd_init)

    p = sub.add_parser("query", help="read-only bottle allocation for a day")
    p.add_argument("--bottle", required=True)
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_query)

    p = sub.add_parser("snapshot", help="read-only allocation for every bottle")
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_snapshot)

    p = sub.add_parser("conservation", help="verify per-bottle conservation")
    p.set_defaults(handler=cmd_conservation)

    p = sub.add_parser("open", help="manually open a bottle")
    p.add_argument("--bottle", required=True)
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_open)

    p = sub.add_parser("hold", help="reserve portions for an arbitrary holder")
    p.add_argument("--holder-id", required=True)
    p.add_argument("--day", required=True, type=int)
    p.add_argument("--bottle", action="append", required=True,
                   help="BOTTLE=PORTIONS; repeat per bottle")
    p.set_defaults(handler=cmd_hold)

    p = sub.add_parser("consume-hold", help="consume a holder's reservations")
    p.add_argument("--holder-id", required=True)
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_consume_hold)

    p = sub.add_parser("release-hold", help="release a holder's reservations")
    p.add_argument("--holder-id", required=True)
    p.add_argument("--day", required=True, type=int)
    p.add_argument("--reason", required=True)
    p.set_defaults(handler=cmd_release_hold)

    p = sub.add_parser("get-hold", help="show a holder's derived allocation")
    p.add_argument("--holder-id", required=True)
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_get_hold)

    p = sub.add_parser("create", help="create a run and reserve its demand")
    p.add_argument("--run-id", required=True)
    p.add_argument("--method-id", required=True)
    p.add_argument("--method-revision", required=True, type=int)
    p.add_argument("--day", required=True, type=int)
    p.add_argument("--bottle", action="append", required=True,
                   help="BOTTLE=PORTIONS; repeat per bottle")
    p.set_defaults(handler=cmd_create)

    p = sub.add_parser("start", help="start a run (consume reservations)")
    p.add_argument("--run-id", required=True)
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_start)

    p = sub.add_parser("release", help="release a run's active reservations")
    p.add_argument("--run-id", required=True)
    p.add_argument("--day", required=True, type=int)
    p.add_argument("--reason", required=True)
    p.set_defaults(handler=cmd_release)

    p = sub.add_parser("revise", help="register a new method revision")
    p.add_argument("--method-id", required=True)
    p.add_argument("--revision", required=True, type=int)
    p.add_argument("--requirement", action="append", required=True,
                   help="KIND=PORTIONS; repeat per kind")
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_revise)

    p = sub.add_parser("get-run", help="show a run and its allocation facts")
    p.add_argument("--run-id", required=True)
    p.set_defaults(handler=cmd_get_run)

    p = sub.add_parser("run-ops", help="initialize and execute an operation file")
    p.add_argument("--inventory", required=True)
    p.add_argument("--operations", required=True)
    p.set_defaults(handler=cmd_run_ops)

    # -- billing -------------------------------------------------------------
    p = sub.add_parser("period-open", help="open a billing period")
    p.add_argument("--period-id", required=True)
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_period_open)

    p = sub.add_parser("price", help="register a supplier list price")
    p.add_argument("--period-id", required=True)
    p.add_argument("--bottle", required=True)
    p.add_argument("--price", required=True, type=int)
    p.add_argument("--on-day", type=int, default=None,
                   help="specific day the price applies to; default = all days")
    p.set_defaults(handler=cmd_price)

    p = sub.add_parser("voucher", help="issue a named point voucher")
    p.add_argument("--document", required=True, help="JSON document path")
    p.set_defaults(handler=cmd_voucher)

    p = sub.add_parser("vouchers", help="list vouchers with lock state")
    p.set_defaults(handler=cmd_vouchers)

    p = sub.add_parser("credentials", help="list true-usage credentials of a period")
    p.add_argument("--period-id", required=True)
    p.set_defaults(handler=cmd_credentials)

    p = sub.add_parser("bill", help="issue (or replay) the bill for a period")
    p.add_argument("--period-id", required=True)
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_bill)

    p = sub.add_parser("bill-get", help="read the active bill of a period")
    p.add_argument("--period-id", required=True)
    p.set_defaults(handler=cmd_bill_get)

    p = sub.add_parser("bill-close", help="freeze a period's write-offs")
    p.add_argument("--period-id", required=True)
    p.set_defaults(handler=cmd_bill_close)

    p = sub.add_parser("period-reopen", help="supersede an open period's bill")
    p.add_argument("--period-id", required=True)
    p.set_defaults(handler=cmd_period_reopen)

    p = sub.add_parser("settlement", help="supplier receivable and counter nets")
    p.add_argument("--period-id", required=True)
    p.set_defaults(handler=cmd_settlement)

    p = sub.add_parser("clear", help="minimum-fee remittance clearing")
    p.add_argument("--document", required=True,
                   help="JSON with balances and routes")
    p.set_defaults(handler=cmd_clear)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except ReagentError as exc:
        _emit({"error_type": exc.error_type, "error": str(exc)})
        return 2
    _emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
