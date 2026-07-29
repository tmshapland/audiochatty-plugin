"""The pass-through terminal: a pty, `claude` inside it, and a loop in the middle.

`wrapper_return_path_plan.md` Phase 1 · W1.

The whole design goal is that this file is *invisible*. A wrapped session has to feel
identical to an unwrapped one — Phase 0's exit criterion is a person using it for an hour
and not being able to tell — which means every terminal behaviour has to survive the round
trip: raw keystrokes, Ctrl-C, window resizes, alternate-screen redraws, the child's own
idea of what mode the terminal is in.

    real terminal ─▶ stdin ─▶ [ this loop ] ─▶ pty master ─▶ pty slave ─▶ claude
    real terminal ◀─ stdout ◀─[ this loop ]◀── pty master ◀───────────────┘

Four things that are easy to get wrong and are each one line of consequence here:

**Raw mode on the real terminal.** Without it the outer tty's line discipline eats Ctrl-C
and buffers input until Enter, and the child — which draws its own prompt — never sees a
keystroke until the line is finished. With it, every byte goes straight through and the
child's own termios settings are the only ones that apply. It has to be restored on *every*
exit path, including a crash, or the user is left in a terminal with no echo.

**A controlling terminal for the child.** Inheriting the slave fd as stdin/stdout/stderr is
not enough to make it the child's controlling tty; that takes a `TIOCSCTTY` after a
`setsid`. Without it there is no job control in the session — anything `claude` spawns that
expects to be a foreground process group misbehaves.

**Window size, twice.** Once at spawn, so the child never draws at 80x24 and then jumps, and
again on every SIGWINCH. Setting the size on the master is what makes the kernel deliver
SIGWINCH to the child, so there is nothing to forward by hand.

**Signals arrive as bytes, not signals.** In raw mode Ctrl-C is `\\x03` on stdin and the
child's own line discipline turns it into a SIGINT. A signal sent to the *wrapper* (a `kill`
from elsewhere) is different, and is forwarded to the child's process group so
`audiochatty run` dies the way `claude` would.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import tty

from wrapper.store import debug

# Big enough that a full-screen redraw is one read, small enough to stay off the stack.
READ_SIZE = 65536

# The loop's idle sleep. Nothing depends on it being short — resize and injection both wake
# it explicitly — so this is only a backstop for noticing the child has exited.
IDLE_TIMEOUT = 1.0

# Sent to the wrapper, forwarded to the child's process group.
FORWARDED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)


class SpawnFailed(Exception):
    """`claude` could not be started. The message is shown to the user verbatim."""


def _get_winsize(fd: int) -> bytes | None:
    try:
        return fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    except OSError:
        return None


def _set_winsize(fd: int, packed: bytes) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)
    except OSError:
        pass


def _fd_is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def _child_setup() -> None:  # pragma: no cover - runs between fork and exec
    """Between the fork and the exec. Give the child its own session and make the pty its
    controlling terminal.

    This runs as `preexec_fn`, which is only safe while the parent is single-threaded — see
    the ordering note in `__main__.py`. Nothing here allocates or takes a lock.
    """
    os.setsid()
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        pass


class PtyProxy:
    def __init__(self, argv: list[str], *, env: dict, injector):
        self.argv = argv
        self.env = env
        self.injector = injector
        self.master_fd: int | None = None
        self.proc: subprocess.Popen | None = None
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_w, False)
        self._resize_wanted = False
        self._forward: int | None = None
        self._saved_attrs = None
        injector.set_wake(self.wake)

    @property
    def child_pid(self) -> int:
        assert self.proc is not None
        return self.proc.pid

    def wake(self) -> None:
        """Interrupt `select`. Safe from a signal handler and from another thread."""
        try:
            os.write(self._wake_w, b"\x01")
        except OSError:
            pass

    # -- startup --

    def spawn(self) -> None:
        """Open the pty and start the child. **Call this before starting any thread.**"""
        self.master_fd, slave_fd = pty.openpty()
        stdin_fd = sys.stdin.fileno()

        # Start the child from the same terminal settings and size the user's terminal
        # already has, so its first frame is drawn at the right dimensions.
        if os.isatty(stdin_fd):
            try:
                termios.tcsetattr(slave_fd, termios.TCSANOW, termios.tcgetattr(stdin_fd))
            except (OSError, termios.error):
                pass
            packed = _get_winsize(stdin_fd)
            if packed:
                _set_winsize(self.master_fd, packed)

        try:
            self.proc = subprocess.Popen(
                self.argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=self.env,
                preexec_fn=_child_setup,
                close_fds=True,
            )
        except (OSError, ValueError) as err:
            os.close(slave_fd)
            os.close(self.master_fd)
            self.master_fd = None
            raise SpawnFailed(f"could not start {self.argv[0]!r}: {err}") from err
        finally:
            # The child holds its own copy. Keeping ours open here means the master never
            # sees EOF when the child exits, and the loop hangs forever.
            if self.proc is not None:
                os.close(slave_fd)

        # Raw mode here rather than at the top of `run()`, because everything between the
        # two — writing the rendezvous file, starting the control thread — is time in which
        # the user's keystrokes would be eaten by the outer line discipline and, worse,
        # discarded by the mode change itself. It is a few milliseconds, but it is the few
        # milliseconds right after someone typed `audiochatty run`, and it is also the window
        # a test lands in, which is how this was found.
        self._enter_raw(sys.stdin.fileno())
        debug(f"spawned {self.argv[0]} as pid {self.proc.pid}")

    # -- the loop --

    def run(self) -> int:
        """Proxy until the child exits. Returns the child's exit code."""
        assert self.master_fd is not None and self.proc is not None
        stdin_fd = sys.stdin.fileno()
        stdout_fd = sys.stdout.fileno()

        self._install_handlers()
        self._enter_raw(stdin_fd)  # a no-op if `spawn` already did it
        try:
            watching = [self.master_fd, self._wake_r]
            if _fd_is_open(stdin_fd):
                watching.append(stdin_fd)
            while True:
                timeout = self.injector.next_timeout(IDLE_TIMEOUT)
                try:
                    readable, _, _ = select.select(watching, [], [], timeout)
                except InterruptedError:
                    # PEP 475 retries most of these for us; a handler that ran and set a
                    # flag lands here, and the next pass acts on it.
                    continue
                except OSError as err:
                    if err.errno != errno.EBADF:
                        raise
                    # Something closed under us. Drop whatever is no longer a fd rather
                    # than spinning on it forever.
                    watching = [fd for fd in watching if _fd_is_open(fd)]
                    if self.master_fd not in watching:
                        break
                    continue

                if self._wake_r in readable:
                    self._drain(self._wake_r)

                if self._forward is not None:
                    self._forward_signal()

                if self._resize_wanted:
                    self._resize_wanted = False
                    packed = _get_winsize(stdin_fd)
                    if packed:
                        _set_winsize(self.master_fd, packed)

                if stdin_fd in readable:
                    data = self._read(stdin_fd)
                    if data is None or data == b"":
                        # Stdin closed. The child stays up — it may still be working — but
                        # there is nothing left to forward, so stop watching a fd that is
                        # permanently readable.
                        watching = [fd for fd in watching if fd != stdin_fd]
                    else:
                        # W6's entire input: the one place that knows the human is typing.
                        self.injector.note_keystroke()
                        self._write(self.master_fd, data)

                if self.master_fd in readable:
                    data = self._read(self.master_fd)
                    if not data:
                        break  # the child closed the pty: it has exited or is about to
                    self._write(stdout_fd, data)

                self.injector.flush(self.master_fd)

                if self.proc.poll() is not None and self.master_fd not in readable:
                    break
        finally:
            self._exit_raw(stdin_fd)
            self._restore_handlers()

        return self._reap()

    # -- terminal state --

    def restore_terminal(self) -> None:
        """For the caller's failure path: `spawn` leaves the terminal raw, and anything that
        goes wrong between there and `run` has to put it back."""
        self._exit_raw(sys.stdin.fileno())

    def _enter_raw(self, fd: int) -> None:
        if self._saved_attrs is not None or not os.isatty(fd):
            return  # already raw, or a pipe — nothing to put into raw mode
        try:
            self._saved_attrs = termios.tcgetattr(fd)
            # `TCSANOW`, not `tty.setraw`'s default `TCSAFLUSH`: flushing discards input that
            # has already been typed. Someone who typed ahead while `claude` was starting
            # should not silently lose it.
            tty.setraw(fd, termios.TCSANOW)
        except (OSError, termios.error) as err:
            debug(f"could not raw-mode stdin: {err}")
            self._saved_attrs = None

    def _exit_raw(self, fd: int) -> None:
        """Restore, whatever happened. A wrapper that crashes and leaves the terminal in raw
        mode leaves the user with no echo and no Ctrl-C — worse than any crash.

        `TCSANOW`, not the conventional `TCSADRAIN`: draining waits for the terminal's output
        queue to empty, and for a pty that means waiting for whatever is on the other end to
        *read* it. A terminal emulator always does, so the two are identical in real use —
        but anything else on the far end (a test harness, a script, an editor that has
        stopped consuming) turns the restore into a hang, and hanging on the way out is not a
        trade worth making for tidier output ordering.
        """
        if self._saved_attrs is None:
            return
        try:
            termios.tcsetattr(fd, termios.TCSANOW, self._saved_attrs)
        except (OSError, termios.error):
            pass
        self._saved_attrs = None

    # -- signals --

    def _install_handlers(self) -> None:
        self._previous = {}
        for sig in FORWARDED_SIGNALS:
            try:
                self._previous[sig] = signal.signal(sig, self._on_signal)
            except (OSError, ValueError):
                pass
        try:
            self._previous[signal.SIGWINCH] = signal.signal(signal.SIGWINCH, self._on_winch)
        except (OSError, ValueError):
            pass

    def _restore_handlers(self) -> None:
        for sig, handler in getattr(self, "_previous", {}).items():
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):
                pass

    def _on_signal(self, signum, _frame) -> None:
        self._forward = signum
        self.wake()

    def _on_winch(self, _signum, _frame) -> None:
        self._resize_wanted = True
        self.wake()

    def _forward_signal(self) -> None:
        signum, self._forward = self._forward, None
        if self.proc is None or signum is None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signum)
        except OSError:
            pass

    # -- fd plumbing --

    @staticmethod
    def _read(fd: int) -> bytes | None:
        try:
            return os.read(fd, READ_SIZE)
        except OSError as err:
            # A pty master whose slave has closed reads EIO on Linux and b"" on macOS.
            # Both mean the same thing to the caller.
            if err.errno in (errno.EIO, errno.EBADF):
                return b""
            if err.errno == errno.EAGAIN:
                return None
            raise

    @staticmethod
    def _write(fd: int, data: bytes) -> None:
        sent = 0
        while sent < len(data):
            try:
                sent += os.write(fd, data[sent:])
            except OSError as err:
                if err.errno == errno.EAGAIN:
                    select.select([], [fd], [], 0.05)
                    continue
                return  # the far end is gone; the loop notices on its next pass

    @staticmethod
    def _drain(fd: int) -> None:
        try:
            os.read(fd, READ_SIZE)
        except OSError:
            pass

    def _reap(self) -> int:
        """The child's exit code becomes ours, so `audiochatty run` is a drop-in for `claude`
        in a script. A child killed by a signal reports 128+signum, the way a shell does."""
        if self.proc is None:
            return 1
        try:
            code = self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = self.proc.wait(timeout=5)
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        return 128 - code if code < 0 else code
