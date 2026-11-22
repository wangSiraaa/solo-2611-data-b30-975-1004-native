#!/usr/bin/env python3
"""Local old hall service (legacy contract).

The frozen old client sends only: window id, hall/branch, service day and a
stable request id.  This server derives the person from the request id and
answers ``reserve`` / ``lookup``; the same request id returns the original
ticket.  It also exposes server-side lookup/intake for the migration, which the
old client never calls.

Run::

    python3 -m cs.legacy_server --db /tmp/legacy.db --port 8101
"""

from __future__ import annotations

import argparse
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .http_common import JsonHandler
from .legacy import LegacyHall


def _int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc


def build_routes(hall: LegacyHall, people: dict[str, str]) -> dict:
    """``people`` maps a request id to the person (ticket id) the frozen client
    could not send.  It is a server-side simulation of the old intake."""

    def person_for(body: dict[str, Any]) -> str:
        request_id = body.get("request_id")
        if not request_id:
            raise ValueError("request_id is required")
        ticket_id = body.get("ticket_id") or people.get(str(request_id))
        if not ticket_id:
            # Cross-restart retransmit: recover the person from the durable
            # request log, since the frozen client never sends a ticket id.
            row = hall.conn.execute(
                "SELECT ticket_id FROM lh_requests WHERE request_id=?",
                (str(request_id),),
            ).fetchone()
            if row is not None:
                return str(row["ticket_id"])
        if not ticket_id:
            raise ValueError(
                f"request {request_id!r} has no known person; "
                "register it with /admin/person"
            )
        return str(ticket_id)

    def reserve(body: dict[str, Any]) -> tuple[int, Any]:
        request_id = str(body["request_id"])
        result = hall.reserve(
            request_id=request_id,
            window_id=str(body.get("window_id", "old-w1")),
            hall_id=str(body.get("hall_id", body.get("branch_id", "OLD"))),
            service_day=str(body["service_day"]),
            ticket_id=person_for(body),
            capacity=(
                _int(body["capacity"], "capacity")
                if body.get("capacity") is not None
                else None
            ),
        )
        return 200, result

    def lookup(body: dict[str, Any]) -> tuple[int, Any]:
        request_id = body.get("request_id")
        if not request_id:
            raise ValueError("request_id is required")
        return 200, hall.lookup(str(request_id))

    def locate(body: dict[str, Any]) -> tuple[int, Any]:
        return 200, hall.locate_person(
            str(body["ticket_id"]), str(body["service_day"])
        )

    def import_ticket(body: dict[str, Any]) -> tuple[int, Any]:
        return 200, hall.import_ticket(
            source=str(body.get("source", "new")),
            external_id=str(body["external_id"]),
            hall_id=str(body.get("hall_id", "OLD")),
            service_day=str(body["service_day"]),
            ticket_no=str(body["ticket_no"]),
            ticket_id=str(body["ticket_id"]),
            window_id=str(body.get("window_id", "old-w1")),
        )

    def open_day(body: dict[str, Any]) -> tuple[int, Any]:
        return 200, hall.open_day(
            str(body["hall_id"]),
            str(body["service_day"]),
            _int(body["capacity"], "capacity"),
        )

    def day(body: dict[str, Any]) -> tuple[int, Any]:
        return 200, hall.day_status(str(body["hall_id"]), str(body["service_day"]))

    def tickets(body: dict[str, Any]) -> tuple[int, Any]:
        return 200, {
            "tickets": hall.tickets_for(str(body["hall_id"]), str(body["service_day"]))
        }

    def requests(body: dict[str, Any]) -> tuple[int, Any]:
        return 200, {
            "requests": hall.requests_for(
                str(body["hall_id"]), str(body["service_day"])
            )
        }

    def register_person(body: dict[str, Any]) -> tuple[int, Any]:
        people[str(body["request_id"])] = str(body["ticket_id"])
        return 200, {"request_id": body["request_id"], "ticket_id": body["ticket_id"]}

    def health(body: dict[str, Any]) -> tuple[int, Any]:
        return 200, {"ok": True, "service": "legacy-hall"}

    return {
        "/reserve": reserve,
        "/lookup": lookup,
        "/admin/locate": locate,
        "/admin/import": import_ticket,
        "/admin/open-day": open_day,
        "/admin/day": day,
        "/admin/tickets": tickets,
        "/admin/requests": requests,
        "/admin/person": register_person,
        "/health": health,
    }


def serve(db_path: str, port: int, people: dict[str, str] | None = None) -> ThreadingHTTPServer:
    hall = LegacyHall.open(db_path)
    routes = build_routes(hall, people if people is not None else {})
    handler = type("LegacyHandler", (JsonHandler,), {"routes": routes})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local old hall service")
    parser.add_argument("--db", required=True)
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument(
        "--people",
        help="JSON file mapping request_id -> ticket_id (server-side intake)",
    )
    args = parser.parse_args(argv)
    people: dict[str, str] = {}
    if args.people:
        people = json.loads(Path(args.people).read_text(encoding="utf-8"))
    server = serve(args.db, args.port, people)
    print(f"legacy hall listening on http://127.0.0.1:{args.port} (db={args.db})",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
