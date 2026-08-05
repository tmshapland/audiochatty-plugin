"""A stub audiochatty backend, so the plugin's tests run with nothing installed.

`coding_agent_build_plan.md` Phase 5 asks for tests that run without Claude Code. This is
the other half of that: tests that run without Flask, without Supabase, and without a
network. It speaks only the routes the plugin knows about, and it records every
request so a test can assert on what was sent rather than only on what came back.

`channel_return_path_plan.md` Phase 3 added the three return-path routes and, with them,
the first `GET` — so requests are keyed by path *without* the query string, and
`entry["query"]` carries the parsed parameters. Existing POST assertions are unaffected:
none of them has a query string to lose.

`voice_approval_plan.md` Phase 3 added the first routes with an **id in the path**, which
is why there is now a template alongside it: `/agent/question/abc/expire` records as
itself and also matches `/agent/question/:id/expire`, so a test can queue a reply or
assert on a route without first knowing the id the stub is about to invent.

It deliberately does *not* validate bodies the way the real backend does. The real
backend's normalisation is tested in `audiochat-backend/tests/test_agent_routes.py`;
duplicating it here would only prove the two fakes agree with each other.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


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
        """Every request to that path, by its literal path *or* its template — so
        `requests_to("/agent/question/:id")` finds the poll without knowing the id."""
        with self._lock:
            return [r for r in self.requests if path in (r["path"], r["template"])]

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
    # The return path. An empty queue is the ordinary answer here — the wrapper polls
    # forever — so the default is an empty list rather than a 404.
    "/agent/inbound": (200, {"messages": []}),
    "/agent/inbound/ack": (200, {"message_ids": []}),
    "/agent/session/verified": (200, {"status": "verified"}),
    # Voice approval. The default for a poll is `pending` on purpose: a test that wants an
    # answer queues one, and a test that wants the hold to run down queues nothing.
    "/agent/question": (202, {"question_id": "55555555-5555-5555-5555-555555555555",
                              "status": "pending"}),
    "/agent/question/:id": (200, {"question_id": "55555555-5555-5555-5555-555555555555",
                                  "status": "pending", "answer_option_id": None}),
    "/agent/question/:id/expire": (200, {"question_id": "55555555-5555-5555-5555-555555555555",
                                         "status": "expired"}),
}


def path_template(path: str) -> str:
    """`/agent/question/<uuid>/expire` → `/agent/question/:id/expire`.

    Only the question routes have an id in them, so this recognises those three rather
    than trying to be a router. Anything else is its own template, which is what keeps
    every existing `requests_to("/agent/turn")` assertion working unchanged.
    """
    parts = path.strip("/").split("/")
    if len(parts) == 3 and parts[:2] == ["agent", "question"]:
        return "/agent/question/:id"
    if len(parts) == 4 and parts[:2] == ["agent", "question"] and parts[3] in ("expire", "answer"):
        return f"/agent/question/:id/{parts[3]}"
    return path


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
            self._handle(body)

        def do_GET(self):  # noqa: N802
            self._handle({})

        def _handle(self, body: dict) -> None:
            split = urlsplit(self.path)
            path = split.path
            template = path_template(path)

            backend.record(
                {
                    "path": path,
                    "template": template,
                    "query": {k: v[0] for k, v in parse_qs(split.query).items()},
                    "body": body,
                    "method": self.command,
                    "authorization": self.headers.get("Authorization"),
                    "user_agent": self.headers.get("User-Agent"),
                }
            )

            # Literal path first, so a test can still script one specific id.
            queued = backend.next_reply(path) or backend.next_reply(template)
            if queued:
                if queued["delay"]:
                    import time

                    time.sleep(queued["delay"])
                self._send(queued["status"], queued["body"])
                return

            status, payload = DEFAULTS.get(
                path, DEFAULTS.get(template, (404, {"error": "not found"}))
            )
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
