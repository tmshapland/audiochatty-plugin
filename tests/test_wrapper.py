"""`audiochatty run`, driven the way a person drives it.

The successor to `tests/test_channel.py`, which was deleted along with the channel it drove
— everything that file asserted about behaviour the wrapper still has is asserted here
instead.

Every test here starts the **real wrapper as a subprocess**, with a **real pty for its own
stdin** and a **fake `claude`** inside it, and asserts on bytes that actually reached that
child. Nothing is stubbed at the module level, and that is deliberate — this component is
three processes and two ptys, and the failures worth catching all live in the wiring:

- a fake that let the wrapper write into a pipe instead of a pty would never notice raw mode
  being broken, and raw mode is the difference between a keystroke arriving now and arriving
  when the user presses Enter;
- a fake `claude` that did not put *its* tty into raw mode would receive a bracketed paste
  mangled by the line discipline (`\\r` silently becoming `\\n`, and nothing arriving at all
  until a newline shows up), which is a fixture bug that reads exactly like a product bug.
  Claude Code's TUI raw-modes its terminal; so does `FakeClaude`.

The one thing these tests cannot cover is whether Claude Code's *interface* interprets a
bracketed paste the way a terminal emulator's would. That needs a human at a keyboard; see
`wrapper/README.md`.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stub_backend import StubBackend  # noqa: E402

from wrapper import inject, poller  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = PLUGIN_ROOT / "wrapper" / "__main__.py"
LAUNCHER = PLUGIN_ROOT / "wrapper" / "audiochatty"

TOKEN = "device-token-for-tests"
# Short enough to keep the suite quick, long enough that "held back" and "let through" are
# not the same measurement.
QUIET_PERIOD = 0.6

# The poller's cadences, compressed. The real numbers are 5s / 30s / 60s and a suite that
# used them would take twenty minutes; what the tests actually assert on is the *shape* — that a
# failure backs off far longer than a success, that an empty answer is cheap, that nothing
# spins. See `poller._tunable`.
POLL = {
    "AUDIOCHATTY_POLL_ACTIVE": "0.1",
    "AUDIOCHATTY_POLL_IDLE": "0.3",
    "AUDIOCHATTY_POLL_COOLDOWN": "1.0",
    "AUDIOCHATTY_POLL_TIMEOUT": "3.0",
}

# Ids are uuids in production (the backend rejects anything else on the ack path), so they
# are uuids here too.
MSG_ID = "55555555-5555-5555-5555-555555555555"
OTHER_MSG_ID = "66666666-6666-6666-6666-666666666666"
AGENT_SESSION = "33333333-3333-3333-3333-333333333333"

_names = itertools.count()


def one_message(text: str = "run the tests", message_id: str = MSG_ID) -> dict:
    return {
        "messages": [
            {
                "id": message_id,
                "text": text,
                "sender_name": "Mike",
                "created_at": "2026-07-29T10:00:00Z",
            }
        ]
    }

# A fake `claude`: raw-modes its tty like the real TUI does, appends everything it is given
# to a log, and exits with a code the test chooses so exit-code passthrough is observable.
FAKE_CLAUDE = '''#!{python}
import os, sys, tty
tty.setraw(0)
sys.stdout.write("fake claude ready\\r\\n")
sys.stdout.flush()
log = open(os.environ["FAKE_CLAUDE_LOG"], "wb", buffering=0)
log.write(("argv=%r\\n" % (sys.argv[1:],)).encode())
buf = b""
while True:
    data = os.read(0, 4096)
    if not data:
        break
    log.write(data)
    buf += data
    if b"EXIT" in buf:
        break
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
'''


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.02):
    """Poll until true. Every wait in this file is on another process doing something, so
    they are all this shape and none of them is a bare `sleep`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


