"""`audiochatty run`, driven the way a person drives it.

`wrapper_return_path_plan.md` Phase 1. The successor to `tests/test_channel.py`.

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
bracketed paste the way a terminal emulator's would. That is Phase 0's W5 spike and needs a
human at a keyboard; see `wrapper/README.md`.
"""

from __future__ import annotations

import json
import os
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

from wrapper import inject  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = PLUGIN_ROOT / "wrapper" / "__main__.py"
LAUNCHER = PLUGIN_ROOT / "wrapper" / "audiochatty"

TOKEN = "device-token-for-tests"
# Short enough to keep the suite quick, long enough that "held back" and "let through" are
# not the same measurement.
QUIET_PERIOD = 0.6

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
    typing on the keyboard, which is the only honest way to exercise W6.
    """

    def __init__(self, home: Path, *, exit_code: int = 0, quiet_period: float = QUIET_PERIOD,
                 extra_args: list[str] | None = None, launcher: bool = False):
        import pty

        self.home = home
        self.log = home / "child.log"
        self.claude = home / "fakeclaude"
        self.claude.write_text(FAKE_CLAUDE.format(python=sys.executable))
        self.claude.chmod(0o755)

        env = dict(os.environ)
        env.update(
            AUDIOCHATTY_HOME=str(home),
            FAKE_CLAUDE_LOG=str(self.log),
            FAKE_CLAUDE_EXIT=str(exit_code),
        )
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
            "agent_session_id": "agent-session-1",
            "claude_session_id": self.session_id,
            "backend_url": "http://127.0.0.1:1/",
            "token": TOKEN,
            "session_name": "laptop",
        }
        body.update(overrides)
        return self.call("/bind", body)

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
        while True:
            try:
                data = os.read(self.stdin_master, 65536)
            except OSError:
                return
            if not data:
                return
            self._screen.extend(data)

    def stderr(self) -> str:
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
        try:
            os.close(self.stdin_master)
        except OSError:
            pass
        if self.proc.stderr:
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

        # 0600, and the token is not in here.
        self.assertEqual(session.rendezvous.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(TOKEN, session.rendezvous.read_text())

    def test_wrapper_env_reaches_the_child(self):
        """W3 in one assertion: the child's environment carries the port and pid, which is
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
        """W8: there is no handshake any more. The wrapper owns the pty, so binding *is* the
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
        """W3's safety check. Without it, a plain `claude` run inside a wrapped session
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
        # The generation moved, which is what stops a Phase 2 poll loop mid-sleep.
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
        """W5. The failure this prevents: Claude Code submitting at the first newline and
        treating the rest of a spoken instruction as a second prompt."""
        session = self.start()
        session.bind()
        text = "first line\nsecond line\n\nlast paragraph"
        self.assertEqual(session.call("/inject", {"token": TOKEN, "text": text})[0], 202)

        saw = wait_for(lambda: b"\x1b[201~" in session.child_saw() and session.child_saw())
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
        """W6, the whole point of the wrapper over `tmux send-keys`: it can see the keyboard,
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

    def test_encoding_is_a_paste_plus_one_enter(self):
        self.assertEqual(inject.encode_paste("a\nb"), b"\x1b[200~a\rb\x1b[201~\r")


if __name__ == "__main__":
    unittest.main()
