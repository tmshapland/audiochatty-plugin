"""The five slash commands, driven the way Claude Code drives them.

Every test runs `scripts/audiochat.py` as a subprocess against the stub backend, with
`AUDIOCHATTY_HOME` pointed at a temp directory — so the suite is safe to run on a machine
that is already paired, and what it asserts is what the command actually does rather than
what an imported function returns.

`wrapper_return_path_plan.md` Phase 3 replaced the channel with the wrapper, and this file
lost a fixture in the process. `FakeClaude` — a real process carrying a real command line —
existed because `connect` used to answer "which session am I, and were channels enabled?" by
reading `ps` output. Nothing reads `ps` any more: the answer arrives in an inherited
environment variable, so the only fixture left is `FakeWrapper`, a real loopback server with
a real rendezvous file, because `connect`'s job is still to find one of those and POST to it.

**`run_cli` always sets `AUDIOCHATTY_WRAPPER_*` explicitly, even to remove them.** Whoever
runs this suite may well be sitting inside a real `audiochatty run`, in which case those
variables are in the environment and point at their actual session. Inheriting them would
make the refusal tests pass against a live wrapper and, worse, POST `/bind` to it.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stub_backend import StubBackend  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
CLI = SCRIPTS / "audiochat.py"

RUN_COMMAND = "audiochatty run"


class FakeWrapper:
    """A rendezvous file plus a loopback server answering `/bind` and `/unbind`.

    The file is written the way `wrapper/store.py` writes it, including `kind` and
    `expected_session_id` — the second of those is what `find_wrapper` compares against to
    refuse a session that inherited these variables without owning them (W3).

    `pid` defaults to this test process, because `read_wrappers` requires the pid to be alive
    and to match the file name. `expected_session_id` defaults to `"sess-1"`, the id nearly
    every test connects with, so the default fixture is the ordinary case rather than the
    permissive one.
    """

    def __init__(self, home: Path, *, pid: int | None = None,
                 expected_session_id: str | None = "sess-1"):
        self.requests: list[dict] = []
        self._status = 200
        self._body: dict = {"status": "bound", "verified": True}
        # Called inside the handler, so a test can observe what else had happened by the
        # time a request arrived. Used for the bind-before-verified ordering check.
        self.observer = None
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _wrapper_handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        self.home = home
        self.pid = pid if pid is not None else os.getpid()
        self.path = home / "wrappers" / f"{self.pid}.json"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.write(expected_session_id=expected_session_id)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def write(self, *, bound_to: str | None = None, verified: bool = False,
              expected_session_id: str | None = "keep", port: int | None = None,
              connect_error: str | None = None) -> None:
        if expected_session_id != "keep":
            self.expected_session_id = expected_session_id
        stamp = "2026-07-29T10:00:00Z"
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "kind": "wrapper",
                    "pid": self.pid,
                    "child_pid": self.pid + 1,
                    "port": self.port if port is None else port,
                    "started_at": stamp,
                    "expected_session_id": self.expected_session_id,
                    "generation": 1 if bound_to else 0,
                    "bound": bool(bound_to),
                    "verified": verified,
                    "claude_session_id": bound_to,
                    "agent_session_id": None,
                    "session_name": None,
                    "backend_url": None,
                    "bound_at": stamp if bound_to else None,
                    "verified_at": stamp if verified else None,
                    # W13. The launch connects silently, so this is where a failure goes.
                    "connect_error": connect_error,
                    "connect_error_at": stamp if connect_error else None,
                }
            )
        )

    def reply(self, status: int, body: dict) -> None:
        self._status, self._body = status, body

    def requests_to(self, path: str) -> list[dict]:
        return [r for r in self.requests if r["path"] == path]

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _wrapper_handler(wrapper: FakeWrapper):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                body = {}
            wrapper.requests.append({"path": self.path, "body": body})
            if wrapper.observer:
                wrapper.observer(self.path, body)
            payload = json.dumps(wrapper._body).encode("utf-8")
            self.send_response(wrapper._status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            """Silence."""

    return Handler


class CliTestCase(unittest.TestCase):
    """Every test gets a paired-shaped world: a stub backend, and one wrapper that started
    session `sess-1` and is waiting to be bound. Tests about the refusals take that away
    deliberately."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.backend = StubBackend()
        self.backend.__enter__()
        self.addCleanup(lambda: self.backend.__exit__(None, None, None))

        self.wrapper = FakeWrapper(self.home)
        self.addCleanup(self.wrapper.stop)

    def run_cli(self, *args: str, backend: str | None = None,
                wrapper_port: int | None = None, wrapper_pid: int | bool | None = None,
                no_wrapper_env: bool = False) -> subprocess.CompletedProcess:
        """`wrapper_pid=False` exports only the port, for the fallback lookup path."""
        env = dict(os.environ)
        env["AUDIOCHATTY_HOME"] = str(self.home)
        env["AUDIOCHATTY_BACKEND_URL"] = backend if backend is not None else self.backend.url
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        # See the module docstring: never inherited, always stated.
        env.pop("AUDIOCHATTY_WRAPPER_PORT", None)
        env.pop("AUDIOCHATTY_WRAPPER_PID", None)
        if not no_wrapper_env:
            port = self.wrapper.port if wrapper_port is None else wrapper_port
            env["AUDIOCHATTY_WRAPPER_PORT"] = str(port)
            if wrapper_pid is not False:
                pid = self.wrapper.pid if wrapper_pid is None else wrapper_pid
                env["AUDIOCHATTY_WRAPPER_PID"] = str(pid)
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def remove_wrapper(self) -> None:
        self.wrapper.path.unlink(missing_ok=True)

    def marker(self, claude_session_id: str = "sess-1") -> dict:
        path = self.home / "sessions" / f"{claude_session_id}.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def credentials(self) -> dict:
        path = self.home / "credentials.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def pair(self) -> None:
        """Get to a paired machine the way a user would: mint, then redeem."""
        self.run_cli("pair-start")
        self.run_cli("pair-finish", "--wait", "0")