class WrappedSession:
    """A live `audiochatty run` with a fake `claude` in it.

    `stdin_master` is the far end of the wrapper's own terminal — writing to it is the test
    typing on the keyboard, which is the only honest way to exercise the never-type-over-
    the-user behaviour.
    """

    def __init__(self, home: Path, *, exit_code: int = 0, quiet_period: float = QUIET_PERIOD,
                 extra_args: list[str] | None = None, launcher: bool = False,
                 poll: dict | None = None, backend: str | None = None):
        import pty

        # Per-session names, because the restart test in `TestDelivery` runs two wrappers
        # against the same `AUDIOCHATTY_HOME` — one shared log would make "the second
        # session never saw it" unprovable.
        tag = next(_names)
        self.home = home
        self.log = home / f"child-{tag}.log"
        self.claude = home / f"fakeclaude-{tag}"
        self.claude.write_text(FAKE_CLAUDE.format(python=sys.executable))
        self.claude.chmod(0o755)

        env = dict(os.environ)
        env.update(
            AUDIOCHATTY_HOME=str(home),
            FAKE_CLAUDE_LOG=str(self.log),
            FAKE_CLAUDE_EXIT=str(exit_code),
            # **Never left unset.** The wrapper connects itself at launch, and
            # `audiochat.backend_url()` falls back to the *deployed* backend when nothing
            # says otherwise — so an unset value here would have this suite registering
            # sessions against production. Unreachable by default, for the same reason
            # `bind()` below is: a test that has not asked to talk to a backend must not.
            AUDIOCHATTY_BACKEND_URL=backend or "http://127.0.0.1:1",
            **POLL,
        )
        env.update(poll or {})
        env.pop("AUDIOCHATTY_DEBUG", None)

        argv = [str(LAUNCHER)] if launcher else [sys.executable, str(WRAPPER)]
        argv += ["run", "--claude-bin", str(self.claude), "--quiet-period", str(quiet_period)]
        argv += extra_args or []

        self.stdin_master, slave = pty.openpty()
        self.proc = subprocess.Popen(
            argv, stdin=slave, stdout=slave, stderr=subprocess.PIPE, env=env
        )
        os.close(slave)

        # A real terminal emulator never stops reading its master, and a fixture that does
        # is not a faithful one — it fills the pty's output buffer and the wrapper blocks
        # writing into a screen nobody is looking at. This thread is the terminal.
        self._screen = bytearray()
        self._reading = True
        self._closed = False
        self._stderr_text: str | None = None
        self._reader = threading.Thread(target=self._drain_screen, daemon=True)
        self._reader.start()

        self.record = wait_for(self._read_record, timeout=10)
        if not self.record:  # pragma: no cover - a startup failure, not a test case
            self.stop()
            raise AssertionError(f"wrapper never wrote a rendezvous file: {self.stderr()}")

        # Wait for the child to be up and to have raw-moded its own tty before any test
        # types. Without this a test races the fixture and its keystrokes land in a line
        # discipline that is still buffering them, which reads exactly like the wrapper
        # dropping input.
        if not wait_for(lambda: b"argv=" in self.child_saw(), timeout=10):
            self.stop()  # pragma: no cover
            raise AssertionError(f"fake claude never started: {self.stderr()}")

    # -- state --

    @property
    def rendezvous(self) -> Path:
        return self.home / "wrappers" / f"{self.proc.pid}.json"

    def _read_record(self) -> dict | None:
        if not self.rendezvous.exists():
            return None
        record = json.loads(self.rendezvous.read_text() or "{}")
        return record or None

    def reread(self) -> dict:
        self.record = self._read_record() or {}
        return self.record

    @property
    def port(self) -> int:
        return int(self.record["port"])

    @property
    def session_id(self) -> str:
        return str(self.record["expected_session_id"])

    # -- driving it --

    def call(self, path: str, body: dict | None = None, method: str = "POST") -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as err:
            return err.code, json.load(err)

    def bind(self, **overrides) -> tuple[int, dict]:
        body = {
            "agent_session_id": AGENT_SESSION,
            "claude_session_id": self.session_id,
            # Deliberately unreachable by default: a test that has not asked to talk to a
            # backend must not have its poll loop reach one.
            "backend_url": "http://127.0.0.1:1/",
            "token": TOKEN,
            "session_name": "laptop",
        }
        body.update(overrides)
        return self.call("/bind", body)

    @property
    def ledger(self) -> Path:
        return self.home / "wrappers" / f"{self.record['claude_session_id']}.delivered.json"

    def type(self, data: bytes) -> None:
        """Type on the keyboard, for real, into the wrapper's own terminal."""
        os.write(self.stdin_master, data)

    def child_saw(self) -> bytes:
        try:
            return self.log.read_bytes()
        except OSError:
            return b""

    def screen(self) -> bytes:
        """Everything the wrapper has written back to the real terminal."""
        return bytes(self._screen)

    def _drain_screen(self) -> None:
        """Poll rather than block, and stop when `stop()` says so.

        A blocking `os.read` here would be simpler and is what this was, but it is two
        hazards at once and both of them cost an afternoon. On macOS, `os.close` of a pty
        master **blocks** while another thread is inside a read on it — so teardown hung.
        And a read left blocked on a closed fd is a read on whatever fd number gets handed
        out next, which in a test that starts two sessions is the *next session's* pty:
        keystrokes meant for the second wrapper vanish into a thread belonging to the first.
        Neither reads like a fixture bug from the outside; the first looks like a wrapper
        that will not exit and the second like a wrapper that drops input.
        """
        import select

        while self._reading:
            try:
                ready, _, _ = select.select([self.stdin_master], [], [], 0.05)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                data = os.read(self.stdin_master, 65536)
            except OSError:
                return
            if not data:
                return
            self._screen.extend(data)

    def stderr(self) -> str:
        if self._stderr_text is not None:
            return self._stderr_text
        if self.proc.stderr and self.proc.poll() is not None:
            return self.proc.stderr.read().decode(errors="replace")
        return ""

    # -- teardown --

    def stop(self, timeout: float = 10.0) -> int | None:
        if self.proc.poll() is None:
            try:
                self.type(b"EXIT")
            except OSError:
                pass
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self.proc.kill()
                self.proc.wait(timeout=5)
        # The reader first, then the fd. See `_drain_screen` for why that order is not
        # optional.
        self._reading = False
        self._reader.join(timeout=2)
        # **Exactly once**, which is why the flag exists rather than a bare `try/except
        # OSError`. `stop()` is called twice on any session a test stops itself, since
        # `tearDown` stops them all again — and a second `os.close` of that number is not a
        # harmless `EBADF` once another `pty.openpty()` has been handed the same number. It
        # closes *that* session's terminal, and the symptom is the next wrapper in the suite
        # ignoring every keystroke and having to be killed after a 10-second wait.
        if not self._closed:
            self._closed = True
            try:
                os.close(self.stdin_master)
            except OSError:
                pass
        # Cached because the pipe is closed right below, and `stop()` is called twice on any
        # session a test stops itself — once there, once again from `tearDown` — so a second
        # `.read()` on an already-closed file must not happen.
        if self.proc.stderr and self._stderr_text is None:
            try:
                self._stderr_text = self.proc.stderr.read().decode(errors="replace")
            except ValueError:
                self._stderr_text = ""
            self.proc.stderr.close()
        return self.proc.returncode


class WrapperTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "credentials.json").write_text(json.dumps({"token": TOKEN}))
        self.sessions: list[WrappedSession] = []

    def tearDown(self) -> None:
        for session in self.sessions:
            session.stop()
        self._tmp.cleanup()

    def start(self, **kwargs) -> WrappedSession:
        session = WrappedSession(self.home, **kwargs)
        self.sessions.append(session)
        return session


