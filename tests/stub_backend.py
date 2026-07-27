"""A stub AudioChat backend, so the plugin's tests run with nothing installed.

`coding_agent_build_plan.md` Phase 5 asks for tests that run without Claude Code. This is
the other half of that: tests that run without Flask, without Supabase, and without a
network. It speaks only the six routes the plugin knows about, and it records every
request so a test can assert on what was sent rather than only on what came back.

It deliberately does *not* validate bodies the way the real backend does. The real
backend's normalisation is tested in `audiochat-backend/tests/test_agent_routes.py`;
duplicating it here would only prove the two fakes agree with each other.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class StubBackend:
    """Start with `with StubBackend() as backend:` and point the plugin at `backend.url`.

    Responses are queued per path: `backend.reply("/agent/turn", 202, {...})`. A path with
    nothing queued gets its default. A queued `delay` sleeps before answering, which is
    how the timeout path is tested without waiting on a real network.
    """

    def __init__(self):
        self.requests: list[dict] = []
        self._queued: dict[str, list[dict]] = {}
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # -- lifecycle --

    def __enter__(self) -> "StubBackend":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    # -- scripting --

    def reply(self, path: str, status: int, body: dict | None = None, delay: float = 0.0) -> None:
        """Queue one response for `path`. Queued responses are consumed in order, which is
        what lets a test drive the pairing poll through pending → approved."""
        with self._lock:
            self._queued.setdefault(path, []).append(
                {"status": status, "body": body or {}, "delay": delay}
            )

    def next_reply(self, path: str) -> dict | None:
        with self._lock:
            queue = self._queued.get(path)
            if queue:
                return queue.pop(0)
        return None

    def record(self, entry: dict) -> None:
        with self._lock:
            self.requests.append(entry)

    # -- assertions --

    def requests_to(self, path: str) -> list[dict]:
        with self._lock:
            return [r for r in self.requests if r["path"] == path]

    def last_request(self, path: str) -> dict | None:
        matching = self.requests_to(path)
        return matching[-1] if matching else None


DEFAULTS: dict[str, tuple[int, dict]] = {
    "/device/code": (
        200,
        {
            "device_code": "stub-device-code",
            "user_code": "WXYZ-1234",
            "verification_uri": "http://localhost:3000/link",
            "interval": 1,
            "expires_in": 600,
        },
    ),
    "/device/token": (
        200,
        {
            "token": "stub-device-token",
            "device_id": "11111111-1111-1111-1111-111111111111",
            "workspace_id": "22222222-2222-2222-2222-222222222222",
            "workspace_name": "Mike's Workspace",
            "profile_name": "Mike",
            "label": "test-machine",
        },
    ),
    "/agent/session": (
        200,
        {
            "session_id": "33333333-3333-3333-3333-333333333333",
            "name": "billing-refactor",
            "status": "active",
        },
    ),
    "/agent/session/end": (200, {"session_id": "33333333-3333-3333-3333-333333333333",
                                 "status": "ended"}),
    "/agent/turn": (202, {"status": "queued", "job_id": "44444444-4444-4444-4444-444444444444"}),
}


def _make_handler(backend: StubBackend):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's naming
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                body = {}

            backend.record(
                {
                    "path": self.path,
                    "body": body,
                    "authorization": self.headers.get("Authorization"),
                    "user_agent": self.headers.get("User-Agent"),
                }
            )

            queued = backend.next_reply(self.path)
            if queued:
                if queued["delay"]:
                    import time

                    time.sleep(queued["delay"])
                self._send(queued["status"], queued["body"])
                return

            status, payload = DEFAULTS.get(self.path, (404, {"error": "not found"}))
            self._send(status, payload)

        def _send(self, status: int, payload: dict) -> None:
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args) -> None:
            """Silence. The test output is the assertions."""

    return Handler
