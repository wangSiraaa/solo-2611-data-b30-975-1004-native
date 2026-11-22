"""Small JSON/HTTP helpers shared by the two local services."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable

Route = Callable[[dict[str, Any]], tuple[int, Any]]


class JsonHandler(BaseHTTPRequestHandler):
    routes: dict[str, Route] = {}

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        return

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._dispatch({})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            self._send(400, {"error": f"bad JSON: {exc}"})
            return
        if not isinstance(body, dict):
            self._send(400, {"error": "body must be a JSON object"})
            return
        self._dispatch(body)

    def _dispatch(self, body: dict[str, Any]) -> None:
        route = self.routes.get(self.path.split("?")[0])
        if route is None:
            self._send(404, {"error": f"no route {self.path!r}"})
            return
        try:
            status, payload = route(body)
        except Exception as exc:  # noqa: BLE001 - surface as JSON error
            self._send(400, {"error": type(exc).__name__, "message": str(exc)})
            return
        self._send(status, payload)