# -- pairing ----------------------------------------------------------------------------


class TestPairing(CliTestCase):
    def test_pair_start_prints_the_code_and_stores_no_credentials(self):
        result = self.run_cli("pair-start")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WXYZ-1234", result.stdout)
        self.assertIn("http://localhost:3000/link", result.stdout)
        # The point of the two-step flow: the first command returns immediately, so the
        # user can actually see the code.
        self.assertEqual(self.credentials(), {})
        self.assertTrue((self.home / "pending.json").exists())

    def test_pair_start_names_pair_finish_as_the_next_step(self):
        """The whole reason these are two commands rather than one run twice: the user is
        told what to run next by name, not told to run this again."""
        result = self.run_cli("pair-start")
        self.assertIn("/audiochatty:audiochatty-pair-finish", result.stdout)

    def test_the_device_code_is_never_printed(self):
        """The `user_code` is meant to be read off the screen. The `device_code` is the
        secret half and must never reach the transcript."""
        result = self.run_cli("pair-start")
        self.assertNotIn("stub-device-code", result.stdout)
        self.assertNotIn("stub-device-code", result.stderr)
        pending = json.loads((self.home / "pending.json").read_text())
        self.assertEqual(pending["device_code"], "stub-device-code")

    def test_pair_finish_redeems_and_stores_the_token(self):
        self.run_cli("pair-start")
        result = self.run_cli("pair-finish", "--wait", "0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Linked to Mike's Workspace as Mike.", result.stdout)
        self.assertEqual(self.credentials()["token"], "stub-device-token")
        self.assertEqual(self.credentials()["backend_url"], self.backend.url)
        # Consumed: nothing left to redeem twice.
        self.assertFalse((self.home / "pending.json").exists())

    def test_the_token_is_never_printed(self):
        self.run_cli("pair-start")
        result = self.run_cli("pair-finish", "--wait", "0")
        self.assertNotIn("stub-device-token", result.stdout)
        self.assertNotIn("stub-device-token", result.stderr)

    def test_credentials_are_0600_and_the_directory_0700(self):
        self.pair()
        mode = stat.S_IMODE((self.home / "credentials.json").stat().st_mode)
        self.assertEqual(mode, 0o600, f"credentials are {oct(mode)}")
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o700)

    def test_pending_approval_keeps_the_code_alive(self):
        self.run_cli("pair-start")
        self.backend.reply("/device/token", 400, {"error": "authorization_pending"})

        result = self.run_cli("pair-finish", "--wait", "0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Still waiting", result.stdout)
        self.assertIn("WXYZ-1234", result.stdout)
        self.assertIn("/audiochatty:audiochatty-pair-finish", result.stdout)
        # Nothing was consumed — running the command again resumes the same pairing.
        self.assertTrue((self.home / "pending.json").exists())
        self.assertEqual(self.credentials(), {})

    def test_slow_down_is_honoured_then_the_poll_succeeds(self):
        self.run_cli("pair-start")
        self.backend.reply("/device/token", 400, {"error": "slow_down", "interval": 1})

        result = self.run_cli("pair-finish", "--wait", "30")

        self.assertIn("Linked to", result.stdout)
        self.assertEqual(len(self.backend.requests_to("/device/token")), 2)

    def test_an_expired_code_is_discarded_with_a_plain_message(self):
        self.run_cli("pair-start")
        self.backend.reply("/device/token", 400, {"error": "expired_token"})

        result = self.run_cli("pair-finish", "--wait", "0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("expired", result.stdout.lower())
        self.assertFalse((self.home / "pending.json").exists())

    def test_an_already_redeemed_code_is_discarded(self):
        self.run_cli("pair-start")
        self.backend.reply("/device/token", 400, {"error": "invalid_grant"})

        result = self.run_cli("pair-finish", "--wait", "0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("no longer valid", result.stdout)
        self.assertFalse((self.home / "pending.json").exists())

    def test_a_rate_limited_mint_says_so(self):
        self.backend.reply("/device/code", 429, {"error": "Too many pairing attempts."})
        result = self.run_cli("pair-start")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Too many pairing attempts", result.stdout)

    def test_an_unreachable_backend_does_not_traceback(self):
        result = self.run_cli("pair-start", backend="http://127.0.0.1:1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not reach audiochatty", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_the_hostname_rides_along_as_the_label(self):
        self.run_cli("pair-start")
        body = self.backend.last_request("/device/code")["body"]
        self.assertTrue(body["label"], "the approving browser needs something to show")

    def test_pairing_ends_by_naming_the_run_command(self):
        """R12, and Phase 0's launch decision: the bare command, not an alias. Four surfaces
        describe this setup and this is the only one we fully control and the only one the
        user is looking at when the step is due."""
        self.run_cli("pair-start")
        result = self.run_cli("pair-finish", "--wait", "0")

        self.assertIn(RUN_COMMAND, result.stdout)
        # The launch flag is gone, and nothing here may reintroduce it.
        self.assertNotIn("dangerously", result.stdout)

    def test_pairing_does_not_send_anyone_to_a_second_step(self):
        """W13. This copy told users to run `/audiochatty-connect` after `audiochatty run`
        for as long as connecting was a separate act. It isn't one — so naming it here would
        send every new user off to do something that now does nothing."""
        self.run_cli("pair-start")
        result = self.run_cli("pair-finish", "--wait", "0")

        self.assertNotIn("/audiochatty-connect", result.stdout)
        self.assertIn("connects the session for you", result.stdout)

    def test_pairing_also_shows_how_to_make_the_command_exist(self):
        """👤 chose the bare command on the grounds that developers can run commands — but
        it has to *be* one first, and that is a real install step rather than an alternative
        way to invoke it. The path is resolved from the plugin, so it is right wherever the
        plugin was installed."""
        self.run_cli("pair-start")
        result = self.run_cli("pair-finish", "--wait", "0")

        self.assertIn("alias audiochatty=", result.stdout)
        self.assertIn(str(SCRIPTS.parent / "wrapper" / "audiochatty"), result.stdout)

    def test_reset_discards_a_pending_code(self):
        self.run_cli("pair-start")
        first = json.loads((self.home / "pending.json").read_text())["user_code"]
        self.run_cli("pair-start", "--reset")
        self.assertEqual(len(self.backend.requests_to("/device/code")), 2)
        self.assertEqual(first, "WXYZ-1234")


class TestPairingInTheWrongState(CliTestCase):
    """Two commands means each can be run when the other one was due. Neither may guess:
    running the wrong half has to say which half was wrong (👤 2026-08-04)."""

    def test_pair_start_reshows_a_live_code_instead_of_minting_another(self):
        """A second live code would leave the first one unexpired and typable into `/link`,
        and the user asking again wants *the* code, not another one."""
        self.run_cli("pair-start")

        result = self.run_cli("pair-start")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WXYZ-1234", result.stdout)
        self.assertIn("already have a pairing code", result.stdout)
        self.assertIn("/audiochatty:audiochatty-pair-finish", result.stdout)
        self.assertEqual(len(self.backend.requests_to("/device/code")), 1)

    def test_pair_finish_before_pair_start_says_which_to_run(self):
        result = self.run_cli("pair-finish", "--wait", "0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("nothing to finish", result.stdout)
        self.assertIn("/audiochatty:audiochatty-pair-start", result.stdout)
        self.assertEqual(self.backend.requests_to("/device/token"), [])

    def test_pair_finish_on_an_already_paired_machine_is_not_an_error(self):
        """Running it twice is the most likely misfire of all, and the machine is in the
        state the user wanted. Saying so beats telling them off."""
        self.pair()

        result = self.run_cli("pair-finish", "--wait", "0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already paired to Mike's Workspace as Mike", result.stdout)
        self.assertIn("/audiochatty:audiochatty-pair-start", result.stdout)
        # One redemption, from `pair()`. This run spent nothing.
        self.assertEqual(len(self.backend.requests_to("/device/token")), 1)
        self.assertEqual(self.credentials()["token"], "stub-device-token")

    def test_pair_finish_on_an_expired_code_says_so_and_clears_it(self):
        self.run_cli("pair-start")
        pending = json.loads((self.home / "pending.json").read_text())
        pending["expires_at"] = time.time() - 1
        (self.home / "pending.json").write_text(json.dumps(pending))

        result = self.run_cli("pair-finish", "--wait", "0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("no longer good", result.stdout)
        self.assertIn("/audiochatty:audiochatty-pair-start", result.stdout)
        self.assertFalse((self.home / "pending.json").exists())
        self.assertEqual(self.backend.requests_to("/device/token"), [])


# -- connect ----------------------------------------------------------------------------


class TestConnect(CliTestCase):
    def test_registers_and_writes_a_marker(self):
        self.pair()
        result = self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"billing-refactor" in audiochatty', result.stdout)

        request = self.backend.last_request("/agent/session")
        self.assertEqual(request["authorization"], "Bearer stub-device-token")
        self.assertEqual(request["body"]["claude_session_id"], "sess-1")
        self.assertEqual(request["body"]["name"], "billing-refactor")

        self.assertEqual(self.marker()["name"], "billing-refactor")
        self.assertEqual(
            self.marker()["session_id"], "33333333-3333-3333-3333-333333333333"
        )

    def test_the_marker_tells_the_stop_hook_to_skip_this_turn(self):
        """The turn that registers the session is the turn that reports the registration,
        and the user is watching it happen. The other half of this is in test_hooks."""
        self.pair()
        self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")
        self.assertTrue(self.marker()["skip_next_turn"])

    def test_the_name_defaults_to_the_folder(self):
        self.pair()
        self.run_cli("connect", "--session-id", "sess-1", "--cwd", "/tmp/my-repo")
        self.assertEqual(self.backend.last_request("/agent/session")["body"]["name"], "my-repo")

    def test_an_empty_argument_is_not_a_name(self):
        """`/audiochatty-connect` with no argument expands to an empty string, which must
        fall through to the folder default rather than being sent as the name."""
        self.pair()
        self.run_cli("connect", "", "--session-id", "sess-1", "--cwd", "/tmp/my-repo")
        self.assertEqual(self.backend.last_request("/agent/session")["body"]["name"], "my-repo")

    def test_the_session_id_falls_back_to_the_environment(self):
        """`${CLAUDE_SESSION_ID}` is substituted by Claude Code into the command line; if
        that ever stops resolving, the exported `CLAUDE_CODE_SESSION_ID` still answers."""
        self.pair()
        env = dict(os.environ)
        env["AUDIOCHATTY_HOME"] = str(self.home)
        env["AUDIOCHATTY_BACKEND_URL"] = self.backend.url
        env["CLAUDE_CODE_SESSION_ID"] = "sess-1"
        env["AUDIOCHATTY_WRAPPER_PORT"] = str(self.wrapper.port)
        env["AUDIOCHATTY_WRAPPER_PID"] = str(self.wrapper.pid)
        subprocess.run(
            [sys.executable, str(CLI), "connect", "--session-id", ""],
            env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(
            self.backend.last_request("/agent/session")["body"]["claude_session_id"],
            "sess-1",
        )

    def test_an_unpaired_machine_is_told_to_pair(self):
        result = self.run_cli("connect", "x", "--session-id", "sess-1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("/audiochatty-pair-start", result.stdout)
        self.assertEqual(self.backend.requests_to("/agent/session"), [])

    def test_a_revoked_token_says_so_and_writes_no_marker(self):
        self.pair()
        self.backend.reply("/agent/session", 401, {"error": "Unauthorized"})

        result = self.run_cli("connect", "x", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 1)
        self.assertIn("revoked", result.stdout)
        self.assertEqual(self.marker(), {})

    def test_a_failed_registration_leaves_no_marker(self):
        """The marker is the Stop hook's gate. A marker without a live registration means
        every turn posts into a 404 forever."""
        self.pair()
        result = self.run_cli("connect", "x", "--session-id", "sess-1",
                              backend="http://127.0.0.1:1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.marker(), {})

    def test_a_session_id_cannot_escape_the_sessions_directory(self):
        self.pair()
        self.wrapper.write(expected_session_id="../../escaped")
        self.run_cli("connect", "x", "--session-id", "../../escaped")
        self.assertFalse((self.home.parent / "escaped.json").exists())
        self.assertTrue(list((self.home / "sessions").glob("*.json")))


# -- connect and the return path (W3, W4, W8) --------------------------------------------


class TestConnectBindsTheWrapper(CliTestCase):
    def test_the_wrapper_is_bound_with_the_registration_it_just_got(self):
        """The whole of the correlation in one assertion: the id `/bind` receives is the one
        the backend answered `/agent/session` with, so the wrapper polls for the right
        session."""
        self.pair()
        result = self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        bind = self.wrapper.requests_to("/bind")
        self.assertEqual(len(bind), 1, "exactly one bind per connect")
        self.assertEqual(
            bind[0]["body"]["agent_session_id"], "33333333-3333-3333-3333-333333333333"
        )
        self.assertEqual(bind[0]["body"]["claude_session_id"], "sess-1")
        self.assertEqual(bind[0]["body"]["backend_url"], self.backend.url)
        self.assertEqual(bind[0]["body"]["session_name"], "billing-refactor")

    def test_the_bind_carries_this_machines_token(self):
        """`/bind` refuses a token that doesn't match `credentials.json`, so a wrapper
        cannot be pointed at a session by some other process on the machine."""
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-1")
        self.assertEqual(
            self.wrapper.requests_to("/bind")[0]["body"]["token"], "stub-device-token"
        )

    def test_the_marker_records_which_wrapper_was_bound(self):
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-1")
        self.assertEqual(self.marker()["wrapper_pid"], self.wrapper.pid)
        self.assertEqual(self.marker()["wrapper_port"], self.wrapper.port)

    def test_a_second_connect_in_the_same_session_rebinds_rather_than_refusing(self):
        """The wrapper treats a re-bind of the same session as a refresh. This side has to
        keep offering it one — the candidate is already `bound`, and a filter on that would
        refuse a re-run in the terminal it belongs to."""
        self.pair()
        self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")
        self.wrapper.write(bound_to="sess-1", verified=True)

        result = self.run_cli("connect", "renamed", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.wrapper.requests_to("/bind")), 2)

    def test_the_wrapper_is_found_by_port_alone_if_the_pid_is_missing(self):
        """The pid is the better key and the one the wrapper exports, but the port alone
        still identifies a process — one listener, one port."""
        self.pair()
        result = self.run_cli("connect", "x", "--session-id", "sess-1", wrapper_pid=False)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.wrapper.requests_to("/bind")), 1)


class TestConnectMarksTheSessionReachable(CliTestCase):
    """W8. The old design proved reachability with a nonce, an injected handshake, a tool
    call from the model, and a retry loop, because a channel could not tell whether its
    events were honoured. The wrapper owns the pty, so binding *is* the proof and the whole
    mechanism collapses into one POST."""

    def test_connect_tells_the_backend_this_session_is_reachable(self):
        self.pair()
        self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")

        request = self.backend.last_request("/agent/session/verified")
        self.assertIsNotNone(request, "the inbox reads this to decide whether you can reply")
        self.assertEqual(request["body"]["claude_session_id"], "sess-1")
        self.assertEqual(request["authorization"], "Bearer stub-device-token")

    def test_it_happens_after_the_bind_not_before(self):
        """Order matters: the claim only becomes true at the bind. Telling the backend first
        would advertise a session as reachable during the window where the bind could still
        fail and be rolled back."""
        self.pair()
        seen: list[int] = []
        self.wrapper.observer = lambda path, body: seen.append(
            len(self.backend.requests_to("/agent/session/verified"))
        )

        self.run_cli("connect", "x", "--session-id", "sess-1")

        self.assertEqual(seen, [0], "verified was posted before the bind arrived")
        self.assertEqual(len(self.backend.requests_to("/agent/session/verified")), 1)

    def test_a_refused_bind_is_never_advertised_as_reachable(self):
        self.pair()
        self.wrapper.reply(409, {"error": "already_bound"})

        result = self.run_cli("connect", "x", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.backend.requests_to("/agent/session/verified"), [])

    def test_a_failed_verified_call_does_not_fail_the_connect(self):
        """The wrapper retries this from its own poll loop, so the cost of losing the call is
        a few seconds of the phone saying "you can't talk to this one" — not a reason to
        undo a registration and a bind that both worked."""
        self.pair()
        self.backend.reply("/agent/session/verified", 502, {"error": "nope"})

        result = self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('"billing-refactor" in audiochatty', result.stdout)
        self.assertEqual(self.marker()["name"], "billing-refactor")
        # And it isn't mentioned: the user just watched connect succeed.
        self.assertNotIn("502", result.stdout)


class TestConnectRefusals(CliTestCase):
    """W4, inherited unchanged from R1: a session that can't be talked to isn't registered at
    all. Each refusal has to happen *before* the registration, or it leaves a live row
    behind. There is one refusal now where there used to be three — no ambiguity case, and no
    launch-flag case."""

    def test_a_session_with_no_wrapper_refuses_and_names_the_run_command(self):
        """The common failure: started with plain `claude`, so nothing can type into it."""
        self.pair()

        result = self.run_cli("connect", "x", "--session-id", "sess-1", no_wrapper_env=True)

        self.assertEqual(result.returncode, 1)
        self.assertIn(RUN_COMMAND, result.stdout)
        self.assertIn("no audiochatty return path", result.stdout)
        self.assertEqual(self.backend.requests_to("/agent/session"), [])
        self.assertEqual(self.wrapper.requests_to("/bind"), [])
        self.assertEqual(self.marker(), {})

    def test_the_refusal_says_how_to_make_the_command_exist(self):
        self.pair()
        result = self.run_cli("connect", "x", "--session-id", "sess-1", no_wrapper_env=True)
        self.assertIn("alias audiochatty=", result.stdout)

    def test_a_missing_rendezvous_file_is_no_wrapper(self):
        """The variables say a wrapper is there; the file says otherwise. Trusting the
        variables alone would POST `/bind` at whatever now holds that port."""
        self.pair()
        self.remove_wrapper()

        result = self.run_cli("connect", "x", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 1)
        self.assertIn(RUN_COMMAND, result.stdout)
        self.assertEqual(self.backend.requests_to("/agent/session"), [])

    def test_a_dead_wrapper_process_is_not_a_wrapper(self):
        """A rendezvous file outliving its process is how `connect` binds to a corpse. The
        wrapper prunes its own at startup; this side must not trust one either."""
        self.pair()
        dead_pid = 999_999
        (self.home / "wrappers" / f"{dead_pid}.json").write_text(
            json.dumps({"version": 1, "kind": "wrapper", "pid": dead_pid,
                        "port": self.wrapper.port, "expected_session_id": "sess-1",
                        "bound": False})
        )
        self.remove_wrapper()

        result = self.run_cli("connect", "x", "--session-id", "sess-1",
                              wrapper_pid=dead_pid)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.wrapper.requests_to("/bind"), [])
        self.assertEqual(self.backend.requests_to("/agent/session"), [])

    def test_a_port_that_disagrees_with_the_rendezvous_file_is_refused(self):
        """The guard against pid reuse: a variable left over from a wrapper that exited, and
        a live process that inherited its pid. The file has to agree about the port."""
        self.pair()

        result = self.run_cli("connect", "x", "--session-id", "sess-1",
                              wrapper_port=self.wrapper.port + 1)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.wrapper.requests_to("/bind"), [])
        self.assertEqual(self.backend.requests_to("/agent/session"), [])

    def test_a_nested_plain_claude_is_refused_by_name(self):
        """W3's own refusal, and the reason `expected_session_id` exists. A wrapped session
        runs a Bash tool call, someone types plain `claude` inside it, and that inner session
        inherits both variables. Binding it would type this user's instructions into a
        terminal they can't see. It gets its own message, because telling them to run
        `audiochatty run` would be true and useless — they already are inside one."""
        self.pair()

        result = self.run_cli("connect", "x", "--session-id", "some-inner-session")

        self.assertEqual(result.returncode, 1)
        self.assertIn("isn't the session `audiochatty run` started", result.stdout)
        self.assertEqual(self.wrapper.requests_to("/bind"), [])
        self.assertEqual(self.backend.requests_to("/agent/session"), [])

    def test_a_wrapper_that_did_not_choose_its_session_id_accepts_ours(self):
        """`--resume`, `--continue` and an explicit `--session-id` all decide the session
        before the wrapper can, so it publishes `null` and pins the first id it is given.
        Refusing here would break every resumed session."""
        self.pair()
        self.wrapper.write(expected_session_id=None)

        result = self.run_cli("connect", "x", "--session-id", "resumed-session")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(
            self.wrapper.requests_to("/bind")[0]["body"]["claude_session_id"],
            "resumed-session",
        )

    def test_a_wrapper_that_refuses_the_bind_rolls_the_registration_back(self):
        """The one path that has to undo something: registration landed, the bind didn't,
        and W4 says there is no such thing as a half-registered session."""
        self.pair()
        self.wrapper.reply(409, {"error": "already_bound"})

        result = self.run_cli("connect", "x", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.marker(), {})
        self.assertEqual(
            self.backend.last_request("/agent/session/end")["body"]["claude_session_id"],
            "sess-1",
        )

    def test_an_unreachable_wrapper_rolls_the_registration_back_too(self):
        """A rendezvous file for a live process whose server is gone. Same conclusion by a
        different route, and it must not traceback."""
        self.pair()
        self.wrapper.stop()

        result = self.run_cli("connect", "x", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.marker(), {})
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(
            self.backend.last_request("/agent/session/end")["body"]["claude_session_id"],
            "sess-1",
        )


# -- status -----------------------------------------------------------------------------


class TestStatus(CliTestCase):
    def test_unpaired(self):
        result = self.run_cli("status", "--session-id", "sess-1")
        self.assertEqual(result.returncode, 0)
        self.assertIn("isn't paired", result.stdout)

    def test_paired_but_this_session_is_not_registered(self):
        self.pair()
        result = self.run_cli("status", "--session-id", "sess-1")
        self.assertIn("Paired with Mike's Workspace as Mike.", result.stdout)
        self.assertIn("NOT registered", result.stdout)

    def test_registered(self):
        self.pair()
        self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")
        result = self.run_cli("status", "--session-id", "sess-1")
        self.assertIn('registered as "billing-refactor"', result.stdout)

    def test_other_sessions_are_listed_so_a_stray_registration_is_visible(self):
        self.pair()
        self.backend.reply("/agent/session", 200, {"session_id": "s-a", "name": "other-repo"})
        self.wrapper.write(expected_session_id="sess-2")
        self.run_cli("connect", "other-repo", "--session-id", "sess-2")

        result = self.run_cli("status", "--session-id", "sess-1")

        self.assertIn('"other-repo"', result.stdout)

    def test_status_makes_no_network_calls(self):
        self.pair()
        before = len(self.backend.requests)
        self.run_cli("status", "--session-id", "sess-1")
        self.assertEqual(len(self.backend.requests), before)


class TestStatusReportsTheWrapper(CliTestCase):
    """This is the command a confused user runs. It stays local: the rendezvous file already
    carries `bound` and `verified`, so there is nothing here to be slow or to be down."""

    def test_a_verified_session_says_it_can_be_talked_to(self):
        self.pair()
        self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")
        self.wrapper.write(bound_to="sess-1", verified=True)

        result = self.run_cli("status", "--session-id", "sess-1")

        self.assertIn("You can talk to this session from audiochatty", result.stdout)

    def test_a_session_with_no_wrapper_is_told_how_to_get_one(self):
        self.pair()

        result = self.run_cli("status", "--session-id", "sess-1", no_wrapper_env=True)

        self.assertIn("no audiochatty return path", result.stdout)
        self.assertIn(RUN_COMMAND, result.stdout)
        self.assertNotIn("dangerously", result.stdout)

    def test_a_registered_session_whose_wrapper_is_unbound_says_to_reconnect(self):
        """The marker survives an unbind; the binding doesn't. What is left is a session that
        reads as registered and silently receives nothing."""
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-1")
        self.wrapper.write()  # unbound again

        result = self.run_cli("status", "--session-id", "sess-1")

        self.assertIn("isn't connected", result.stdout)
        self.assertIn("/audiochatty-connect", result.stdout)

    def test_an_unconnected_session_is_told_the_wrapper_is_waiting(self):
        self.pair()

        result = self.run_cli("status", "--session-id", "sess-1")

        self.assertIn("waiting to be connected", result.stdout)

    def test_a_nested_session_is_told_why_it_cannot_be_talked_to(self):
        self.pair()

        result = self.run_cli("status", "--session-id", "some-inner-session")

        self.assertIn("belongs to a", result.stdout)
        self.assertIn("different session", result.stdout)

    def test_an_unknown_session_id_does_not_read_as_connected(self):
        """Two unknowns are not a match. An unbound wrapper records a null
        `claude_session_id`, so comparing it against an empty session id of our own used to
        come out equal and report the session as connected."""
        self.pair()

        result = self.run_cli("status", "--session-id", "")

        self.assertIn("waiting to be connected", result.stdout)
        self.assertNotIn("You can talk to this session", result.stdout)

    def test_the_wrapper_report_still_makes_no_network_calls(self):
        """Not even a loopback `GET /status`. The promise is that this command cannot hang and
        cannot fail because something else is down."""
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-1")
        before = len(self.backend.requests)

        self.run_cli("status", "--session-id", "sess-1")

        self.assertEqual(len(self.backend.requests), before)
        self.assertEqual(self.wrapper.requests_to("/status"), [])


# -- disconnect -------------------------------------------------------------------------


class TestDisconnect(CliTestCase):
    def test_removes_the_marker_and_ends_the_session(self):
        self.pair()
        self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")

        result = self.run_cli("disconnect", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Stopped sending "billing-refactor"', result.stdout)
        self.assertEqual(self.marker(), {})
        self.assertEqual(
            self.backend.last_request("/agent/session/end")["body"]["claude_session_id"],
            "sess-1",
        )

    def test_an_unregistered_session_is_not_an_error(self):
        self.pair()
        result = self.run_cli("disconnect", "--session-id", "sess-1")
        self.assertEqual(result.returncode, 0)
        self.assertIn("wasn't registered", result.stdout)
        self.assertEqual(self.backend.requests_to("/agent/session/end"), [])

    def test_the_wrapper_is_unbound_as_well(self):
        """A wrapper left bound keeps polling for instructions addressed to a session the
        user has just closed, and would type one into this terminal."""
        self.pair()
        self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")
        self.wrapper.write(bound_to="sess-1", verified=True)

        self.run_cli("disconnect", "--session-id", "sess-1")

        unbind = self.wrapper.requests_to("/unbind")
        self.assertEqual(len(unbind), 1)
        self.assertEqual(unbind[0]["body"]["token"], "stub-device-token")

    def test_the_unbind_does_not_need_the_environment_variables(self):
        """Found by the binding rather than by the inherited port, deliberately: this has to
        work in the session being retired even if its wrapper's variables never reached
        here, and a wrapper bound to *this* session is ours however we found it."""
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-1")
        self.wrapper.write(bound_to="sess-1", verified=True)

        self.run_cli("disconnect", "--session-id", "sess-1", no_wrapper_env=True)

        self.assertEqual(len(self.wrapper.requests_to("/unbind")), 1)

    def test_another_sessions_wrapper_is_left_alone(self):
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-1")
        self.wrapper.write(bound_to="a-different-session")

        self.run_cli("disconnect", "--session-id", "sess-1")

        self.assertEqual(self.wrapper.requests_to("/unbind"), [])

    def test_an_unreachable_wrapper_does_not_stop_the_disconnect(self):
        """The unbind is best effort by design: a wrapper that cannot be reached is one that
        has already exited, which is the same outcome by a different route."""
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-1")
        self.wrapper.write(bound_to="sess-1", verified=True)
        self.wrapper.stop()

        result = self.run_cli("disconnect", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Stopped sending", result.stdout)
        self.assertEqual(self.marker(), {})

    def test_the_marker_goes_even_when_the_backend_is_unreachable(self):
        """Local state first. A failed POST must not leave a hook posting turns for a
        session the user believes they closed."""
        self.pair()
        self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")

        result = self.run_cli("disconnect", "--session-id", "sess-1",
                              backend="http://127.0.0.1:1")

        self.assertEqual(result.returncode, 0)
        self.assertIn("Stopped sending", result.stdout)
        self.assertEqual(self.marker(), {})


# -- W13: connect is a repair tool now --------------------------------------------------


class TestConnectIsARepairTool(CliTestCase):
    """Phase 6.5. `audiochatty run` connects its own session, so the three reasons left to
    run this command by hand are: retry after a failed launch connect, rename, and reconnect
    after a disconnect. The first case it never had to handle before is the one that is now
    *likely* — being run against a session that is already connected."""

    def tombstone(self, claude_session_id: str = "sess-1") -> dict:
        path = self.home / "disconnected" / f"{claude_session_id}.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def test_an_already_connected_session_is_reported_not_reregistered(self):
        self.pair()
        self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")
        self.wrapper.write(bound_to="sess-1", verified=True)
        before = len(self.backend.requests_to("/agent/session"))

        result = self.run_cli("connect", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('already connected as "billing-refactor"', result.stdout)
        self.assertEqual(len(self.backend.requests_to("/agent/session")), before)

    def test_a_name_still_renames_an_already_connected_session(self):
        """Renaming is one of the three reasons this command survives, so asking for a name
        has to beat the "already connected, nothing to do" shortcut."""
        self.pair()
        self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")
        self.wrapper.write(bound_to="sess-1", verified=True)
        # The registered name comes back from the backend, so the stub has to agree for the
        # rename to be observable end to end rather than only in the request body.
        self.backend.reply("/agent/session", 200, {"session_id": "s-a", "name": "auth-bug"})

        result = self.run_cli("connect", "auth-bug", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"auth-bug" in audiochatty', result.stdout)
        self.assertEqual(
            self.backend.last_request("/agent/session")["body"]["name"], "auth-bug"
        )

    def test_it_clears_the_tombstone_so_a_reconnect_actually_sticks(self):
        """The tombstone stops *automatic* reconnection. An explicit connect is the user
        asking by name, and must not be blocked by their earlier decision — nor leave the
        tombstone behind to block the next `/clear`."""
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-1")
        self.run_cli("disconnect", "--session-id", "sess-1")
        self.assertTrue(self.tombstone())

        result = self.run_cli("connect", "x", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tombstone(), {})

    def test_the_marker_still_skips_the_turn_that_reports_the_connect(self):
        """Only on this path. The turn that runs the slash command ends by relaying its
        output, which is not worth reading back to the person who typed it — but a launch has
        no such turn, which is what `test_a_launch_connect_does_not_skip_a_turn` pins."""
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-1")
        self.assertTrue(self.marker()["skip_next_turn"])


class TestDisconnectStaysDisconnected(CliTestCase):
    """W13's sharpest edge. Connecting is automatic now, and `SessionStart` fires again on
    `/clear` — so without a record of the user's decision, going quiet would silently undo
    itself. The hook-side half of this is in `test_hooks.py`."""

    def tombstone(self, claude_session_id: str = "sess-1") -> dict:
        path = self.home / "disconnected" / f"{claude_session_id}.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def test_it_leaves_a_tombstone(self):
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-1")

        self.run_cli("disconnect", "--session-id", "sess-1")

        self.assertEqual(self.tombstone()["claude_session_id"], "sess-1")

    def test_an_unregistered_session_is_tombstoned_too(self):
        """Someone who runs `/audiochatty-disconnect` in a session that never connected is
        still stating a preference, and the hook has to honour it."""
        self.pair()

        self.run_cli("disconnect", "--session-id", "sess-1")

        self.assertTrue(self.tombstone())

    def test_the_tombstone_is_not_mistaken_for_a_registration(self):
        """It lives in its own directory precisely so nothing globbing `sessions/*.json`
        picks it up — including the "other registered sessions" list."""
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-2")
        self.run_cli("disconnect", "--session-id", "sess-2")

        result = self.run_cli("status", "--session-id", "sess-1")

        self.assertNotIn("Other registered sessions", result.stdout)

    def test_status_says_a_disconnected_session_stays_that_way(self):
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-1")
        self.run_cli("disconnect", "--session-id", "sess-1")

        result = self.run_cli("status", "--session-id", "sess-1")

        self.assertIn("was disconnected", result.stdout)
        self.assertIn("/audiochatty-connect", result.stdout)


class TestStatusExplainsASilentLaunchFailure(CliTestCase):
    """W13 made connecting silent, so `/audiochatty-status` is the only place a failed
    connect surfaces at all. The wrapper writes the code; this turns it into a sentence."""

    def test_an_unreachable_backend_at_launch_is_named(self):
        self.pair()
        self.wrapper.write(connect_error="unreachable")

        result = self.run_cli("status", "--session-id", "sess-1")

        self.assertIn("couldn't reach audiochatty", result.stdout)
        self.assertIn("/audiochatty-connect", result.stdout)

    def test_a_revoked_token_at_launch_is_named(self):
        self.pair()
        self.wrapper.write(connect_error="revoked")

        result = self.run_cli("status", "--session-id", "sess-1")

        self.assertIn("rejected the machine's token", result.stdout)
        self.assertIn("/audiochatty-pair-start", result.stdout)

    def test_an_unrecognised_code_still_produces_something_honest(self):
        self.pair()
        self.wrapper.write(connect_error="something-new")

        result = self.run_cli("status", "--session-id", "sess-1")

        self.assertIn("didn't manage it", result.stdout)

    def test_a_connected_session_never_mentions_a_stale_error(self):
        """The wrapper clears the field on success, but a stale value must not outrank a
        working session here either."""
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-1")
        self.wrapper.write(bound_to="sess-1", verified=True, connect_error="unreachable")

        result = self.run_cli("status", "--session-id", "sess-1")

        self.assertIn("You can talk to this session from audiochatty", result.stdout)
        self.assertNotIn("didn't manage it", result.stdout)

    def test_naming_the_cause_still_makes_no_network_calls(self):
        self.pair()
        self.wrapper.write(connect_error="unreachable")
        before = len(self.backend.requests)

        self.run_cli("status", "--session-id", "sess-1")

        self.assertEqual(len(self.backend.requests), before)


if __name__ == "__main__":
    unittest.main()
