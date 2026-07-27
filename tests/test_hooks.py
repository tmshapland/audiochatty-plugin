"""The two hooks, fed recorded hook JSON on stdin — no Claude Code required.

The three things worth breaking here are the three the plan singles out: the marker check
(without it every terminal on the machine starts talking), the `SessionEnd` reason branch
(without it `/clear` silently kills a live registration), and the silent-failure path
(without it a backend outage is felt in somebody's terminal). Each has its own class.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import stop_hook  # noqa: E402
from stub_backend import StubBackend  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
STOP = SCRIPTS / "stop_hook.py"
SESSION_END = SCRIPTS / "session_end_hook.py"

# A blackhole address: the connection neither completes nor is refused, so this exercises
# the *timeout*, which is the case a user would actually feel. 127.0.0.1:1 is refused
# instantly and tests a different branch.
UNROUTABLE = "http://10.255.255.1:9"


class HookTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.backend = StubBackend()
        self.backend.__enter__()
        self.addCleanup(lambda: self.backend.__exit__(None, None, None))
        self.write_credentials()

    def write_credentials(self) -> None:
        (self.home).mkdir(parents=True, exist_ok=True)
        (self.home / "credentials.json").write_text(
            json.dumps({"token": "stub-device-token", "backend_url": self.backend.url})
        )

    def register(self, claude_session_id: str, name: str = "billing-refactor") -> None:
        sessions = self.home / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / f"{claude_session_id}.json").write_text(
            json.dumps({"claude_session_id": claude_session_id, "name": name})
        )

    def run_hook(self, script: Path, payload: dict, backend: str | None = None,
                 timeout: float = 60) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["AUDIOCHATTY_HOME"] = str(self.home)
        env["AUDIOCHATTY_BACKEND_URL"] = backend if backend is not None else self.backend.url
        return subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps(payload),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def stop_payload(self, **overrides) -> dict:
        """A recorded `Stop` hook input, as documented in the hooks reference."""
        payload = {
            "session_id": "sess-1",
            "prompt_id": "550e8400-e29b-41d4-a716-446655440000",
            "transcript_path": str(self.home / "transcript.jsonl"),
            "cwd": "/Users/mike/repos/audiochat",
            "permission_mode": "default",
            "hook_event_name": "Stop",
            "last_assistant_message": "I finished the refactor and the tests pass.",
            "stop_hook_active": False,
        }
        payload.update(overrides)
        return payload


# -- the marker check --------------------------------------------------------------------


class TestStopHookMarkerCheck(HookTestCase):
    def test_an_unregistered_session_sends_nothing(self):
        """Stop hooks are global. This is the check that keeps the other fourteen
        terminals open that day silent."""
        result = self.run_hook(STOP, self.stop_payload())

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.backend.requests_to("/agent/turn"), [])
        self.assertEqual(result.stdout, "")

    def test_a_registered_session_posts_the_turn(self):
        self.register("sess-1")
        result = self.run_hook(STOP, self.stop_payload())

        self.assertEqual(result.returncode, 0)
        request = self.backend.last_request("/agent/turn")
        self.assertIsNotNone(request)
        self.assertEqual(request["authorization"], "Bearer stub-device-token")
        self.assertEqual(request["body"]["claude_session_id"], "sess-1")
        self.assertEqual(
            request["body"]["last_assistant_message"],
            "I finished the refactor and the tests pass.",
        )
        self.assertEqual(request["body"]["cwd"], "/Users/mike/repos/audiochat")

    def test_only_the_registered_session_of_two_posts(self):
        self.register("sess-1")
        self.run_hook(STOP, self.stop_payload(session_id="sess-2"))
        self.run_hook(STOP, self.stop_payload(session_id="sess-1"))

        posted = [r["body"]["claude_session_id"] for r in self.backend.requests_to("/agent/turn")]
        self.assertEqual(posted, ["sess-1"])

    def test_an_unpaired_machine_sends_nothing(self):
        (self.home / "credentials.json").unlink()
        self.register("sess-1")
        result = self.run_hook(STOP, self.stop_payload())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.backend.requests_to("/agent/turn"), [])

    def test_an_empty_turn_is_not_sent(self):
        """A turn with no text and no tool calls would be a 400. Don't make the backend
        say so once per empty turn."""
        self.register("sess-1")
        result = self.run_hook(STOP, self.stop_payload(last_assistant_message="",
                                                       transcript_path=None))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.backend.requests_to("/agent/turn"), [])

    def test_garbage_on_stdin_is_survivable(self):
        self.register("sess-1")
        for raw in ("", "   ", "not json", "[]", "null"):
            with self.subTest(raw=raw):
                result = subprocess.run(
                    [sys.executable, str(STOP)],
                    input=raw,
                    env={**os.environ, "AUDIOCHATTY_HOME": str(self.home),
                         "AUDIOCHATTY_BACKEND_URL": self.backend.url},
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)


# -- the silent-failure path and its latency ---------------------------------------------


class TestStopHookNeverBlocks(HookTestCase):
    def test_a_refused_connection_is_silent_and_instant(self):
        self.register("sess-1")
        started = time.monotonic()
        result = self.run_hook(STOP, self.stop_payload(), backend="http://127.0.0.1:1")
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertLess(elapsed, 3.0, "a refused connection must cost nothing")

    def test_an_unreachable_backend_costs_one_timeout_then_nothing(self):
        """The plan's requirement is that a backend which is down or asleep never makes
        the terminal wait. A short timeout alone does not deliver that — it makes *every*
        turn pay it. The breaker is what turns one cost into no cost."""
        self.register("sess-1")

        first_started = time.monotonic()
        first = self.run_hook(STOP, self.stop_payload(), backend=UNROUTABLE)
        first_elapsed = time.monotonic() - first_started

        second_started = time.monotonic()
        second = self.run_hook(STOP, self.stop_payload(), backend=UNROUTABLE)
        second_elapsed = time.monotonic() - second_started

        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        self.assertLess(first_elapsed, 6.0, f"first turn took {first_elapsed:.1f}s")
        self.assertLess(second_elapsed, 1.5, f"breaker did not hold: {second_elapsed:.1f}s")
        self.assertTrue((self.home / "state.json").exists())

    def test_a_slow_backend_is_abandoned_rather_than_waited_on(self):
        self.register("sess-1")
        self.backend.reply("/agent/turn", 202, {"status": "queued"}, delay=10.0)

        started = time.monotonic()
        result = self.run_hook(STOP, self.stop_payload(), timeout=30)
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 0)
        self.assertLess(elapsed, 6.0, f"the hook waited {elapsed:.1f}s on a slow backend")

    def test_a_404_does_not_trip_the_breaker(self):
        """A disconnected session 404s on every turn. That is a working backend giving
        the right answer, and silencing a healthy machine over it would be a bug."""
        self.register("sess-1")
        self.backend.reply("/agent/turn", 404, {"error": "No active session"})

        self.run_hook(STOP, self.stop_payload())
        self.run_hook(STOP, self.stop_payload())

        self.assertEqual(len(self.backend.requests_to("/agent/turn")), 2)

    def test_a_recovered_backend_clears_the_breaker(self):
        self.register("sess-1")
        self.run_hook(STOP, self.stop_payload(), backend="http://127.0.0.1:1")
        self.assertTrue((self.home / "state.json").exists())

        # A CLI command against a working backend is the other thing that clears it, but
        # the direct route is a successful turn once the cooldown has passed.
        (self.home / "state.json").write_text(json.dumps({"backend_down_until": 0}))
        self.run_hook(STOP, self.stop_payload())

        self.assertEqual(len(self.backend.requests_to("/agent/turn")), 1)
        self.assertEqual(json.loads((self.home / "state.json").read_text()), {})


# -- what the turn payload contains ------------------------------------------------------


class TestTurnPayload(HookTestCase):
    def write_transcript(self, rows: list[dict]) -> str:
        path = self.home / "transcript.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return str(path)

    @staticmethod
    def user(text: str) -> dict:
        return {"type": "user", "message": {"role": "user", "content": text}}

    @staticmethod
    def tool_result(tool_use_id: str) -> dict:
        return {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}],
            },
        }

    @staticmethod
    def assistant(*blocks: dict) -> dict:
        return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}

    @staticmethod
    def tool_use(name: str, ident: str = "toolu_1") -> dict:
        return {"type": "tool_use", "id": ident, "name": name, "input": {}}

    def test_tool_calls_are_scoped_to_this_turn(self):
        path = self.write_transcript([
            self.user("first question"),
            self.assistant(self.tool_use("Grep", "t1")),
            self.tool_result("t1"),
            self.assistant({"type": "text", "text": "done"}),
            self.user("second question"),
            self.assistant(self.tool_use("Edit", "t2"), self.tool_use("Edit", "t3")),
            self.tool_result("t2"),
            self.assistant(self.tool_use("Bash", "t4")),
            self.tool_result("t4"),
            self.assistant({"type": "text", "text": "finished"}),
        ])

        self.assertEqual(stop_hook.tool_calls_from_transcript(path), ["Edit", "Edit", "Bash"])

    def test_a_tool_result_does_not_end_the_turn(self):
        """Tool results are user-role messages. Treating one as the start of the turn
        would drop every tool call before it."""
        path = self.write_transcript([
            self.user("go"),
            self.assistant(self.tool_use("Read", "t1")),
            self.tool_result("t1"),
            self.assistant(self.tool_use("Write", "t2")),
        ])
        self.assertEqual(stop_hook.tool_calls_from_transcript(path), ["Read", "Write"])

    def test_meta_rows_do_not_end_the_turn(self):
        path = self.write_transcript([
            self.user("go"),
            self.assistant(self.tool_use("Read", "t1")),
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<reminder>"}},
            self.assistant(self.tool_use("Edit", "t2")),
        ])
        self.assertEqual(stop_hook.tool_calls_from_transcript(path), ["Read", "Edit"])

    def test_subagent_traffic_is_excluded(self):
        path = self.write_transcript([
            self.user("go"),
            self.assistant(self.tool_use("Task", "t1")),
            {**self.assistant(self.tool_use("Grep", "t2")), "isSidechain": True},
            self.assistant({"type": "text", "text": "done"}),
        ])
        self.assertEqual(stop_hook.tool_calls_from_transcript(path), ["Task"])

    def test_a_turn_with_no_tools_is_empty_not_an_error(self):
        path = self.write_transcript([
            self.user("hello"),
            self.assistant({"type": "text", "text": "hi"}),
        ])
        self.assertEqual(stop_hook.tool_calls_from_transcript(path), [])

    def test_a_missing_or_corrupt_transcript_is_survivable(self):
        for path in (None, "", "/nonexistent/transcript.jsonl"):
            with self.subTest(path=path):
                self.assertEqual(stop_hook.tool_calls_from_transcript(path), [])

        corrupt = self.home / "corrupt.jsonl"
        corrupt.write_text("{not json\n" + json.dumps(self.assistant(self.tool_use("Edit"))) + "\n")
        self.assertEqual(stop_hook.tool_calls_from_transcript(str(corrupt)), ["Edit"])

    def test_the_payload_is_exactly_the_four_documented_keys(self):
        self.register("sess-1")
        path = self.write_transcript([
            self.user("go"),
            self.assistant(self.tool_use("Edit", "t1")),
        ])
        self.run_hook(STOP, self.stop_payload(transcript_path=path))

        body = self.backend.last_request("/agent/turn")["body"]
        self.assertEqual(
            set(body),
            {"claude_session_id", "last_assistant_message", "tool_calls", "stop_reason", "cwd"},
        )
        self.assertEqual(body["tool_calls"], ["Edit"])

    def test_nothing_from_the_hook_input_leaks_into_the_payload(self):
        """`transcript_path` and `permission_mode` are on the way in and must not be on
        the way out — the backend drops them, but the plugin should not send them."""
        self.register("sess-1")
        self.run_hook(STOP, self.stop_payload())
        body = self.backend.last_request("/agent/turn")["body"]
        self.assertNotIn("transcript_path", body)
        self.assertNotIn("permission_mode", body)


# -- the SessionEnd reason branch ---------------------------------------------------------


class TestSessionEndReasons(HookTestCase):
    def end_payload(self, reason: str, session_id: str = "sess-1") -> dict:
        return {
            "session_id": session_id,
            "transcript_path": str(self.home / "transcript.jsonl"),
            "cwd": "/Users/mike/repos/audiochat",
            "hook_event_name": "SessionEnd",
            "reason": reason,
        }

    def test_the_reason_table(self):
        """`/clear` and `/resume` keep the session id and the terminal. Ending the
        registration there kills something the user is still using, and they find out by
        never hearing from it again."""
        cases = [
            ("clear", False),
            ("resume", False),
            ("bypass_permissions_disabled", False),
            ("logout", True),
            ("prompt_input_exit", True),
            ("other", True),
            # Not a documented value. Unknown reasons must not end a session: a reason
            # added by a future Claude Code release would otherwise silently break this.
            ("something_new", False),
            ("", False),
        ]
        for reason, should_end in cases:
            with self.subTest(reason=reason):
                self.setUp()
                self.register("sess-1")
                self.run_hook(SESSION_END, self.end_payload(reason))

                ended = bool(self.backend.requests_to("/agent/session/end"))
                self.assertEqual(ended, should_end)
                self.assertEqual(
                    (self.home / "sessions" / "sess-1.json").exists(),
                    not should_end,
                    "the marker survives everything but a real ending",
                )

    def test_a_missing_reason_field_is_not_an_ending(self):
        self.register("sess-1")
        payload = self.end_payload("clear")
        del payload["reason"]
        self.run_hook(SESSION_END, payload)
        self.assertEqual(self.backend.requests_to("/agent/session/end"), [])

    def test_an_unregistered_session_is_ignored(self):
        result = self.run_hook(SESSION_END, self.end_payload("logout"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.backend.requests_to("/agent/session/end"), [])

    def test_a_failed_end_still_removes_the_marker(self):
        """The session is over either way. A marker left behind is a marker that can
        never fire, but it would keep showing up in /audiochatty-status."""
        self.register("sess-1")
        result = self.run_hook(SESSION_END, self.end_payload("logout"),
                               backend="http://127.0.0.1:1")
        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.home / "sessions" / "sess-1.json").exists())

    def test_garbage_on_stdin_is_survivable(self):
        for raw in ("", "not json", "[]"):
            with self.subTest(raw=raw):
                result = subprocess.run(
                    [sys.executable, str(SESSION_END)],
                    input=raw,
                    env={**os.environ, "AUDIOCHATTY_HOME": str(self.home),
                         "AUDIOCHATTY_BACKEND_URL": self.backend.url},
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