class TestRendezvous(WrapperTestCase):
    def test_file_contents_and_permissions(self):
        session = self.start()
        record = session.record

        self.assertEqual(record["version"], 1)
        self.assertEqual(record["kind"], "wrapper")
        self.assertEqual(record["pid"], session.proc.pid)
        self.assertFalse(record["bound"])
        self.assertFalse(record["verified"])
        self.assertIsNone(record["claude_session_id"])
        self.assertGreater(record["port"], 0)
        # The child is a real process and it is ours.
        self.assertNotEqual(record["child_pid"], session.proc.pid)
        self.assertTrue(record["expected_session_id"])

        # 0600, in a 0700 directory, and the token is not in here. (The directory mode came
        # over from `test_channel.py`: the ledgers live beside these files, and
        # the whole point of the token check is that neither is worth reading.)
        self.assertEqual(session.rendezvous.stat().st_mode & 0o777, 0o600)
        self.assertEqual(session.rendezvous.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(TOKEN, session.rendezvous.read_text())

    def test_wrapper_env_reaches_the_child(self):
        """In one assertion: the child's environment carries the port and pid, which is
        the whole of how `/audiochatty-connect` finds its wrapper."""
        session = self.start()
        environ = Path(f"/proc/{session.record['child_pid']}/environ")
        if environ.exists():  # Linux
            values = environ.read_bytes().decode(errors="replace")
        else:  # macOS
            values = subprocess.run(
                ["ps", "-Eww", "-o", "command=", "-p", str(session.record["child_pid"])],
                capture_output=True,
                text=True,
            ).stdout
        self.assertIn(f"AUDIOCHATTY_WRAPPER_PORT={session.port}", values)
        self.assertIn(f"AUDIOCHATTY_WRAPPER_PID={session.proc.pid}", values)

    def test_session_id_is_passed_to_claude(self):
        """The wrapper mints the session id and hands it to `claude`, which is what makes the
        `session_mismatch` check below possible at all."""
        session = self.start()
        argv = wait_for(lambda: b"argv=" in session.child_saw() and session.child_saw())
        self.assertIn("--session-id", argv.decode())
        self.assertIn(session.session_id, argv.decode())

    def test_resume_leaves_the_session_id_alone(self):
        """`--resume` decides the session itself, so the wrapper must not mint one — and then
        it has nothing to cross-check, so `/bind` pins whatever it is first given."""
        session = self.start(extra_args=["--", "--resume"])
        self.assertIsNone(session.record["expected_session_id"])
        status, body = session.bind(claude_session_id="a-session-we-did-not-choose")
        self.assertEqual((status, body["status"]), (200, "bound"))

    def test_stale_entries_are_pruned_at_startup(self):
        """A `kill -9` leaves a file behind, and a stale file is how `connect` binds to a
        corpse — or to a port something else has since been given."""
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        stale = self.home / "wrappers" / f"{dead.pid}.json"
        stale.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stale.write_text(json.dumps({"version": 1, "pid": dead.pid, "port": 1}))
        # A live one, for contrast: pruning must be about liveness, not about age.
        live = self.home / "wrappers" / f"{os.getpid()}.json"
        live.write_text(json.dumps({"version": 1, "pid": os.getpid(), "port": 2}))

        session = self.start()
        self.assertFalse(stale.exists())
        self.assertTrue(live.exists())
        self.assertTrue(session.rendezvous.exists())

    def test_file_is_removed_on_exit(self):
        session = self.start()
        path = session.rendezvous
        session.stop()
        self.assertFalse(path.exists())

    def test_child_exit_code_is_the_wrapper_exit_code(self):
        """So `audiochatty run` is a drop-in for `claude` inside a script."""
        session = self.start(exit_code=7)
        self.assertEqual(session.stop(), 7)

    def test_launcher_script_works(self):
        """`wrapper/audiochatty` is the command the docs tell people to alias. It has to run
        the same program the tests run."""
        session = self.start(launcher=True)
        self.assertEqual(session.record["kind"], "wrapper")


class TestBind(WrapperTestCase):
    def test_status_before_binding(self):
        session = self.start()
        status, body = session.call("/status", method="GET")
        self.assertEqual(status, 200)
        self.assertFalse(body["bound"])
        self.assertFalse(body["verified"])
        self.assertEqual(body["expected_session_id"], session.session_id)
        self.assertNotIn("token", json.dumps(body))

    def test_bind_marks_verified_immediately(self):
        """There is no handshake any more. The wrapper owns the pty, so binding *is* the
        proof that the session can be reached."""
        session = self.start()
        status, body = session.bind()
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "bound")
        self.assertTrue(body["verified"])

        record = session.reread()
        self.assertTrue(record["bound"])
        self.assertTrue(record["verified"])
        self.assertTrue(record["verified_at"])
        self.assertEqual(record["claude_session_id"], session.session_id)
        self.assertEqual(record["backend_url"], "http://127.0.0.1:1")  # trailing / stripped
        self.assertEqual(record["session_name"], "laptop")

    def test_bind_refuses_a_token_that_is_not_this_machines(self):
        """The token is what stops a stray local process pointing this wrapper at a session
        it does not own."""
        session = self.start()
        status, body = session.bind(token="not-the-device-token")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "token_mismatch")
        self.assertFalse(session.reread()["bound"])

    def test_bind_refuses_a_session_this_wrapper_did_not_start(self):
        """The safety check. Without it, a plain `claude` run inside a wrapped session
        inherits our port and can point someone's spoken instructions at a terminal they
        cannot see."""
        session = self.start()
        status, body = session.bind(claude_session_id="some-other-session")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "session_mismatch")
        self.assertEqual(body["expected_session_id"], session.session_id)
        self.assertFalse(session.reread()["bound"])

    def test_missing_fields_and_bad_json(self):
        session = self.start()
        status, body = session.call("/bind", {"token": TOKEN})
        self.assertEqual((status, body["error"]), (400, "missing_fields"))

        request = urllib.request.Request(
            f"http://127.0.0.1:{session.port}/bind", data=b"{not json", method="POST"
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 400)

    def test_rebinding_the_same_session_refreshes_in_place(self):
        """`/audiochatty-connect` run twice in the same terminal, or run again after
        re-pairing. Not an error — and the ledger a later phase keeps must not be reset."""
        session = self.start()
        session.bind()
        status, body = session.bind(agent_session_id="agent-session-2")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "rebound")
        self.assertTrue(body["verified"])
        self.assertEqual(session.reread()["agent_session_id"], "agent-session-2")

    def test_unbind_then_status(self):
        session = self.start()
        session.bind()
        status, body = session.call("/unbind", {"token": TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(body["claude_session_id"], session.session_id)

        record = session.reread()
        self.assertFalse(record["bound"])
        self.assertFalse(record["verified"])
        self.assertIsNone(record["claude_session_id"])
        # The generation moved, which is what stops a poll loop mid-sleep.
        self.assertEqual(record["generation"], 2)

    def test_unbind_refuses_a_bad_token(self):
        session = self.start()
        session.bind()
        status, _ = session.call("/unbind", {"token": "wrong"})
        self.assertEqual(status, 403)
        self.assertTrue(session.reread()["bound"])

    def test_unknown_routes_404(self):
        session = self.start()
        self.assertEqual(session.call("/nope", method="GET")[0], 404)
        self.assertEqual(session.call("/nope", {})[0], 404)


class TestInjection(WrapperTestCase):
    def test_an_unbound_wrapper_types_nothing(self):
        """The "costs nothing until connected" property, moved from the old channel: until a
        session is bound, this process is inert."""
        session = self.start()
        status, body = session.call("/inject", {"token": TOKEN, "text": "do the thing"})
        self.assertEqual((status, body["error"]), (409, "not_bound"))
        time.sleep(QUIET_PERIOD + 0.3)
        self.assertNotIn(b"\x1b[200~", session.child_saw())

    def test_inject_refuses_a_bad_token(self):
        session = self.start()
        session.bind()
        status, body = session.call("/inject", {"token": "wrong", "text": "hi"})
        self.assertEqual((status, body["error"]), (403, "token_mismatch"))

    def test_multi_line_text_arrives_as_one_paste(self):
        """The failure this prevents: Claude Code submitting at the first newline and
        treating the rest of a spoken instruction as a second prompt."""
        session = self.start()
        session.bind()
        text = "first line\nsecond line\n\nlast paragraph"
        self.assertEqual(session.call("/inject", {"token": TOKEN, "text": text})[0], 202)

        # Waits for the *Enter*, not just the closing marker: those are two writes now, a
        # `SUBMIT_DELAY` apart, so stopping at `\x1b[201~` would race the thing being asserted.
        saw = wait_for(lambda: session.child_saw().endswith(b"\x1b[201~\r") and session.child_saw())
        self.assertIn(b"\x1b[200~", saw)
        # One paste, one Enter, and the line breaks are CRs inside it — what a terminal
        # emulator sends when a human pastes.
        self.assertEqual(saw.count(b"\x1b[200~"), 1)
        self.assertEqual(saw.count(b"\x1b[201~"), 1)
        body = saw.split(b"\x1b[200~", 1)[1].split(b"\x1b[201~", 1)[0]
        self.assertEqual(body, b"first line\rsecond line\r\rlast paragraph")
        self.assertTrue(saw.endswith(b"\x1b[201~\r"))

    def test_an_escape_in_the_text_cannot_end_the_paste_early(self):
        """The sharp edge: `\\x1b[201~` inside the text would close the paste and hand the
        rest to the terminal as live keystrokes."""
        session = self.start()
        session.bind()
        session.call("/inject", {"token": TOKEN, "text": "before \x1b[201~\x1b[2J after"})
        saw = wait_for(lambda: b"\x1b[201~" in session.child_saw() and session.child_saw())
        self.assertEqual(saw.count(b"\x1b[201~"), 1)
        self.assertNotIn(b"\x1b[2J", saw)

    def test_the_quiet_period_holds_an_injection_back_while_you_are_typing(self):
        """The whole point of the wrapper over `tmux send-keys`: it can see the keyboard,
        so it can wait for a pause instead of splicing into a half-written line."""
        session = self.start()
        session.bind()

        session.type(b"i was in the middle of")
        wait_for(lambda: b"middle of" in session.child_saw())
        started = time.monotonic()
        session.call("/inject", {"token": TOKEN, "text": "spoken instruction"})

        # Keep typing across what would otherwise have been the quiet period.
        for _ in range(4):
            time.sleep(QUIET_PERIOD / 3)
            session.type(b" more")
            self.assertNotIn(b"\x1b[200~", session.child_saw())

        # Stop typing; now it lands.
        saw = wait_for(lambda: b"\x1b[200~" in session.child_saw() and session.child_saw(),
                       timeout=QUIET_PERIOD + 3)
        held = time.monotonic() - started
        self.assertIn(b"spoken instruction", saw)
        self.assertGreater(held, QUIET_PERIOD)
        # It waits for a pause, and it does not lose what the user was typing.
        self.assertIn(b"i was in the middle of more more more more", saw)
        self.assertLess(saw.index(b"more more"), saw.index(b"\x1b[200~"))

    def test_an_idle_session_is_typed_into_immediately(self):
        """The common case: nobody is at the keyboard, which is why they are talking to their
        phone. A quiet period that also delayed *this* would be a bug, not caution."""
        session = self.start()
        session.bind()
        started = time.monotonic()
        session.call("/inject", {"token": TOKEN, "text": "go"})
        wait_for(lambda: b"\x1b[200~" in session.child_saw(), timeout=3)
        self.assertIn(b"go", session.child_saw())
        self.assertLess(time.monotonic() - started, QUIET_PERIOD)

    def test_status_reports_a_pending_injection(self):
        session = self.start(quiet_period=30.0)
        session.bind()
        session.type(b"x")
        wait_for(lambda: b"x" in session.child_saw())
        session.call("/inject", {"token": TOKEN, "text": "held"})
        self.assertEqual(session.call("/status", method="GET")[1]["pending_injections"], 1)


class TestDelivery(WrapperTestCase):
    """A message sitting in the backend queue ends up typed into the session.

    Every test here runs the real poll loop against `tests/stub_backend.py` — the same stub
    the old channel's tests used, speaking the same three routes, which demonstrates that
    nothing about the queue changed when the delivery mechanism did.
    """

    def test_a_queued_message_is_typed_once_and_acked_once(self):
        with StubBackend() as backend:
            backend.reply("/agent/inbound", 200, one_message())
            session = self.start()
            session.bind(backend_url=backend.url)

            saw = wait_for(lambda: b"\x1b[200~" in session.child_saw() and session.child_saw())
            self.assertIn(b"run the tests", saw)

            acks = wait_for(lambda: backend.requests_to("/agent/inbound/ack"))
            self.assertEqual(acks[0]["body"]["message_ids"], [MSG_ID])
            self.assertEqual(acks[0]["authorization"], f"Bearer {TOKEN}")

            # The poll names the session it was bound to, and nothing else.
            polls = backend.requests_to("/agent/inbound")
            self.assertEqual(polls[0]["query"], {"session_id": AGENT_SESSION})
            self.assertEqual(polls[0]["method"], "GET")

            # Keep polling for several more rounds; it must not type it again or ack again.
            time.sleep(0.5)
            self.assertGreater(len(backend.requests_to("/agent/inbound")), 2)
            self.assertEqual(session.child_saw().count(b"\x1b[200~"), 1)
            self.assertEqual(len(backend.requests_to("/agent/inbound/ack")), 1)

    def test_the_ledger_is_written_before_the_ack(self):
        """The order, from the outside: the file that prevents a duplicate exists by the
        time the backend is told anything."""
        with StubBackend() as backend:
            backend.reply("/agent/inbound", 200, one_message())
            session = self.start()
            session.bind(backend_url=backend.url)
            wait_for(lambda: backend.requests_to("/agent/inbound/ack"))

            ledger = self.home / "wrappers" / f"{session.session_id}.delivered.json"
            self.assertTrue(ledger.exists())
            self.assertEqual(json.loads(ledger.read_text())["message_ids"], [MSG_ID])
            self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)

    def test_a_multi_line_message_arrives_as_a_single_paste(self):
        """On the path that matters: not `/inject` by hand, but a message that came off
        the backend queue the way a spoken instruction really does."""
        with StubBackend() as backend:
            backend.reply(
                "/agent/inbound",
                200,
                one_message("first paragraph, line one\nline two\n\nsecond paragraph"),
            )
            session = self.start()
            session.bind(backend_url=backend.url)

            # The Enter, not just the closing marker — two writes, `SUBMIT_DELAY` apart.
            saw = wait_for(
                lambda: session.child_saw().endswith(b"\x1b[201~\r") and session.child_saw()
            )
            self.assertEqual(saw.count(b"\x1b[200~"), 1)
            self.assertEqual(saw.count(b"\x1b[201~"), 1)
            body = saw.split(b"\x1b[200~", 1)[1].split(b"\x1b[201~", 1)[0]
            self.assertEqual(
                body, b"first paragraph, line one\rline two\r\rsecond paragraph"
            )
            self.assertTrue(saw.endswith(b"\x1b[201~\r"))

    def test_a_failed_ack_is_retried_without_a_second_injection(self):
        with StubBackend() as backend:
            backend.reply("/agent/inbound", 200, one_message())
            backend.reply("/agent/inbound/ack", 500, {"error": "nope"})
            session = self.start()
            session.bind(backend_url=backend.url)

            acks = wait_for(lambda: len(backend.requests_to("/agent/inbound/ack")) >= 2
                            and backend.requests_to("/agent/inbound/ack"))
            self.assertTrue(acks)
            self.assertEqual(acks[1]["body"]["message_ids"], [MSG_ID])
            # A failed ack is bookkeeping, not delivery: the instruction is already in the
            # terminal, so it must not go in again and the poll must not stop.
            self.assertEqual(session.child_saw().count(b"\x1b[200~"), 1)

    def test_no_duplicate_across_a_restart(self):
        """The actual requirement. The ledger is keyed by Claude Code session rather than
        by pid precisely so that it is *not* empty here, which is the one moment it matters.
        """
        with StubBackend() as backend:
            backend.reply("/agent/inbound", 200, one_message())
            first = self.start()
            first.bind(backend_url=backend.url)
            wait_for(lambda: backend.requests_to("/agent/inbound/ack"))
            self.assertIn(b"run the tests", first.child_saw())
            session_id = first.session_id
            first.stop()

            # The backend serves it again — an ack that was lost in flight, or a session
            # that was revived. A new process, a new pid, the same conversation.
            backend.reply("/agent/inbound", 200, one_message())
            second = self.start(extra_args=["--", "--session-id", session_id])
            self.assertIsNone(second.record["expected_session_id"])
            second.bind(claude_session_id=session_id, backend_url=backend.url)

            wait_for(lambda: len(backend.requests_to("/agent/inbound/ack")) >= 2)
            # Re-acked, so the backend stops serving it — but never re-typed.
            self.assertNotIn(b"\x1b[200~", second.child_saw())
            self.assertNotIn(b"run the tests", second.child_saw())
            self.assertEqual(
                backend.requests_to("/agent/inbound/ack")[-1]["body"]["message_ids"], [MSG_ID]
            )

    def test_a_backend_answering_with_errors_backs_off_instead_of_spinning(self):
        """The circuit breaker, ported whole. A sleeping Render service is an outage measured
        in hours, and a 5-second loop through it is a request every 5 seconds for hours."""
        with StubBackend() as backend:
            for _ in range(200):
                backend.reply("/agent/inbound", 500, {"error": "asleep"})
            session = self.start(poll={"AUDIOCHATTY_POLL_ACTIVE": "0.02",
                                      "AUDIOCHATTY_POLL_COOLDOWN": "1.5"})
            session.bind(backend_url=backend.url)

            wait_for(lambda: backend.requests_to("/agent/inbound"))
            time.sleep(1.0)
            # At a 0.02s active interval this would be ~50 requests. With the cooldown it is
            # one, and the assertion is deliberately loose about the exact number.
            attempts = len(backend.requests_to("/agent/inbound"))
            self.assertLessEqual(attempts, 3, f"{attempts} requests — the breaker is not holding")

    def test_a_backend_that_is_not_there_at_all_is_invisible_to_the_user(self):
        """Rule 2 of the whole project, applied here: a backend that is down must never be
        something the person in the terminal has to notice."""
        session = self.start(poll={"AUDIOCHATTY_POLL_ACTIVE": "0.05",
                                   "AUDIOCHATTY_POLL_COOLDOWN": "0.2"})
        session.bind(backend_url="http://127.0.0.1:1")
        time.sleep(0.6)

        self.assertIsNone(session.proc.poll(), "the wrapper died on an unreachable backend")
        screen = session.screen()
        self.assertNotIn(b"Traceback", screen)
        self.assertNotIn(b"127.0.0.1:1", screen)
        self.assertNotIn(b"Connection refused", screen)
        # And the terminal still works, which is the property that actually matters.
        session.type(b"still typing")
        self.assertTrue(wait_for(lambda: b"still typing" in session.child_saw()))

    def test_an_unpaired_wrapper_makes_no_network_calls(self):
        """The "costs nothing until connected" property, in the one place it survives.

        It used to hold for every unbound wrapper. It is spent knowingly now: a paired
        machine registers at launch (`TestConnectOnLaunch`). What survives — and what this
        pins — is that an *unpaired* machine still does nothing at all, so `audiochatty run`
        before the machine is paired is indistinguishable from plain `claude`.
        """
        (self.home / "credentials.json").unlink()
        with StubBackend() as backend:
            session = self.start(
                backend=backend.url, poll={"AUDIOCHATTY_POLL_ACTIVE": "0.02"}
            )
            self.assertFalse(session.call("/status", method="GET")[1]["polling"])
            time.sleep(0.4)
            self.assertEqual(backend.requests, [])

    def test_polling_starts_at_the_bind_with_no_handshake_in_between(self):
        with StubBackend() as backend:
            session = self.start(poll={"AUDIOCHATTY_POLL_ACTIVE": "0.02"})
            self.assertFalse(session.call("/status", method="GET")[1]["polling"])

            session.bind(backend_url=backend.url)

            self.assertTrue(wait_for(lambda: backend.requests_to("/agent/inbound")))
            self.assertTrue(session.call("/status", method="GET")[1]["polling"])

    def test_unbinding_stops_the_poll_loop(self):
        with StubBackend() as backend:
            session = self.start()
            session.bind(backend_url=backend.url)
            wait_for(lambda: backend.requests_to("/agent/inbound"))
            session.call("/unbind", {"token": TOKEN})

            # It exits at its next tick, not at the end of a 30-second sleep.
            self.assertTrue(wait_for(
                lambda: not session.call("/status", method="GET")[1]["polling"], timeout=3
            ))
            settled = len(backend.requests_to("/agent/inbound"))
            time.sleep(0.4)
            self.assertEqual(len(backend.requests_to("/agent/inbound")), settled)

    def test_verification_is_reported_and_retried_until_it_lands(self):
        """The verification loose end. The phone-side inbox reads `channel_verified_at`
        directly, so a reachable session the backend never heard about is one the user is
        wrongly told they cannot talk to."""
        with StubBackend() as backend:
            backend.reply("/agent/session/verified", 500, {"error": "nope"})
            session = self.start()
            session.bind(backend_url=backend.url)

            posts = wait_for(lambda: len(backend.requests_to("/agent/session/verified")) >= 2
                             and backend.requests_to("/agent/session/verified"))
            self.assertTrue(posts)
            self.assertEqual(posts[0]["body"], {"claude_session_id": session.session_id})

            # It stops once the backend confirms, rather than posting forever.
            time.sleep(0.5)
            self.assertEqual(len(backend.requests_to("/agent/session/verified")), 2)

    def test_a_caller_that_already_reported_verification_is_not_second_guessed(self):
        with StubBackend() as backend:
            session = self.start()
            session.bind(backend_url=backend.url, verified_reported=True)
            wait_for(lambda: len(backend.requests_to("/agent/inbound")) >= 3)
            self.assertEqual(backend.requests_to("/agent/session/verified"), [])

    def test_a_malformed_row_never_becomes_a_keystroke(self):
        with StubBackend() as backend:
            backend.reply("/agent/inbound", 200, {"messages": [
                {"text": "no id, so it cannot be deduped"},
                {"id": OTHER_MSG_ID, "text": "   "},
                "not even an object",
                {"id": MSG_ID, "text": "this one is real"},
            ]})
            session = self.start()
            session.bind(backend_url=backend.url)

            saw = wait_for(lambda: b"\x1b[201~" in session.child_saw() and session.child_saw())
            self.assertEqual(saw.count(b"\x1b[200~"), 1)
            self.assertIn(b"this one is real", saw)
            self.assertNotIn(b"cannot be deduped", saw)
            acks = wait_for(lambda: backend.requests_to("/agent/inbound/ack"))
            self.assertEqual(acks[0]["body"]["message_ids"], [MSG_ID])

    def test_the_quiet_period_still_applies_to_a_message_off_the_queue(self):
        """The quiet period and the ledger together, which is the interaction the port had to
        get right: the ack follows the *typing*, not the fetching, so a message held back
        because the user is mid-sentence is not yet marked delivered anywhere."""
        with StubBackend() as backend:
            backend.reply("/agent/inbound", 200, one_message("spoken while you were typing"))
            session = self.start(quiet_period=2.0)
            session.bind(backend_url=backend.url)

            deadline = time.monotonic() + 1.2
            while time.monotonic() < deadline:
                session.type(b"x")
                time.sleep(0.1)
            # Fetched, held, and — the part that matters — not acked and not in the ledger.
            self.assertNotIn(b"\x1b[200~", session.child_saw())
            self.assertEqual(backend.requests_to("/agent/inbound/ack"), [])
            ledger = self.home / "wrappers" / f"{session.session_id}.delivered.json"
            self.assertFalse(ledger.exists())

            # Stop typing: it lands, and only then is it acked.
            self.assertTrue(wait_for(lambda: b"\x1b[200~" in session.child_saw(), timeout=5))
            self.assertTrue(wait_for(lambda: backend.requests_to("/agent/inbound/ack")))


class TestConnectOnLaunch(WrapperTestCase):
    """`audiochatty run` is the whole of the setup: the wrapper registers its own session
    and binds itself, so nothing has to be typed into the session.

    Every test here starts a *real* wrapper against a stub backend and asserts on what the
    backend received, because the thing worth breaking is the wiring — a connect that runs on
    the wrong thread, before the control server, or not at all.
    """

    def marker(self, claude_session_id: str) -> dict:
        path = self.home / "sessions" / f"{claude_session_id}.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def test_a_launch_registers_binds_and_reports_reachable(self):
        with StubBackend() as backend:
            session = self.start(backend=backend.url)

            self.assertTrue(
                wait_for(lambda: backend.requests_to("/agent/session"), timeout=10),
                f"never registered: {session.stderr()}",
            )
            registration = backend.last_request("/agent/session")
            self.assertEqual(
                registration["body"]["claude_session_id"], session.session_id
            )
            self.assertEqual(registration["authorization"], f"Bearer {TOKEN}")

            # Bound in-process, so there is no `/bind` request to observe — the rendezvous
            # file is the evidence, and `verified` proves it went through the real bind rules
            # rather than being written by hand.
            self.assertTrue(wait_for(lambda: session.reread().get("bound"), timeout=10))
            self.assertEqual(session.record["claude_session_id"], session.session_id)
            self.assertTrue(session.record["verified"])
            self.assertTrue(
                wait_for(lambda: backend.requests_to("/agent/session/verified"), timeout=10)
            )

    def test_the_marker_is_written_so_the_stop_hook_can_fire(self):
        with StubBackend() as backend:
            session = self.start(backend=backend.url)
            self.assertTrue(
                wait_for(lambda: self.marker(session.session_id), timeout=10)
            )
            marker = self.marker(session.session_id)

            self.assertEqual(marker["name"], "billing-refactor")
            self.assertEqual(marker["wrapper_pid"], session.proc.pid)
            # No connect *turn* to swallow on this path — the next turn is real work.
            self.assertNotIn("skip_next_turn", marker)

    def test_polling_starts_without_anyone_binding_by_hand(self):
        with StubBackend() as backend:
            session = self.start(
                backend=backend.url, poll={"AUDIOCHATTY_POLL_ACTIVE": "0.05"}
            )

            self.assertTrue(
                wait_for(lambda: backend.requests_to("/agent/inbound"), timeout=10)
            )
            self.assertTrue(session.call("/status", method="GET")[1]["polling"])

    def test_the_name_can_be_chosen_at_launch(self):
        """`--name` replaces the argument `/audiochatty-connect [name]` used to take. Without
        it, moving the connect to launch would silently remove the ability to name a session
        at all."""
        with StubBackend() as backend:
            self.start(backend=backend.url, extra_args=["--name", "auth-bug"])

            self.assertTrue(wait_for(lambda: backend.requests_to("/agent/session"), timeout=10))
            self.assertEqual(
                backend.last_request("/agent/session")["body"]["name"], "auth-bug"
            )

    def test_a_resumed_session_is_left_for_the_hook(self):
        """`--resume` means the session id is not ours to mint, so `expected_session_id` is
        null and the wrapper cannot know what to register. It must not guess — that case
        belongs to the SessionStart hook, which runs inside the session."""
        with StubBackend() as backend:
            session = self.start(backend=backend.url, extra_args=["--", "--resume"])

            self.assertIsNone(session.record["expected_session_id"])
            time.sleep(0.6)
            self.assertEqual(backend.requests_to("/agent/session"), [])
            self.assertFalse(session.reread().get("bound"))
            # And nothing is recorded as an error: this is a handoff, not a failure.
            self.assertIsNone(session.record.get("connect_error"))

    def test_a_dead_backend_leaves_a_usable_terminal_and_a_readable_reason(self):
        """The connect runs on a thread precisely so this is true. The default fixture
        backend is `127.0.0.1:1`, which refuses instantly."""
        session = self.start()

        # The terminal works: the child is up and keystrokes still reach it.
        session.type(b"hello")
        self.assertTrue(wait_for(lambda: b"hello" in session.child_saw(), timeout=5))

        self.assertTrue(
            wait_for(lambda: session.reread().get("connect_error"), timeout=10),
            "a silent failure with nothing written down is the one outcome forbidden here",
        )
        self.assertEqual(session.record["connect_error"], "unreachable")
        self.assertTrue(session.record["connect_error_at"])
        self.assertFalse(session.record["bound"])

    def test_a_slow_backend_does_not_delay_the_terminal(self):
        """The whole reason step 3a is on a thread. A sleeping Render service takes its full
        timeout, and the user is typing."""
        with StubBackend() as backend:
            backend.reply("/agent/session", 200, {"session_id": "s-a", "name": "x"}, delay=2.0)
            started = time.monotonic()
            session = self.start(backend=backend.url)
            elapsed = time.monotonic() - started

            session.type(b"typed while it registers")
            self.assertTrue(
                wait_for(lambda: b"typed while it registers" in session.child_saw(), timeout=5)
            )
            self.assertLess(elapsed, 2.0, "the launch waited for the registration")

    def test_an_unpaired_launch_records_no_error(self):
        """Not paired is not a failure — it is a machine that has not opted in, and
        `/audiochatty-status` already says so from the credentials file. Recording an error
        would put a scary line in front of someone who simply has not logged in yet."""
        (self.home / "credentials.json").unlink()
        with StubBackend() as backend:
            session = self.start(backend=backend.url)

            time.sleep(0.6)
            self.assertEqual(backend.requests, [])
            self.assertIsNone(session.reread().get("connect_error"))

    def test_the_launch_says_nothing_on_the_users_screen(self):
        """The silence is a hard constraint: stdout is the child's screen and a stray line
        there is a corrupted TUI."""
        with StubBackend() as backend:
            session = self.start(backend=backend.url)
            self.assertTrue(wait_for(lambda: session.reread().get("bound"), timeout=10))

            self.assertNotIn(b"audiochatty", session.screen())
            self.assertNotIn("audiochatty:", session.stderr())

    def test_a_failed_connect_can_be_retried_by_hand_without_restarting(self):
        """What `/audiochatty-connect` is *for* now. The wrapper is running and unbound; the
        slash command finds it through the same rendezvous file and binds it over loopback."""
        session = self.start()  # unreachable backend, so the launch connect fails
        self.assertTrue(wait_for(lambda: session.reread().get("connect_error"), timeout=10))

        with StubBackend() as backend:
            env = dict(os.environ)
            env.update(
                AUDIOCHATTY_HOME=str(self.home),
                AUDIOCHATTY_BACKEND_URL=backend.url,
                AUDIOCHATTY_WRAPPER_PORT=str(session.port),
                AUDIOCHATTY_WRAPPER_PID=str(session.proc.pid),
            )
            result = subprocess.run(
                [sys.executable, str(PLUGIN_ROOT / "scripts" / "audiochat.py"),
                 "connect", "retried", "--session-id", session.session_id],
                env=env, capture_output=True, text=True, timeout=60,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(wait_for(lambda: session.reread().get("bound"), timeout=10))
            self.assertEqual(session.record["claude_session_id"], session.session_id)


class TestVerbose(WrapperTestCase):
    """`--verbose` used to be just the one startup line; it is now a superset of
    `AUDIOCHATTY_DEBUG=1` — same shared `store.debug()`, reached without setting the env var
    (which would otherwise leak into the wrapped `claude`'s own environment)."""

    def test_verbose_narrates_via_the_shared_debug_path(self):
        session = self.start(extra_args=["--verbose"])
        self.assertTrue(wait_for(lambda: b"argv=" in session.child_saw()))
        session.stop()

        err = session.stderr()
        self.assertIn("audiochatty: port", err)  # the original, dedicated startup line
        self.assertIn("[audiochatty wrapper] spawned", err)  # pty_proxy's debug() line

    def test_verbose_narrates_the_injector(self):
        """`inject.py` had no diagnostic output under either mechanism before this — prove
        `--verbose` now reaches its enqueue/type/submit transitions too."""
        session = self.start(extra_args=["--verbose"])
        session.bind()
        session.call("/inject", {"token": TOKEN, "text": "narrate me"})
        wait_for(lambda: session.child_saw().endswith(b"\x1b[201~\r") and session.child_saw())
        session.stop()

        err = session.stderr()
        self.assertIn("enqueued instruction", err)
        self.assertIn("typed paste", err)
        self.assertIn("submitted paste", err)

    def test_without_verbose_the_injector_stays_silent(self):
        """The counterpart to the above: same instruction, no flag, nothing on stderr. Checked
        after `stop()` (unlike the screen-silence test) so it reflects what was actually
        written, not `stderr()`'s "" default while the process is still running."""
        session = self.start()
        session.bind()
        session.call("/inject", {"token": TOKEN, "text": "stay quiet"})
        wait_for(lambda: session.child_saw().endswith(b"\x1b[201~\r") and session.child_saw())
        session.stop()

        err = session.stderr()
        self.assertNotIn("audiochatty:", err)
        self.assertNotIn("[audiochatty wrapper]", err)


class TestTerminalBehaviour(WrapperTestCase):
    def test_keystrokes_arrive_without_waiting_for_enter(self):
        """Raw mode, observed rather than asserted about: an unnewlined keystroke reaching the
        child means the outer line discipline is out of the way."""
        session = self.start()
        session.type(b"abc")
        self.assertTrue(wait_for(lambda: b"abc" in session.child_saw()))

    def test_child_output_reaches_the_real_terminal(self):
        session = self.start()
        self.assertTrue(wait_for(lambda: b"fake claude ready" in session.screen()))

    def test_a_resize_reaches_the_child(self):
        """A wrapper you can tell you are inside is a wrapper nobody will use, and the first
        way you notice is a TUI drawn at the wrong width."""
        import fcntl
        import struct
        import termios

        session = self.start()
        wait_for(lambda: b"argv=" in session.child_saw())
        fcntl.ioctl(session.stdin_master, termios.TIOCSWINSZ, struct.pack("HHHH", 33, 111, 0, 0))
        os.kill(session.proc.pid, signal.SIGWINCH)

        # Ask the child's pty rather than the child: the size is a property of the tty, and
        # setting it on the master is also what makes the kernel deliver SIGWINCH inside.
        got = wait_for(lambda: self._pty_size(session) == (33, 111), timeout=3)
        self.assertTrue(got, f"child pty is {self._pty_size(session)}, wanted (33, 111)")

    @staticmethod
    def _pty_size(session) -> tuple[int, int]:
        import fcntl
        import struct
        import termios

        try:
            tty_name = subprocess.run(
                ["ps", "-o", "tty=", "-p", str(session.record["child_pid"])],
                capture_output=True,
                text=True,
            ).stdout.strip()
            fd = os.open(f"/dev/{tty_name}", os.O_RDONLY | os.O_NONBLOCK)
        except (OSError, KeyError):
            return (0, 0)
        try:
            packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
            rows, cols = struct.unpack("HHHH", packed)[:2]
            return (rows, cols)
        except OSError:
            return (0, 0)
        finally:
            os.close(fd)

    def test_terminate_is_forwarded_to_the_child(self):
        """`kill` the wrapper and the session dies with it, the way killing `claude` would."""
        session = self.start()
        child_pid = session.record["child_pid"]
        session.proc.send_signal(signal.SIGTERM)
        session.proc.wait(timeout=10)
        self.assertTrue(wait_for(lambda: not self._alive(child_pid), timeout=5))

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


class TestSanitizer(unittest.TestCase):
    """The one part worth testing without three processes, because it is pure text."""

    def test_control_characters_are_stripped(self):
        self.assertEqual(inject.sanitize("a\x1b[201~b"), "a[201~b")
        self.assertEqual(inject.sanitize("a\x00\x07b"), "ab")
        self.assertEqual(inject.sanitize("keep\tthe\ttabs"), "keep\tthe\ttabs")

    def test_line_endings_are_normalised(self):
        self.assertEqual(inject.sanitize("a\r\nb\rc\nd"), "a\nb\nc\nd")

    def test_length_is_capped(self):
        text = "x" * (inject.MAX_CONTENT_CHARS + 500)
        self.assertEqual(len(inject.sanitize(text)), inject.MAX_CONTENT_CHARS)

    def test_empty_text_encodes_to_nothing(self):
        self.assertEqual(inject.encode_paste("   \n  "), b"")
        self.assertEqual(inject.encode_paste("\x1b\x00"), b"")

    def test_encoding_is_a_paste_and_carries_no_enter(self):
        """The Enter is not part of the encoding — `Injector.flush` writes it separately, a
        beat later, because the TUI swallows one that arrives in the same read."""
        self.assertEqual(inject.encode_paste("a\nb"), b"\x1b[200~a\rb\x1b[201~")


class TestSubmitIsASeparateWrite(unittest.TestCase):
    """The regression that made an instruction land in the prompt and never run.

    Sent as one write, `\\x1b[201~\\r` is swallowed by Claude Code's TUI often enough to be a
    coin flip — measured against 2.1.221, 2 of 6 single-line instructions never submitted.
    A separate `os.write` is *not* sufficient (the bytes coalesce into one pty read); only a
    real gap is. So what this pins is the gap, at the one place that can guarantee it.
    """

    def setUp(self):
        import tty

        self.master, self.slave = os.openpty()
        # Raw, like Claude Code's TUI and like `FakeClaude`. Left canonical, the slave would
        # not surface a read until a newline arrived — so a paste with no trailing Enter,
        # which is exactly what this class asserts on, would look like nothing at all.
        tty.setraw(self.slave)
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)

    def read_child(self) -> bytes:
        """Everything the child can see so far, without blocking."""
        import select as _select

        seen = b""
        while _select.select([self.slave], [], [], 0.05)[0]:
            seen += os.read(self.slave, 65536)
        return seen

    def test_the_enter_lands_only_after_the_delay(self):
        injector = inject.Injector(quiet_period=0.0, submit_delay=0.2)
        injector.enqueue("run the tests", message_id="m1")

        injector.flush(self.master)
        first = self.read_child()
        self.assertTrue(first.endswith(b"\x1b[201~"), f"paste not written alone: {first!r}")
        self.assertNotIn(b"\r", first.split(b"\x1b[200~", 1)[1])

        # Still inside the gap: flushing again must not submit early.
        self.assertEqual(injector.flush(self.master), 0)
        self.assertEqual(self.read_child(), b"")
        self.assertEqual(injector.pending(), 1, "an unsubmitted paste is not delivered")
        self.assertEqual(injector.drain_delivered(), [])

        time.sleep(0.25)
        self.assertEqual(injector.flush(self.master), 1)
        self.assertEqual(self.read_child(), b"\r")
        self.assertEqual(injector.drain_delivered(), ["m1"])
        self.assertEqual(injector.pending(), 0)

    def test_the_loop_is_told_to_come_back_for_the_enter(self):
        """The gap only works if `select` does not then sleep a full second through it."""
        injector = inject.Injector(quiet_period=0.0, submit_delay=0.2)
        injector.enqueue("run the tests", message_id="m1")
        injector.flush(self.master)
        self.assertLessEqual(injector.next_timeout(1.0), 0.2)

    def test_two_instructions_do_not_merge_into_one_prompt(self):
        """The second paste must not go in before the first one's Enter, or two spoken
        instructions arrive as a single run-on prompt."""
        injector = inject.Injector(quiet_period=0.0, submit_delay=0.1)
        injector.enqueue("first instruction", message_id="m1")
        injector.enqueue("second instruction", message_id="m2")

        injector.flush(self.master)
        seen = self.read_child()
        self.assertEqual(seen.count(b"\x1b[200~"), 1, "both pastes went in at once")

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and injector.pending():
            injector.flush(self.master)
            seen += self.read_child()
            time.sleep(0.02)

        # The shape that matters: paste, Enter, paste, Enter — never paste, paste.
        skeleton = re.sub(rb"[^\r]*?\x1b\[201~", b"<paste>", seen.replace(b"\x1b[200~", b""))
        self.assertEqual(skeleton, b"<paste>\r<paste>\r")
        self.assertEqual(sorted(injector.drain_delivered()), ["m1", "m2"])


class TestParseInbound(unittest.TestCase):
    """`poller.parse_inbound`, the other part worth testing on its own, because it is the
    boundary between "the backend said something" and "we type into a terminal"."""

    def test_both_response_shapes(self):
        row = {"id": "a", "text": "hi", "sender_name": "Mike", "created_at": "now"}
        self.assertEqual(poller.parse_inbound({"messages": [row]}), [row])
        self.assertEqual(poller.parse_inbound([row]), [row])

    def test_anything_unusable_is_dropped(self):
        for payload in ({}, None, "nope", {"messages": "nope"}, [None, 3, "x"]):
            self.assertEqual(poller.parse_inbound(payload), [])
        self.assertEqual(poller.parse_inbound({"messages": [{"text": "no id"}]}), [])
        self.assertEqual(poller.parse_inbound({"messages": [{"id": "x", "text": " \n "}]}), [])

    def test_fields_are_capped(self):
        parsed = poller.parse_inbound({"messages": [{
            "id": "a" * 400,
            "text": "t" * (inject.MAX_CONTENT_CHARS + 100),
            "sender_name": "n" * 400,
            "created_at": "c" * 400,
        }]})
        self.assertEqual(len(parsed[0]["id"]), poller.MAX_ID_CHARS)
        self.assertEqual(len(parsed[0]["text"]), inject.MAX_CONTENT_CHARS)
        self.assertEqual(len(parsed[0]["sender_name"]), poller.MAX_SENDER_CHARS)
        self.assertEqual(len(parsed[0]["created_at"]), poller.MAX_CREATED_AT_CHARS)

    def test_missing_optional_fields_become_empty_strings(self):
        parsed = poller.parse_inbound({"messages": [{"id": "a", "text": "hi"}]})
        self.assertEqual(parsed[0]["sender_name"], "")
        self.assertEqual(parsed[0]["created_at"], "")


class TestDeliveredLedger(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._previous = os.environ.get("AUDIOCHATTY_HOME")
        os.environ["AUDIOCHATTY_HOME"] = self._tmp.name

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop("AUDIOCHATTY_HOME", None)
        else:
            os.environ["AUDIOCHATTY_HOME"] = self._previous
        self._tmp.cleanup()

    def test_it_survives_being_reopened(self):
        ledger = poller.DeliveredLedger("session-one")
        ledger.add("a")
        ledger.add("a")  # twice is once
        ledger.persist()

        reopened = poller.DeliveredLedger("session-one")
        self.assertIn("a", reopened)
        self.assertNotIn("b", reopened)
        self.assertEqual(reopened.ids, ["a"])
        # A different session is a different ledger.
        self.assertNotIn("a", poller.DeliveredLedger("session-two"))

    def test_it_is_capped_at_the_most_recent_ids(self):
        ledger = poller.DeliveredLedger("session-one")
        for index in range(poller.MAX_DELIVERED_IDS + 50):
            ledger.add(str(index))
        ledger.persist()

        reopened = poller.DeliveredLedger("session-one")
        self.assertEqual(len(reopened.ids), poller.MAX_DELIVERED_IDS)
        self.assertIn(str(poller.MAX_DELIVERED_IDS + 49), reopened)
        self.assertNotIn("0", reopened)

    def test_a_session_id_cannot_escape_the_directory(self):
        ledger = poller.DeliveredLedger("../../etc/passwd")
        self.assertEqual(ledger.path.parent.name, "wrappers")
        self.assertNotIn("..", ledger.path.name)

    def test_a_corrupt_file_reads_as_empty(self):
        ledger = poller.DeliveredLedger("session-one")
        ledger.path.write_text("{not json")
        self.assertEqual(poller.DeliveredLedger("session-one").ids, [])


if __name__ == "__main__":
    unittest.main()
