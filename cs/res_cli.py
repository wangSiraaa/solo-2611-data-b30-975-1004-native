#!/usr/bin/env python3
"""Command line for the reservation subsystem (books and hall tickets).

Examples
--------
::

    python3 -m cs.res_cli --db res.db init
    python3 -m cs.res_cli --db res.db branch B --class B
    python3 -m cs.res_cli --db res.db seed --facts facts.jsonl --order X Y
    python3 -m cs.res_cli --db res.db queue B
    python3 -m cs.res_cli --db res.db book B --opened-by 资格处
    python3 -m cs.res_cli --db res.db day-open B 2026-11-22 --capacity 50
    python3 -m cs.res_cli --db res.db issue new-w 2026-11-22 --request-id R1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .reservation_store import ReservationStore
from .store import StoreError

CATALOG = {
    "format": "classification-catalog/1",
    "revision": "G1",
    "nodes": [
        {"id": "R", "name": "全部"},
        {"id": "A", "name": "办件"},
        {"id": "B", "name": "办件"},
        {"id": "L", "name": "交叉子类"},
    ],
    "edges": [["A", "R"], ["B", "R"], ["L", "A"], ["L", "B"]],
}


def _dump(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _store(args: argparse.Namespace) -> ReservationStore:
    store = ReservationStore.open_reservation(args.db)
    return store


def cmd_init(args: argparse.Namespace) -> int:
    catalog = (
        json.loads(Path(args.catalog).read_text(encoding="utf-8"))
        if args.catalog
        else CATALOG
    )
    with _store(args) as store:
        if not store.is_initialized():
            store.init_catalog(catalog)
        _dump({"catalog_revision": store.catalog_revision()})
    return 0


def cmd_branch(args: argparse.Namespace) -> int:
    with _store(args) as store:
        _dump(store.add_branch(args.branch_id, args.cls, name=args.name))
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    with _store(args) as store:
        _dump(
            store.seed(
                facts=_read_jsonl(args.facts),
                order=args.order,
                request_id=args.request_id,
            )
        )
    return 0


def cmd_cut(args: argparse.Namespace) -> int:
    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
    with _store(args) as store:
        _dump(store.submit_qualification(bundle, request_id=args.request_id, at=args.at))
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    with _store(args) as store:
        _dump(
            store.apply_gate(
                branch_id=args.branch_id,
                kind=args.kind,
                ticket_id=args.ticket_id,
                justification=args.justification,
                request_id=args.request_id,
                position=args.position,
                entered_shift=args.entered_shift,
                entered_seq=args.entered_seq,
            )
        )
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    with _store(args) as store:
        _dump(store.live(args.branch_id))
    return 0


def cmd_book(args: argparse.Namespace) -> int:
    with _store(args) as store:
        _dump(
            store.open_book(
                branch_id=args.branch_id,
                opened_by=args.opened_by,
                request_id=args.request_id,
                limit=args.limit,
            )
        )
    return 0


def cmd_book_reopen(args: argparse.Namespace) -> int:
    with _store(args) as store:
        _dump(store.reopen_book(args.book_id, by=args.by))
    return 0


def cmd_day_open(args: argparse.Namespace) -> int:
    with _store(args) as store:
        _dump(store.open_day(args.branch_id, args.service_day, args.capacity))
    return 0


def cmd_issue(args: argparse.Namespace) -> int:
    with _store(args) as store:
        _dump(
            store.issue(
                request_id=args.request_id,
                window_id=args.window_id,
                service_day=args.service_day,
                ticket_id=args.ticket_id,
            )
        )
    return 0


def cmd_window(args: argparse.Namespace) -> int:
    with _store(args) as store:
        _dump(store.window_summary(args.window_id, args.service_day))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cs.res_cli",
        description="Reservation books and hall-issued tickets.",
    )
    parser.add_argument("--db", default="reservation.db")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="initialize catalog (optionally from a file)")
    p.add_argument("--catalog")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("branch", help="create a branch managing classes")
    p.add_argument("branch_id")
    p.add_argument("--class", dest="cls", action="append", required=True)
    p.add_argument("--name")
    p.set_defaults(func=cmd_branch)

    p = sub.add_parser("seed", help="seed facts and arrive order")
    p.add_argument("--facts", required=True)
    p.add_argument("--order", nargs="+", required=True)
    p.add_argument("--request-id", default="seed")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("cut", help="submit a qualification cut from a JSON file")
    p.add_argument("bundle")
    p.add_argument("--request-id", required=True)
    p.add_argument("--at")
    p.set_defaults(func=cmd_cut)

    p = sub.add_parser("gate", help="record a pre-open appeal")
    p.add_argument("branch_id")
    p.add_argument("kind", choices=["insert", "drop", "restore", "correct", "admit"])
    p.add_argument("ticket_id")
    p.add_argument("--justification", required=True)
    p.add_argument("--request-id", required=True)
    p.add_argument("--position", default="back")
    p.add_argument("--entered-shift")
    p.add_argument("--entered-seq", type=int)
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("queue", help="show a branch's live waiting book")
    p.add_argument("branch_id")
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("book", help="sign out a waiting book")
    p.add_argument("branch_id")
    p.add_argument("--opened-by", default="资格处")
    p.add_argument("--request-id", required=True)
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_book)

    p = sub.add_parser("book-reopen", help="re-open a signed-out book")
    p.add_argument("book_id")
    p.add_argument("--by", default="资格处")
    p.set_defaults(func=cmd_book_reopen)

    p = sub.add_parser("day-open", help="open a branch service day")
    p.add_argument("branch_id")
    p.add_argument("service_day")
    p.add_argument("--capacity", type=int, required=True)
    p.set_defaults(func=cmd_day_open)

    p = sub.add_parser("issue", help="issue the next ticket from a window")
    p.add_argument("window_id")
    p.add_argument("service_day")
    p.add_argument("--request-id", required=True)
    p.add_argument("--ticket-id")
    p.set_defaults(func=cmd_issue)

    p = sub.add_parser("window", help="show a window's issued tickets and remainder")
    p.add_argument("window_id")
    p.add_argument("service_day")
    p.set_defaults(func=cmd_window)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except StoreError as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
