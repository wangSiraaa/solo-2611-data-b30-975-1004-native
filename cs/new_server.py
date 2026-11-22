#!/usr/bin/env python3
"""Local new-system service: qualification books + new-hall issuance.

It owns its reservation database and, when configured, speaks to a running old
hall over HTTP (never in a shared transaction).  Routes cover branches, cuts,
gates, books, day quotas and issuance, plus a restart-safe request log.

Run::

    python3 -m cs.new_server --db /tmp/new.db --port 8102 \
        --legacy-url http://127.0.0.1:8101
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any, Callable

from .http_common import JsonHandler
from .reservation_store import ReservationStore


def _client(url: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:  # pragma: no cover - network failure
        raise RuntimeError(f"old hall unreachable: {exc}") from exc


def build_routes(
    store: ReservationStore,
    legacy_url: str | None,
    legacy_hall_id: str = "OLD",
) -> dict[str, Callable[[dict[str, Any]], tuple[int, Any]]]:
    def reconcile(ticket_id: str, service_day: str) -> dict[str, Any] | None:
        if not legacy_url:
            return None
        return _client(
            legacy_url,
            "/admin/locate",
            {"ticket_id": ticket_id, "service_day": service_day},
        )

    def branch(body):
        return 200, store.add_branch(
            str(body["branch_id"]),
            list(body["classes"]),
            name=body.get("name"),
        )

    def branches(body):
        return 200, {"branches": store.list_branches()}

    def window(body):
        return 200, store.add_window(
            str(body["window_id"]), str(body["branch_id"]), str(body["hall_id"])
        )

    def cut(body):
        return 200, store.submit_qualification(
            body["bundle"], request_id=str(body["request_id"]), at=body.get("at")
        )

    def gate(body):
        return 200, store.apply_gate(
            branch_id=str(body["branch_id"]),
            kind=str(body["kind"]),
            ticket_id=str(body["ticket_id"]),
            justification=str(body["justification"]),
            request_id=str(body["request_id"]),
            at=body.get("at"),
            basis=body.get("basis"),
            position=str(body.get("position", "back")),
            entered_shift=body.get("entered_shift"),
            entered_seq=body.get("entered_seq"),
        )

    def queue(body):
        return 200, store.live(str(body["branch_id"]))

    def snapshot(body):
        return 200, store.snapshot(
            str(body["branch_id"]), int(body["area_seq"])
        )

    def open_book(body):
        return 200, store.open_book(
            branch_id=str(body["branch_id"]),
            opened_by=str(body.get("opened_by", "qualification-office")),
            request_id=str(body["request_id"]),
            ticket_ids=body.get("ticket_ids"),
            limit=body.get("limit"),
            at=body.get("at"),
        )

    def reopen_book(body):
        return 200, store.reopen_book(
            str(body["book_id"]), by=str(body.get("by", "qualification-office"))
        )

    def close_book(body):
        return 200, store.close_book(str(body["book_id"]))

    def books(body):
        return 200, {"books": store.books(body.get("branch_id"))}

    def open_day(body):
        return 200, store.open_day(
            str(body["branch_id"]),
            str(body["service_day"]),
            body.get("capacity"),
        )

    def set_quota(body):
        return 200, store.set_day_quota(
            str(body["branch_id"]), str(body["service_day"]), int(body["capacity"])
        )

    def close_day(body):
        return 200, store.close_day(str(body["branch_id"]), str(body["service_day"]))

    def day(body):
        return 200, store.day_status(str(body["branch_id"]), str(body["service_day"]))

    def issue(body):
        result = store.issue(
            request_id=str(body["request_id"]),
            window_id=str(body["window_id"]),
            service_day=str(body["service_day"]),
            ticket_id=body.get("ticket_id"),
            reconcile=reconcile,
        )
        if result.get("status") == "issued" and legacy_url:
            # Mirror the new ticket into the old hall so a late old retry for
            # the same person is refused.  Best-effort and idempotent.
            try:
                _client(
                    legacy_url,
                    "/admin/import",
                    {
                        "source": "new",
                        "external_id": result["request_id"],
                        "hall_id": str(body.get("hall_id", "NEW")),
                        "service_day": str(body["service_day"]),
                        "ticket_no": result["ticket_no"],
                        "ticket_id": result["ticket_id"],
                        "window_id": str(body["window_id"]),
                    },
                )
            except RuntimeError:
                pass
        return 200, result

    def request(body):
        return 200, store.request(str(body["request_id"])) or {"status": "unknown"}

    def window_summary(body):
        return 200, store.window_summary(
            str(body["window_id"]), str(body["service_day"])
        )

    def tickets(body):
        return 200, {
            "tickets": store.tickets_for(
                str(body["branch_id"]), str(body["service_day"])
            )
        }

    def redeem(body):
        return 200, store.redeem_seat(
            branch_id=str(body["branch_id"]),
            service_day=str(body["service_day"]),
            ticket_id=str(body["ticket_id"]),
            window_id=str(body.get("window_id", "prebooked")),
            source=str(body.get("source", "prebooked")),
        )

    def health(body):
        return 200, {
            "ok": True,
            "service": "new-system",
            "legacy_url": legacy_url,
            "legacy_hall_id": legacy_hall_id,
        }

    return {
        "/branch": branch,
        "/branches": branches,
        "/window": window,
        "/cut": cut,
        "/gate": gate,
        "/queue": queue,
        "/snapshot": snapshot,
        "/book/open": open_book,
        "/book/reopen": reopen_book,
        "/book/close": close_book,
        "/books": books,
        "/day/open": open_day,
        "/day/set-quota": set_quota,
        "/day/close": close_day,
        "/day": day,
        "/issue": issue,
        "/request": request,
        "/window/summary": window_summary,
        "/tickets": tickets,
        "/redeem": redeem,
        "/health": health,
    }


def serve(
    db_path: str, port: int, legacy_url: str | None = None
) -> ThreadingHTTPServer:
    store = ReservationStore.open_reservation(db_path)
    routes = build_routes(store, legacy_url)
    handler = type("NewHandler", (JsonHandler,), {"routes": routes})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local new-system service")
    parser.add_argument("--db", required=True)
    parser.add_argument("--port", type=int, default=8102)
    parser.add_argument("--legacy-url", default=None)
    args = parser.parse_args(argv)
    server = serve(args.db, args.port, args.legacy_url)
    print(f"new system listening on http://127.0.0.1:{args.port} (db={args.db})",
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
