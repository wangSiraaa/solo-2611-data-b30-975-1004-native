#!/usr/bin/env python3
"""Command line interface for the classification catalog and fact store."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .store import (
    CatalogError,
    ConflictError,
    FactError,
    ProductError,
    Store,
    StoreError,
)


def _dump(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise StoreError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
    return out


def _open(args: argparse.Namespace) -> Store:
    return Store.open(args.db)


def cmd_init(args: argparse.Namespace) -> int:
    data = _read_json(args.catalog)
    with _open(args) as store:
        state = store.init_catalog(data, request_id=args.request_id)
        # Optionally seed facts from a JSONL file in one atomic commit.
        if args.facts:
            rows = _read_jsonl(args.facts)
            facts = [
                {
                    "ticket_id": r["ticket_id"],
                    "revision": int(r["revision"]),
                    "amount": int(r["amount"]),
                    "classes": list(r.get("classes", [])),
                }
                for r in rows
            ]
            committed = store.apply_fact_revisions(facts, request_id=args.fact_request_id)
            _dump(
                {
                    "catalog_revision": state.revision,
                    "facts_applied": len(committed.facts),
                    "cut": committed.cut.as_dict(),
                }
            )
        else:
            _dump({"catalog_revision": state.revision, "cut": store.current_cut().as_dict()})
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    operation = _read_json(args.operation)
    with _open(args) as store:
        committed = store.submit(operation)
        _dump(committed.as_dict())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Apply an operations JSONL file in order, stopping at the first reject."""

    results = []
    with _open(args) as store:
        for index, operation in enumerate(_read_jsonl(args.operations), 1):
            request_id = operation.get("request_id")
            try:
                committed = store.submit(operation)
                results.append(
                    {
                        "line": index,
                        "request_id": request_id,
                        "accepted": True,
                        "result": committed.as_dict(),
                    }
                )
            except StoreError as exc:
                results.append(
                    {
                        "line": index,
                        "request_id": request_id,
                        "accepted": False,
                        "error": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                if args.stop_on_error:
                    break
        record = {
            "operations": results,
            "cut": store.current_cut().as_dict(),
        }
        if args.out:
            Path(args.out).write_text(
                json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        _dump(record)
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    targets = args.targets
    if args.queries:
        data = _read_json(args.queries)
        requests = data.get("requests", data if isinstance(data, list) else [])
        all_results = []
        with _open(args) as store:
            # One query run = one committed cut.  All named requests in this
            # file (and any export) are pinned to the same cut, so results are
            # never stitched together from different points in time.
            pinned = store.current_cut()
            for request in requests:
                result = store.query(
                    request["targets"], name=request.get("name"), snapshot=pinned
                )
                if args.export:
                    payload = {
                        "format": "classification-summary/1",
                        "cut": result["cut"],
                        "groups": result["groups"],
                        "union": result["union"],
                    }
                    if request.get("name") is not None:
                        payload["name"] = request["name"]
                    out_path = Path(args.export) / f"{request.get('name') or 'query'}.json"
                    out_path.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                        + "\n",
                        encoding="utf-8",
                    )
                all_results.append(result)
        _dump({"results": all_results})
        return 0
    with _open(args) as store:
        result = store.query(targets, name=args.name)
        if args.export:
            payload = {
                "format": "classification-summary/1",
                "cut": result["cut"],
                "groups": result["groups"],
                "union": result["union"],
            }
            if args.name is not None:
                payload["name"] = args.name
            Path(args.export).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        _dump(result)
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    with _open(args) as store:
        result = store.publish(
            args.targets, name=args.name, product_id=args.product_id
        )
        out = Path(args.out) if args.out else None
        if out:
            out.write_text(
                json.dumps(result["payload"], ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
        _dump(
            {
                "product_id": result["product_id"],
                "created_at": result["created_at"],
                "cut": result["payload"]["cut"],
                "out": str(out) if out else None,
            }
        )
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    classes = args.classes
    with _open(args) as store:
        result = store.explain_target(args.target, classes)
        _dump(result)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with _open(args) as store:
        if args.product:
            _dump(store.get_product(args.product))
            return 0
        _dump(
            {
                "cut": store.current_cut().as_dict(),
                "catalog": store.load_catalog_state().as_json(),
                "ancestors": {
                    nid: store.ancestors(nid)
                    for nid in sorted(store.load_catalog_state().nodes)
                },
                "descendants": {
                    nid: store.descendants(nid)
                    for nid in sorted(store.load_catalog_state().nodes)
                },
                "products": store.list_products(),
                "registers": store.list_registers(),
            }
        )
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    with _open(args) as store:
        _dump({"history": store.history(), "cut": store.current_cut().as_dict()})
    return 0


def _load_ballot_file(path: str) -> dict[str, Any]:
    """Read a settlement request: proposals, target, pairs, costs, order."""

    data = _read_json(path)
    if not isinstance(data, dict):
        raise StoreError(f"{path}: expected a JSON object")
    return data


def _pairs_from_request(data: dict[str, Any]) -> list[list[str]]:
    pairs = data.get("pairs", [])
    out: list[list[str]] = []
    for raw in pairs:
        if isinstance(raw, dict):
            out.append([raw["left"], raw["right"]])
        else:
            out.append([raw[0], raw[1]])
    return out


def cmd_register(args: argparse.Namespace) -> int:
    data = _load_ballot_file(args.request)
    proposals = data.get("proposals") or data.get("targets")
    if not proposals:
        raise StoreError("register request needs 'proposals' or 'targets'")
    pairs = _pairs_from_request(data)
    with _open(args) as store:
        result = store.publish_register(
            proposals,
            pairs,
            name=args.name or data.get("name"),
            register_id=args.register_id,
        )
        if args.out:
            Path(args.out).write_text(
                json.dumps(result["payload"], ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
        _dump(
            {
                "register_id": result["register_id"],
                "created_at": result["created_at"],
                "cut": result["payload"]["cut"],
                "out": args.out,
            }
        )
    return 0


def cmd_show_register(args: argparse.Namespace) -> int:
    with _open(args) as store:
        _dump(store.get_register(args.register))
    return 0


def cmd_settle(args: argparse.Namespace) -> int:
    data = _load_ballot_file(args.request)
    target = data.get("target")
    proposals = data.get("proposals") or data.get("targets")
    if not target or not proposals:
        raise StoreError("settlement request needs 'target' and 'proposals'")
    pairs = _pairs_from_request(data)

    def cost_key(a: str, b: str) -> str:
        return f"{a}|{b}" if a < b else f"{b}|{a}"

    costs: dict[str, int] = {}
    forbidden: list[list[str]] = []
    for raw in data.get("pairs", []):
        if not isinstance(raw, dict):
            continue
        a, b = str(raw["left"]), str(raw["right"])
        key = cost_key(a, b)
        explicitly_forbidden = bool(raw.get("forbidden")) or (
            "cost" in raw and raw["cost"] is None
        )
        if explicitly_forbidden:
            forbidden.append([a, b])
        elif "cost" in raw:
            costs[key] = raw["cost"]
    for a, b in data.get("forbidden", []):
        forbidden.append([str(a), str(b)])
    for key, value in data.get("costs", {}).items():
        if isinstance(key, (list, tuple)) and len(key) == 2:
            costs[cost_key(str(key[0]), str(key[1]))] = value
        else:
            costs[str(key)] = value

    order = data.get("order") or data.get("chair_order")
    register = args.register or data.get("register")
    if not register:
        raise StoreError(
            "settlement must cite a frozen register (--register or 'register')"
        )

    with _open(args) as store:
        result = store.settle(
            target,
            proposals,
            pairs,
            costs=costs or None,
            forbidden=forbidden,
            order=order,
            register=register,
            name=args.name or data.get("name"),
            persist=args.persist,
        )
        if args.out:
            Path(args.out).write_text(
                json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        _dump(result)
    return 0


def cmd_show_settlement(args: argparse.Namespace) -> int:
    with _open(args) as store:
        _dump(store.get_settlement(args.settlement))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cs",
        description="Classification catalog, fact store, cut-pinned summaries.",
    )
    parser.add_argument("--db", default="classification.db", help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="initialize catalog and optional facts")
    p.add_argument("catalog", help="classification-catalog/1 JSON file")
    p.add_argument("--facts", help="facts JSONL file")
    p.add_argument("--request-id", default="init-catalog")
    p.add_argument("--fact-request-id", default="init-facts")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("submit", help="submit one operation JSON file")
    p.add_argument("operation")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("run", help="apply an operations JSONL file in order")
    p.add_argument("operations")
    p.add_argument("--out", help="write the full result JSON here")
    p.add_argument(
        "--stop-on-error",
        action="store_true",
        help="stop at the first rejected operation",
    )
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("query", help="run a multi-target query at one cut")
    p.add_argument("targets", nargs="*", help="target node ids")
    p.add_argument("--queries", help="queries.json with named requests")
    p.add_argument("--name", help="name for a single query")
    p.add_argument("--export", help="write classification-summary/1 JSON to a file or directory")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("publish", help="query and persist an immutable product")
    p.add_argument("targets", nargs="+")
    p.add_argument("--name")
    p.add_argument("--product-id")
    p.add_argument("--out", help="write the product payload to this file")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("explain", help="explain coverage for direct classes")
    p.add_argument("target")
    p.add_argument("classes", nargs="+")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("show", help="show current cut, catalog, products")
    p.add_argument("--product", help="show one stored product")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("history", help="show committed events")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("register", help="freeze a both-sides ballot register")
    p.add_argument("request", help="JSON with proposals and pairs")
    p.add_argument("--name")
    p.add_argument("--register-id")
    p.add_argument("--out", help="write the ballot-register/1 payload here")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("show-register", help="show a frozen register")
    p.add_argument("register")
    p.set_defaults(func=cmd_show_register)

    p = sub.add_parser("settle", help="cheapest legal agenda leaving a proposal")
    p.add_argument("request", help="JSON with target, proposals, pairs/costs/order")
    p.add_argument("--register", help="vote strictly from this frozen register id")
    p.add_argument("--name")
    p.add_argument("--persist", action="store_true", help="store the settlement")
    p.add_argument("--out", help="write the agenda-settlement/1 payload here")
    p.set_defaults(func=cmd_settle)

    p = sub.add_parser("show-settlement", help="show a stored settlement")
    p.add_argument("settlement")
    p.set_defaults(func=cmd_show_settlement)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (StoreError, CatalogError, FactError, ConflictError, ProductError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
