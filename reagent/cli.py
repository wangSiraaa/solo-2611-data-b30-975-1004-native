"""Independent CLI entry points for inventory and run orchestration."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading

from .errors import ReagentError
from .inventory import InventoryService
from .deposits import DepositService
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


# -- deposit / clearance / shipping -----------------------------------------
def cmd_credential(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = DepositService(conn).credential(args.bottle)
    conn.close()
    return result


def cmd_explain_voucher(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = DepositService(conn).explain_voucher(args.bottle, args.day)
    conn.close()
    return result


def cmd_recount(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = DepositService(conn).recount(args.bottle, args.day)
    conn.close()
    return result


def cmd_open_clearance(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = DepositService(conn).open_clearance(
        args.bottle, args.portions, args.day
    )
    conn.close()
    return result


def cmd_sign_clearance(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = DepositService(conn).sign_clearance(
        args.request_id,
        args.counter_id,
        args.day,
        inventory_version=args.inventory_version,
        portions=args.portions,
    )
    conn.close()
    return result


def cmd_get_clearance(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = DepositService(conn).clearance_view(args.request_id)
    conn.close()
    return result


def cmd_apply_voucher(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = DepositService(conn).apply_voucher(args.bottle, args.day)
    conn.close()
    return result


def cmd_add_trip(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = DepositService(conn).add_trip(
        args.trip_id, args.departure_day, args.arrival_day, args.max_boxes
    )
    conn.close()
    return result


def cmd_add_box(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = DepositService(conn).add_box_model(
        args.model_id, args.slots, args.kind
    )
    conn.close()
    return result


def cmd_add_offer(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = DepositService(conn).add_trip_offer(
        args.trip_id, args.model_id, args.price
    )
    conn.close()
    return result


def cmd_add_supplier_kind(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = DepositService(conn).add_supplier_kind(args.supplier, args.kind)
    conn.close()
    return result


def cmd_plan(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = DepositService(conn).plan_shipment(
        args.day, args.max_bottles, args.max_trips
    )
    conn.close()
    return result


def cmd_ship(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = DepositService(conn).ship(
        args.day, args.shipment_id, args.max_bottles, args.max_trips
    )
    conn.close()
    return result


def cmd_get_shipment(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = DepositService(conn).shipment_view(args.shipment_id)
    conn.close()
    return result


def cmd_vouchers(args: argparse.Namespace) -> dict:
    conn = connect(args.db)
    result = DepositService(conn).vouchers(args.status)
    conn.close()
    return {"vouchers": result}


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
    deposits = DepositService(conn)
    results: list[dict] = []
    for op in document.get("operations", []):
        results.append(_execute_one(conn, inventory, runs, deposits, op))
    return results


def _execute_one(
    conn: sqlite3.Connection,
    inventory: InventoryService,
    runs: RunService,
    deposits: DepositService,
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
    if name == "credential":
        return {"op": name, "result": deposits.credential(op["bottle_id"])}
    if name == "explain_voucher":
        return {
            "op": name,
            "result": deposits.explain_voucher(op["bottle_id"], op["day"]),
        }
    if name == "recount":
        return {
            "op": name,
            "result": deposits.recount(op["bottle_id"], op["day"]),
        }
    if name == "open_clearance":
        return {
            "op": name,
            "result": deposits.open_clearance(
                op["bottle_id"], op["portions"], op["day"]
            ),
        }
    if name == "sign_clearance":
        return {
            "op": name,
            "result": deposits.sign_clearance(
                op["request_id"],
                op["counter_id"],
                op["day"],
                inventory_version=op.get("inventory_version"),
                portions=op.get("portions"),
            ),
        }
    if name == "get_clearance":
        return {
            "op": name,
            "result": deposits.clearance_view(op["request_id"]),
        }
    if name == "apply_voucher":
        return {
            "op": name,
            "result": deposits.apply_voucher(op["bottle_id"], op["day"]),
        }
    if name == "add_trip":
        return {
            "op": name,
            "result": deposits.add_trip(
                op["trip_id"],
                op["departure_day"],
                op["arrival_day"],
                op["max_boxes"],
            ),
        }
    if name == "add_box":
        return {
            "op": name,
            "result": deposits.add_box_model(
                op["model_id"], op["slots"], op["kinds"]
            ),
        }
    if name == "add_offer":
        return {
            "op": name,
            "result": deposits.add_trip_offer(
                op["trip_id"], op["model_id"], op["price"]
            ),
        }
    if name == "add_supplier_kind":
        return {
            "op": name,
            "result": deposits.add_supplier_kind(op["supplier"], op["kind"]),
        }
    if name == "plan":
        return {
            "op": name,
            "result": deposits.plan_shipment(
                op["day"],
                op.get("max_bottles", 18),
                op.get("max_trips", 3),
            ),
        }
    if name == "ship":
        return {
            "op": name,
            "result": deposits.ship(
                op["day"],
                op.get("shipment_id"),
                op.get("max_bottles", 18),
                op.get("max_trips", 3),
            ),
        }
    if name == "shipment":
        return {
            "op": name,
            "result": deposits.shipment_view(op["shipment_id"]),
        }
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

    # -- deposit / clearance / shipping -----------------------------------
    p = sub.add_parser("credential", help="show a bottle's supply credential")
    p.add_argument("--bottle", required=True)
    p.set_defaults(handler=cmd_credential)

    p = sub.add_parser("explain-voucher",
                       help="explain why a bottle is (not) refund-eligible")
    p.add_argument("--bottle", required=True)
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_explain_voucher)

    p = sub.add_parser("recount", help="bump a bottle's stocktake version")
    p.add_argument("--bottle", required=True)
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_recount)

    p = sub.add_parser("open-clearance",
                       help="open physical write-off of expired unallocated stock")
    p.add_argument("--bottle", required=True)
    p.add_argument("--portions", required=True, type=int)
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_open_clearance)

    p = sub.add_parser("sign-clearance", help="record one counter's clearance sign-off")
    p.add_argument("--request-id", required=True)
    p.add_argument("--counter-id", required=True)
    p.add_argument("--day", required=True, type=int)
    p.add_argument("--inventory-version", type=int, default=None)
    p.add_argument("--portions", type=int, default=None)
    p.set_defaults(handler=cmd_sign_clearance)

    p = sub.add_parser("get-clearance", help="show a clearance request")
    p.add_argument("--request-id", required=True)
    p.set_defaults(handler=cmd_get_clearance)

    p = sub.add_parser("apply-voucher", help="issue the refund voucher for a bottle")
    p.add_argument("--bottle", required=True)
    p.add_argument("--day", required=True, type=int)
    p.set_defaults(handler=cmd_apply_voucher)

    p = sub.add_parser("add-trip", help="register a recycling-station trip")
    p.add_argument("--trip-id", required=True)
    p.add_argument("--departure-day", required=True, type=int)
    p.add_argument("--arrival-day", required=True, type=int)
    p.add_argument("--max-boxes", required=True, type=int)
    p.set_defaults(handler=cmd_add_trip)

    p = sub.add_parser("add-box", help="register a box model")
    p.add_argument("--model-id", required=True)
    p.add_argument("--slots", required=True, type=int)
    p.add_argument("--kind", action="append", required=True,
                   help="allowed bottle kind; repeat per kind")
    p.set_defaults(handler=cmd_add_box)

    p = sub.add_parser("add-offer", help="price a box model on a trip")
    p.add_argument("--trip-id", required=True)
    p.add_argument("--model-id", required=True)
    p.add_argument("--price", required=True, type=int)
    p.set_defaults(handler=cmd_add_offer)

    p = sub.add_parser("add-supplier-kind",
                       help="declare a supplier accepts a bottle kind")
    p.add_argument("--supplier", required=True)
    p.add_argument("--kind", required=True)
    p.set_defaults(handler=cmd_add_supplier_kind)

    p = sub.add_parser("plan", help="compute the optimal loading without shipping")
    p.add_argument("--day", required=True, type=int)
    p.add_argument("--max-bottles", type=int, default=18)
    p.add_argument("--max-trips", type=int, default=3)
    p.set_defaults(handler=cmd_plan)

    p = sub.add_parser("ship", help="persist the optimal shipment")
    p.add_argument("--day", required=True, type=int)
    p.add_argument("--shipment-id", default=None)
    p.add_argument("--max-bottles", type=int, default=18)
    p.add_argument("--max-trips", type=int, default=3)
    p.set_defaults(handler=cmd_ship)

    p = sub.add_parser("get-shipment", help="show a shipment receipt")
    p.add_argument("--shipment-id", required=True)
    p.set_defaults(handler=cmd_get_shipment)

    p = sub.add_parser("vouchers", help="list refund vouchers")
    p.add_argument("--status", default=None, choices=["open", "shipped"])
    p.set_defaults(handler=cmd_vouchers)

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
