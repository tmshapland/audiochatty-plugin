"""The four slash commands, driven the way Claude Code drives them.

Every test runs `scripts/audiochat.py` as a subprocess against the stub backend, with
`AUDIOCHATTY_HOME` pointed at a temp directory — so the suite is safe to run on a machine
that is already paired, and what it asserts is what the command actually does rather than
what an imported function returns.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stub_backend import StubBackend  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
CLI = SCRIPTS / "audiochat.py"


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.backend = StubBackend()
        self.backend.__enter__()
        self.addCleanup(lambda: self.backend.__exit__(None, None, None))

    def run_cli(self, *args: str, backend: str | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["AUDIOCHATTY_HOME"] = str(self.home)
        env["AUDIOCHATTY_BACKEND_URL"] = backend if backend is not None else self.backend.url
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

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
