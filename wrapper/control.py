"""The local control port. HTTP plumbing only — every decision is in `store.WrapperState`.

`wrapper_return_path_plan.md` Phase 1 · W3. Ported from `startRendezvousServer`
(`channel/server.ts:905`).

**Loopback only, on an ephemeral port.** The rendezvous file is how anyone finds it, so
there is no fixed number to collide with when fifteen of these are running, and nothing
outside this machine can reach it whatever the network thinks it is doing.

The socket is bound in `__init__` and served in `serve()`, and those are two calls rather
than one because the port has to be known before the child is spawned while the serving
thread must not exist until after — see the ordering note in `__main__.py`.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from wrapper.store import debug

# A local request body is small and comes from a process on this machine, but "local" is not
# "trusted" — this is a cap on what a stray script can make us allocate.
MAX_BODY_BYTES = 256 * 1024


class ControlServer:
    def __init__(self, host: str = "127.0.0.1"):
        self._state = None
        self._server = ThreadingHTTPServer((host, 0), _make_handler(self))
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def state(self):
        return self._state

    def serve(self, state) -> None:
        self._state = state
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="audiochatty-control", daemon=True
        )
        self._thread.start()
        debug(f"control port listening on 127.0.0.1:{self.port}")

    def close(self) -> None:
        try:
            self._server.shutdown()
        except Exception:
            pass
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)


def _make_handler(control: ControlServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "audiochatty-wrapper/0.1.0"

        def log_message(self, fmt, *args):
            # The default writes to stderr, which is the user's terminal. Silent unless
            # somebody asked.
            debug("control %s" % (fmt % args))

        # -- routes --

        def do_GET(self):  # noqa: N802
            if self.path.split("?", 1)[0] == "/status":
                self._reply(*self._state_call("status"))
            else:
                self._reply(404, {"error": "not_found"})

        def do_POST(self):  # noqa: N802
            route = self.path.split("?", 1)[0]
            if route not in ("/bind", "/unbind", "/inject"):
                self._reply(404, {"error": "not_found"})
                return
            body, ok = self._read_json()
            if not ok:
                self._reply(400, {"error": "invalid_json"})
                return
            self._reply(*self._state_call(route.lstrip("/"), body))

        # -- plumbing --

        def _state_call(self, name: str, *args) -> tuple[int, dict]:
            state = control.state
            if state is None:  # pragma: no cover - the window before `serve()`
                return 503, {"error": "not_ready"}
            try:
                return getattr(state, name)(*args)
            except Exception as err:  # a 500 on the control port must not kill the wrapper
                debug(f"{name} failed: {err!r}")
                return 500, {"error": "internal"}

        def _read_json(self) -> tuple[dict, bool]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return {}, False
            if length < 0 or length > MAX_BODY_BYTES:
                return {}, False
            if length == 0:
                return {}, True  # an empty body is fine for /unbind
            try:
                parsed = json.loads(self.rfile.read(length).decode("utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                return {}, False
            return (parsed, True) if isinstance(parsed, dict) else ({}, False)

        def _reply(self, status: int, body: dict) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler
