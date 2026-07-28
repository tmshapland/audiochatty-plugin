"""The four slash commands, driven the way Claude Code drives them.

Every test runs `scripts/audiochat.py` as a subprocess against the stub backend, with
`AUDIOCHATTY_HOME` pointed at a temp directory — so the suite is safe to run on a machine
that is already paired, and what it asserts is what the command actually does rather than
what an imported function returns.

`channel_return_path_plan.md` Phase 4 added the return path to `connect`, and with it two
fixtures below. Both exist because the thing being tested is a *correlation* between three
processes, and faking it at the wrong layer would prove nothing:

- **`FakeClaude`** is a real process carrying a real command line, because the check it
  stands in for reads `ps` output. A stub that returned "yes, the flag is there" would test
  the caller and not the parser, and the parser is where the product breaks.
- **`FakeChannel`** is a real HTTP server on a real loopback port with a real rendezvous
  file, because `connect`'s job is to find one of those and POST to it.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stub_backend import StubBackend  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
CLI = SCRIPTS / "audiochat.py"

LAUNCH_FLAG = "--dangerously-load-development-channels"
LAUNCH_TARGET = "plugin:audiochatty@audiochatty"


class FakeClaude:
    """A live process standing in for `claude`, with a command line we choose.

    `cmd_connect` decides whether this session's channel events are honoured by reading the
    `claude` process's argv, so the only honest fixture for that is a process whose argv
    really does or doesn't carry the flag. It sleeps rather than doing anything, and the
    tests point `CLAUDE_PID` at it.
    """

    def __init__(self, *, flag: bool = True, target: str = LAUNCH_TARGET):
        args = [sys.executable, "-c", "import time; time.sleep(120)"]
        if flag:
            args += [LAUNCH_FLAG, target]
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    @property
    def pid(self) -> int:
        return self.proc.pid

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class FakeChannel:
    """A rendezvous file plus a loopback server answering `/bind` and `/unbind`.

    The rendezvous is written the way `channel/server.ts` writes it, including the
    `claude_env.CLAUDE_PID` that ties it to a `FakeClaude` — that is one of the three
    signals `find_channel` matches on, and the one that doesn't depend on this test
    process's own ancestry.
    """

    def __init__(self, home: Path, claude_pid: int, *, bound_to: str | None = None,
                 pid: int | None = None):
        self.claude_pid = claude_pid
        self.requests: list[dict] = []
        self._status = 200
        self._body: dict = {"status": "bound"}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _channel_handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        self.home = home
        # `read_channels` requires the pid to be alive and the file to be `<pid>.json`.
        # This process and its parent are the two most convenient live pids there are, and
        # two of them is what the ambiguity test needs.
        self.pid = pid if pid is not None else os.getpid()
        self.path = home / "channels" / f"{self.pid}.json"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.write(bound_to=bound_to)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def write(self, *, bound_to: str | None = None, verified: bool = False) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": self.pid,
                    "ppid": os.getppid(),
                    "port": self.port,
                    "started_at": "2026-07-28T10:00:00Z",
                    "ancestry": [{"pid": self.pid, "ppid": os.getppid(), "comm": "bun"}],
                    "claude_env": {"CLAUDE_PID": str(self.claude_pid)},
                    "bound": bool(bound_to),
                    "verified": verified,
                    "claude_session_id": bound_to,
                    "agent_session_id": None,
                    "session_name": None,
                    "backend_url": None,
                    "bound_at": None,
                    "verified_at": None,
                }
            )
        )

    def retarget(self, claude_pid: int) -> None:
        """Point this channel at a different `claude`. Tests about the launch flag need a
        channel that genuinely belongs to the process whose command line they are asking
        about — otherwise `connect` refuses for the *other* reason and the test passes
        without exercising anything."""
        self.claude_pid = claude_pid
        self.write()

    def reply(self, status: int, body: dict) -> None:
        self._status, self._body = status, body

    def requests_to(self, path: str) -> list[dict]:
        return [r for r in self.requests if r["path"] == path]

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _channel_handler(channel: FakeChannel):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                body = {}
            channel.requests.append({"path": self.path, "body": body})
            payload = json.dumps(channel._body).encode("utf-8")
            self.send_response(channel._status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            """Silence."""

    return Handler


class CliTestCase(unittest.TestCase):
    """Every test gets a paired-shaped world: a stub backend, a `claude` carrying the
    channel flag, and one channel waiting to be bound. Tests about the refusals take those
    away deliberately."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.backend = StubBackend()
        self.backend.__enter__()
        self.addCleanup(lambda: self.backend.__exit__(None, None, None))

        self.claude = FakeClaude(flag=True)
        self.addCleanup(self.claude.stop)
        self.channel = FakeChannel(self.home, self.claude.pid)
        self.addCleanup(self.channel.stop)

    def run_cli(self, *args: str, backend: str | None = None,
                claude_pid: int | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["AUDIOCHATTY_HOME"] = str(self.home)
        env["AUDIOCHATTY_BACKEND_URL"] = backend if backend is not None else self.backend.url
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        # Without this the ancestry walk would find whatever `claude` this suite happens to
        # be running under, which is a different answer on a laptop than in CI.
        env["CLAUDE_PID"] = str(self.claude.pid if claude_pid is None else claude_pid)
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def remove_channel(self) -> None:
        self.channel.path.unlink(missing_ok=True)

    def credentials(self) -> dict:
        path = self.home / "credentials.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def pair(self) -> None:
        """Get to a paired machine the way a user would: mint, then redeem."""
        self.run_cli("login")
        self.run_cli("login", "--wait", "0")


# -- login ------------------------------------------------------------------------------


class TestLogin(CliTestCase):
    def test_first_run_prints_the_code_and_stores_no_credentials(self):
        result = self.run_cli("login")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WXYZ-1234", result.stdout)
        self.assertIn("http://localhost:3000/link", result.stdout)
        # The point of the two-step flow: the first run returns immediately, so the user
        # can actually see the code.
        self.assertEqual(self.credentials(), {})
        self.assertTrue((self.home / "pending.json").exists())

    def test_the_device_code_is_never_printed(self):
        """The `user_code` is meant to be read off the screen. The `device_code` is the
        secret half and must never reach the transcript."""
        result = self.run_cli("login")
        self.assertNotIn("stub-device-code", result.stdout)
        self.assertNotIn("stub-device-code", result.stderr)
        pending = json.loads((self.home / "pending.json").read_text())
        self.assertEqual(pending["device_code"], "stub-device-code")

    def test_second_run_redeems_and_stores_the_token(self):
        self.run_cli("login")
        result = self.run_cli("login", "--wait", "0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Linked to Mike's Workspace as Mike.", result.stdout)
        self.assertEqual(self.credentials()["token"], "stub-device-token")
        self.assertEqual(self.credentials()["backend_url"], self.backend.url)
        # Consumed: nothing left to redeem twice.
        self.assertFalse((self.home / "pending.json").exists())

    def test_the_token_is_never_printed(self):
        self.run_cli("login")
        result = self.run_cli("login", "--wait", "0")
        self.assertNotIn("stub-device-token", result.stdout)
        self.assertNotIn("stub-device-token", result.stderr)

    def test_credentials_are_0600_and_the_directory_0700(self):
        self.pair()
        mode = stat.S_IMODE((self.home / "credentials.json").stat().st_mode)
        self.assertEqual(mode, 0o600, f"credentials are {oct(mode)}")
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o700)

    def test_pending_approval_keeps_the_code_alive(self):
        self.run_cli("login")
        self.backend.reply("/device/token", 400, {"error": "authorization_pending"})

        result = self.run_cli("login", "--wait", "0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Still waiting", result.stdout)
        self.assertIn("WXYZ-1234", result.stdout)
        # Nothing was consumed — running the command again resumes the same pairing.
        self.assertTrue((self.home / "pending.json").exists())
        self.assertEqual(self.credentials(), {})

    def test_slow_down_is_honoured_then_the_poll_succeeds(self):
        self.run_cli("login")
        self.backend.reply("/device/token", 400, {"error": "slow_down", "interval": 1})

        result = self.run_cli("login", "--wait", "30")

        self.assertIn("Linked to", result.stdout)
        self.assertEqual(len(self.backend.requests_to("/device/token")), 2)

    def test_an_expired_code_is_discarded_with_a_plain_message(self):
        self.run_cli("login")
        self.backend.reply("/device/token", 400, {"error": "expired_token"})

        result = self.run_cli("login", "--wait", "0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("expired", result.stdout.lower())
        self.assertFalse((self.home / "pending.json").exists())

    def test_an_already_redeemed_code_is_discarded(self):
        self.run_cli("login")
        self.backend.reply("/device/token", 400, {"error": "invalid_grant"})

        result = self.run_cli("login", "--wait", "0")

        self.assertEqual(result.returncode, 1)
        self.assertIn("no longer valid", result.stdout)
        self.assertFalse((self.home / "pending.json").exists())

    def test_a_rate_limited_mint_says_so(self):
        self.backend.reply("/device/code", 429, {"error": "Too many pairing attempts."})
        result = self.run_cli("login")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Too many pairing attempts", result.stdout)

    def test_an_unreachable_backend_does_not_traceback(self):
        result = self.run_cli("login", backend="http://127.0.0.1:1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not reach audiochatty", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_the_hostname_rides_along_as_the_label(self):
        self.run_cli("login")
        body = self.backend.last_request("/device/code")["body"]
        self.assertTrue(body["label"], "the approving browser needs something to show")

    def test_pairing_ends_by_naming_the_launch_command(self):
        """R12. Four surfaces describe this setup and this is the only one we fully control
        and the only one the user is looking at when the step is due."""
        self.run_cli("login")
        result = self.run_cli("login", "--wait", "0")

        self.assertIn(f"claude {LAUNCH_FLAG} {LAUNCH_TARGET}", result.stdout)
        self.assertIn("tell that session", result.stdout)

    def test_reset_discards_a_pending_code(self):
        self.run_cli("login")
        first = json.loads((self.home / "pending.json").read_text())["user_code"]
        self.run_cli("login", "--reset")
        self.assertEqual(len(self.backend.requests_to("/device/code")), 2)
        self.assertEqual(first, "WXYZ-1234")


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

        marker = json.loads((self.home / "sessions" / "sess-1.json").read_text())
        self.assertEqual(marker["name"], "billing-refactor")
        self.assertEqual(marker["session_id"], "33333333-3333-3333-3333-333333333333")

    def test_the_marker_tells_the_stop_hook_to_skip_this_turn(self):
        """The turn that registers the session is the turn that reports the registration,
        and the user is watching it happen. The other half of this is in test_hooks."""
        self.pair()
        self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")
        marker = json.loads((self.home / "sessions" / "sess-1.json").read_text())
        self.assertTrue(marker["skip_next_turn"])

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
        env["CLAUDE_CODE_SESSION_ID"] = "sess-from-env"
        env["CLAUDE_PID"] = str(self.claude.pid)
        subprocess.run(
            [sys.executable, str(CLI), "connect", "--session-id", ""],
            env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(
            self.backend.last_request("/agent/session")["body"]["claude_session_id"],
            "sess-from-env",
        )

    def test_an_unpaired_machine_is_told_to_log_in(self):
        result = self.run_cli("connect", "x", "--session-id", "sess-1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("/audiochatty-login", result.stdout)
        self.assertEqual(self.backend.requests_to("/agent/session"), [])

    def test_a_revoked_token_says_so_and_writes_no_marker(self):
        self.pair()
        self.backend.reply("/agent/session", 401, {"error": "Unauthorized"})

        result = self.run_cli("connect", "x", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 1)
        self.assertIn("revoked", result.stdout)
        self.assertFalse((self.home / "sessions" / "sess-1.json").exists())

    def test_a_failed_registration_leaves_no_marker(self):
        """The marker is the Stop hook's gate. A marker without a live registration means
        every turn posts into a 404 forever."""
        self.pair()
        result = self.run_cli("connect", "x", "--session-id", "sess-1",
                              backend="http://127.0.0.1:1")
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.home / "sessions" / "sess-1.json").exists())

    def test_a_session_id_cannot_escape_the_sessions_directory(self):
        self.pair()
        self.run_cli("connect", "x", "--session-id", "../../escaped")
        self.assertFalse((self.home.parent / "escaped.json").exists())
        self.assertTrue(list((self.home / "sessions").glob("*.json")))


# -- connect and the return path (R1, R4) -----------------------------------------------


class TestConnectBindsTheChannel(CliTestCase):
    def test_the_channel_is_bound_with_the_registration_it_just_got(self):
        """The whole of R4 in one assertion: the id `/bind` receives is the one the backend
        answered `/agent/session` with, so the channel polls for the right session."""
        self.pair()
        result = self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        bind = self.channel.requests_to("/bind")
        self.assertEqual(len(bind), 1, "exactly one bind per connect")
        self.assertEqual(
            bind[0]["body"]["agent_session_id"], "33333333-3333-3333-3333-333333333333"
        )
        self.assertEqual(bind[0]["body"]["claude_session_id"], "sess-1")
        self.assertEqual(bind[0]["body"]["backend_url"], self.backend.url)
        self.assertEqual(bind[0]["body"]["session_name"], "billing-refactor")

    def test_the_bind_carries_this_machines_token(self):
        """`/bind` refuses a token that doesn't match `credentials.json`, so a channel
        cannot be pointed at a session by some other process on the machine."""
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-1")
        self.assertEqual(
            self.channel.requests_to("/bind")[0]["body"]["token"], "stub-device-token"
        )

    def test_the_marker_records_which_channel_was_bound(self):
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-1")
        marker = json.loads((self.home / "sessions" / "sess-1.json").read_text())
        self.assertEqual(marker["channel_pid"], self.channel.pid)
        self.assertEqual(marker["channel_port"], self.channel.port)

    def test_a_second_connect_in_the_same_session_rebinds_rather_than_refusing(self):
        """The channel treats a re-bind of the same session as a refresh (Phase 2's
        standing obligation). This side has to keep offering it one — the candidate is
        already `bound`, and a naive "only unbound channels" filter would refuse."""
        self.pair()
        self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")
        self.channel.write(bound_to="sess-1")

        result = self.run_cli("connect", "renamed", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.channel.requests_to("/bind")), 2)

    def test_a_channel_bound_to_another_session_is_not_ours(self):
        """Fifteen terminals, one of them already connected. Its channel belongs to it, and
        binding it here would put this session's instructions in that terminal."""
        self.pair()
        self.channel.write(bound_to="somebody-elses-session")

        result = self.run_cli("connect", "x", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.channel.requests_to("/bind"), [])
        self.assertEqual(self.backend.requests_to("/agent/session"), [])


class TestConnectRefusals(CliTestCase):
    """R1: a session that can't be talked to isn't registered at all. Each refusal has to
    happen *before* the registration, or it leaves a live row behind."""

    def test_no_channel_refuses_and_prints_the_relaunch_command(self):
        self.pair()
        self.remove_channel()

        result = self.run_cli("connect", "x", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 1)
        self.assertIn(LAUNCH_FLAG, result.stdout)
        self.assertIn(LAUNCH_TARGET, result.stdout)
        self.assertEqual(self.backend.requests_to("/agent/session"), [])
        self.assertFalse((self.home / "sessions" / "sess-1.json").exists())

    def test_a_session_started_without_the_flag_refuses(self):
        """The failure this catches is invisible everywhere else: the channel process runs
        whether or not channels are enabled, so it binds happily and then every event it
        sends is dropped with no error."""
        self.pair()
        unflagged = FakeClaude(flag=False)
        self.addCleanup(unflagged.stop)
        self.channel.retarget(unflagged.pid)

        result = self.run_cli("connect", "x", "--session-id", "sess-1",
                              claude_pid=unflagged.pid)

        self.assertEqual(result.returncode, 1)
        self.assertIn("without Claude Code's channel flag", result.stdout)
        self.assertIn(LAUNCH_FLAG, result.stdout)
        self.assertEqual(self.backend.requests_to("/agent/session"), [])
        self.assertFalse((self.home / "sessions" / "sess-1.json").exists())

    def test_a_flag_naming_a_different_plugin_does_not_count(self):
        self.pair()
        other = FakeClaude(flag=True, target="plugin:telegram@claude-plugins-official")
        self.addCleanup(other.stop)
        self.channel.retarget(other.pid)

        result = self.run_cli("connect", "x", "--session-id", "sess-1", claude_pid=other.pid)

        self.assertEqual(result.returncode, 1)
        self.assertIn("without Claude Code's channel flag", result.stdout)
        self.assertEqual(self.backend.requests_to("/agent/session"), [])

    def test_the_allowlisted_flag_counts_too(self):
        """`--channels` is what an allowlisted or org-approved plugin is launched with. It
        has to pass, or the plugin breaks on the day it stops needing the scary one."""
        self.pair()
        approved = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)", "--channels", LAUNCH_TARGET],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(approved.wait)
        self.addCleanup(approved.kill)
        self.channel.retarget(approved.pid)

        result = self.run_cli("connect", "x", "--session-id", "sess-1", claude_pid=approved.pid)

        self.assertEqual(result.returncode, 0, result.stdout)

    def test_an_unreadable_command_line_fails_open(self):
        """A `ps` that answers nothing must not become a refusal: the cost of failing closed
        is a plugin that can never register anything, on a platform we haven't seen."""
        self.pair()
        dead = FakeClaude(flag=False)
        pid = dead.pid
        dead.stop()
        self.channel.retarget(pid)

        result = self.run_cli("connect", "x", "--session-id", "sess-1", claude_pid=pid)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.channel.requests_to("/bind")), 1)

    def test_two_channels_for_one_terminal_is_an_error_not_a_guess(self):
        second = FakeChannel(self.home, self.claude.pid, pid=os.getppid())
        self.addCleanup(second.stop)
        self.pair()

        result = self.run_cli("connect", "x", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 1)
        self.assertIn("more than one", result.stdout.lower())
        self.assertEqual(self.backend.requests_to("/agent/session"), [])

    def test_a_channel_that_refuses_the_bind_rolls_the_registration_back(self):
        """The one path that has to undo something: registration landed, the bind didn't,
        and R1 says there is no such thing as a half-registered session."""
        self.pair()
        self.channel.reply(409, {"error": "already_bound"})

        result = self.run_cli("connect", "x", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.home / "sessions" / "sess-1.json").exists())
        self.assertEqual(
            self.backend.last_request("/agent/session/end")["body"]["claude_session_id"],
            "sess-1",
        )

    def test_a_dead_channel_process_is_not_a_channel(self):
        """A rendezvous file outliving its process is how `connect` binds to a corpse. The
        channel prunes its own on startup; this side must not trust one either."""
        self.pair()
        dead_pid = 999_999
        (self.home / "channels" / f"{dead_pid}.json").write_text(
            json.dumps({"pid": dead_pid, "port": self.channel.port, "ancestry": [],
                        "claude_env": {"CLAUDE_PID": str(self.claude.pid)}, "bound": False})
        )
        self.remove_channel()

        result = self.run_cli("connect", "x", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.channel.requests_to("/bind"), [])


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
        self.run_cli("connect", "other-repo", "--session-id", "sess-2")

        result = self.run_cli("status", "--session-id", "sess-1")

        self.assertIn('"other-repo"', result.stdout)

    def test_status_makes_no_network_calls(self):
        self.pair()
        before = len(self.backend.requests)
        self.run_cli("status", "--session-id", "sess-1")
        self.assertEqual(len(self.backend.requests), before)


class TestStatusReportsTheChannel(CliTestCase):
    """This is the command a confused user runs, and R11's failure is the thing it most
    needs to name. It stays local: the rendezvous file already carries `bound` and
    `verified`, so there is nothing here to be slow or to be down."""

    def test_a_verified_session_says_it_can_be_talked_to(self):
        self.pair()
        self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")
        self.channel.write(bound_to="sess-1", verified=True)

        result = self.run_cli("status", "--session-id", "sess-1")

        self.assertIn("You can talk to this session from audiochatty", result.stdout)

    def test_an_unverified_binding_names_the_flag_when_that_is_the_cause(self):
        """Five things land a session in "unverified" and only a process inside it can tell
        them apart — which is why the inbox points here. Two of them are visible from here,
        and the missing flag is the one people actually hit."""
        self.pair()
        unflagged = FakeClaude(flag=False)
        self.addCleanup(unflagged.stop)
        self.channel.retarget(unflagged.pid)
        self.channel.write(bound_to="sess-1", verified=False)
        self.channel.claude_pid = unflagged.pid

        result = self.run_cli("status", "--session-id", "sess-1", claude_pid=unflagged.pid)

        self.assertIn("connected but unconfirmed", result.stdout)
        self.assertIn("started without the channel flag", result.stdout)

    def test_an_unverified_binding_with_the_flag_says_to_finish_a_turn(self):
        """The handshake can only be answered once this command has returned and the model's
        turn begins, so "not yet" is the ordinary reading right after connecting."""
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-1")
        self.channel.write(bound_to="sess-1", verified=False)

        result = self.run_cli("status", "--session-id", "sess-1")

        self.assertIn("connected but unconfirmed", result.stdout)
        self.assertIn("finish a turn", result.stdout)

    def test_a_session_with_no_channel_is_told_how_to_get_one(self):
        self.pair()
        self.remove_channel()

        result = self.run_cli("status", "--session-id", "sess-1")

        self.assertIn("No audiochatty channel is running", result.stdout)
        self.assertIn(LAUNCH_FLAG, result.stdout)

    def test_a_registered_session_whose_channel_restarted_says_to_reconnect(self):
        """The marker survives a channel process; the binding doesn't. What is left is a
        session that reads as registered and silently receives nothing."""
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-1")
        self.channel.write()  # a fresh, unbound channel — the old process is gone

        result = self.run_cli("status", "--session-id", "sess-1")

        self.assertIn("isn't connected", result.stdout)
        self.assertIn("/audiochatty-connect", result.stdout)

    def test_the_channel_report_still_makes_no_network_calls(self):
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-1")
        before = len(self.backend.requests)
        self.run_cli("status", "--session-id", "sess-1")
        self.assertEqual(len(self.backend.requests), before)
        self.assertEqual(self.channel.requests_to("/status"), [])


# -- disconnect -------------------------------------------------------------------------


class TestDisconnect(CliTestCase):
    def test_removes_the_marker_and_ends_the_session(self):
        self.pair()
        self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")

        result = self.run_cli("disconnect", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Stopped sending "billing-refactor"', result.stdout)
        self.assertFalse((self.home / "sessions" / "sess-1.json").exists())
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

    def test_the_channel_is_unbound_as_well(self):
        """A channel left bound keeps polling for instructions addressed to a session the
        user has just closed, and would deliver one into this terminal."""
        self.pair()
        self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")
        self.channel.write(bound_to="sess-1")

        self.run_cli("disconnect", "--session-id", "sess-1")

        unbind = self.channel.requests_to("/unbind")
        self.assertEqual(len(unbind), 1)
        self.assertEqual(unbind[0]["body"]["token"], "stub-device-token")

    def test_another_sessions_channel_is_left_alone(self):
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-1")
        self.channel.write(bound_to="a-different-session")

        self.run_cli("disconnect", "--session-id", "sess-1")

        self.assertEqual(self.channel.requests_to("/unbind"), [])

    def test_an_unreachable_channel_does_not_stop_the_disconnect(self):
        """The unbind is best effort by design: a channel that cannot be reached is one that
        has already exited, which is the same outcome by a different route."""
        self.pair()
        self.run_cli("connect", "x", "--session-id", "sess-1")
        self.channel.write(bound_to="sess-1")
        self.channel.stop()

        result = self.run_cli("disconnect", "--session-id", "sess-1")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Stopped sending", result.stdout)
        self.assertFalse((self.home / "sessions" / "sess-1.json").exists())

    def test_the_marker_goes_even_when_the_backend_is_unreachable(self):
        """Local state first. A failed POST must not leave a hook posting turns for a
        session the user believes they closed."""
        self.pair()
        self.run_cli("connect", "billing-refactor", "--session-id", "sess-1")

        result = self.run_cli("disconnect", "--session-id", "sess-1",
                              backend="http://127.0.0.1:1")

        self.assertEqual(result.returncode, 0)
        self.assertIn("Stopped sending", result.stdout)
        self.assertFalse((self.home / "sessions" / "sess-1.json").exists())


if __name__ == "__main__":
    unittest.main()
