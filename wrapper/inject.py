"""Typing into the session on your behalf.

`wrapper_return_path_plan.md` Phase 1 · W5, W6.

Two rules, and each one exists because of a specific way this goes wrong.

**W5 — a spoken instruction is one paste, not a stream of keystrokes.** A single instruction
from audiochatty routinely carries several turns' worth of speech joined by blank lines.
Written raw into the pty, Claude Code's prompt would submit at the first newline and treat
the rest as a second instruction. So the text goes in wrapped in the terminal's
bracketed-paste markers — "everything between these is one paste, don't submit yet" — with
a single Enter after the closing marker. Inside a real paste an emulator sends CR for a line
break (xterm translates LF→CR on paste, and iTerm2 and VS Code's terminal follow it), so
that is what goes on the wire here too.

**W6 — never type over the user.** The wrapper sees every key on its way to the child, so
it knows to the millisecond when the human last typed. An instruction that arrives
mid-sentence waits for a pause. This is the one thing tmux could never do (`send-keys`
cannot see your keystrokes) and it is the defence against the worst failure mode in this
design: a spoken instruction spliced into a half-finished line the user was writing.

**The Enter is a separate write, `SUBMIT_DELAY` after the paste, and that is load-bearing.**
Sent in the *same* write as the closing marker, Claude Code's TUI usually — but not always —
swallows it: the instruction lands in the prompt box, correct and complete, and just sits
there until a human presses Return. Measured against the real TUI (2.1.221) it is a race,
not a deterministic bug, which is the worst shape this could have taken: 4 of 6 single-line
instructions submitted and 2 hung, while multi-line ones happened to submit every time. That
is why it survived Phase 0's spike — a hand-driven test with a few lines of dictation passes
— and why `tests/test_wrapper.py`'s fake child could never have caught it. The trigger is the
paste and the CR arriving in one `read()`, so a separate `os.write` alone does **not** fix it
(measured: still hangs); the bytes coalesce in the pty. Only a real gap does. 30ms was enough
in every trial, 150ms is the shipped value, and the delay is why `flush` below is a state
machine rather than three lines — the proxy thread cannot sleep.

The sanitiser is not cosmetic. `\\x1b[201~` appearing *inside* the text would end the paste
early and hand the remainder to the terminal as live keystrokes — arbitrary escape sequences
typed into your session by whatever produced that text. Stripping every C0 control except
tab and newline removes that whole class, and there is nothing a spoken instruction can
legitimately need in it.
"""

from __future__ import annotations

import os
import threading
import time

from wrapper.store import debug

PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
SUBMIT = b"\r"

# A single instruction, capped. Spoken text is short; this is a guard against a backend that
# has gone wrong, not a policy about how much someone can say. Same number, same reasoning,
# as `MAX_CONTENT_CHARS` in `channel/server.ts`.
MAX_CONTENT_CHARS = 20_000

# How long the user has to have stopped typing. Long enough that a pause for thought
# mid-sentence does not count as "finished", short enough that an instruction spoken into a
# session nobody is sitting at lands immediately.
DEFAULT_QUIET_PERIOD = 1.5

# The gap between the closing paste marker and the Enter — see the module docstring. Five
# times the smallest gap that worked in testing, and still far below the threshold at which
# a person would notice the prompt pause before it submits. It is a deliberately generous
# multiple because the failure it prevents is silent: the instruction looks delivered.
#
# It does cost a sliver of W6. For these 150ms the paste is in the prompt un-submitted, so a
# keystroke landing in that window joins the text that is about to be sent. The window only
# opens after the user has already been quiet for `quiet_period` (a hundred times longer), so
# this trades a rare, small mess for a common, silent failure — but it is the reason not to
# raise this number casually.
SUBMIT_DELAY = 0.15

# Only these two survive the control-character filter: a paste can legitimately contain a
# newline (that is the entire reason W5 exists) and a tab.
_KEEP = {"\n", "\t"}


def sanitize(text: str) -> str:
    """Everything that must be true of text before it is allowed near a terminal."""
    text = str(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c for c in text if c in _KEEP or (ord(c) >= 0x20 and ord(c) != 0x7F))
    text = text.strip()
    if len(text) > MAX_CONTENT_CHARS:
        text = text[:MAX_CONTENT_CHARS].rstrip()
    return text


def encode_paste(text: str) -> bytes:
    """The bytes a terminal emulator would send if a human pasted `text` — **not** the Enter.

    The Enter is deliberately not here. It has to reach the pty in its own write, a beat
    later, or the TUI swallows it (module docstring); keeping it out of the encoding is what
    stops it being accidentally re-joined by a future caller.
    """
    body = sanitize(text).replace("\n", "\r").encode("utf-8", "replace")
    if not body:
        return b""
    return PASTE_START + body + PASTE_END


def write_all(fd: int, data: bytes) -> int:
    """`os.write` on a pty can be short. Everything here is a full write or an error."""
    sent = 0
    while sent < len(data):
        sent += os.write(fd, data[sent:])
    return sent


def type_text(master_fd: int, text: str, *, submit_delay: float = SUBMIT_DELAY) -> int:
    """Type `text` into the pty as one bracketed paste, then Enter. Returns bytes written.

    The signature the plan names. **This one sleeps**, so it is for callers that own their
    thread — a script, a test. The proxy loop must not use it: `Injector.flush` splits the
    same two writes across loop passes instead, because a proxy thread asleep for 150ms is a
    terminal frozen for 150ms.
    """
    data = encode_paste(text)
    if not data:
        return 0
    written = write_all(master_fd, data)
    time.sleep(submit_delay)
    return written + write_all(master_fd, SUBMIT)


class Injector:
    """The queue between "an instruction arrived" and "the user has stopped typing".

    `enqueue` is called from other threads (the control server now, Phase 2's poller
    later); `flush` is called only from the proxy loop, which is what keeps two threads from
    interleaving writes into the same pty.
    """

    def __init__(
        self,
        *,
        quiet_period: float = DEFAULT_QUIET_PERIOD,
        submit_delay: float = SUBMIT_DELAY,
        wake=None,
    ):
        self._lock = threading.Lock()
        self._pending: list[tuple[str, str | None]] = []
        self._quiet_period = max(0.0, float(quiet_period))
        self._submit_delay = max(0.0, float(submit_delay))
        #: A paste that is in the terminal but has not been submitted yet:
        #: `(deadline, message_id)`. At most one at a time — the next instruction's paste
        #: must not go in before this one's Enter, or two instructions merge into one prompt.
        self._awaiting: tuple[float, str | None] | None = None
        # Called after `enqueue` so the proxy loop's `select` returns now rather than at the
        # end of its timeout. Without it an instruction can sit for a second doing nothing.
        self._wake = wake
        self._on_delivered = None
        self._last_keystroke = 0.0
        self._delivered: list[str | None] = []

    # -- from the proxy loop --

    def set_wake(self, wake) -> None:
        """The proxy owns the pipe that interrupts `select`, and it is constructed after the
        injector, so it hands the callback back here rather than the other way round."""
        self._wake = wake

    def set_on_delivered(self, callback) -> None:
        """Called once, after any flush that actually wrote something.

        Phase 2's poller is the caller. It needs this because *it* cannot know when the
        bytes went in — the quiet period means an instruction can be handed over here and
        typed a minute later — and the ack it owes the backend has to follow the typing, not
        the handing over. See `poller.py`'s note on the order.
        """
        self._on_delivered = callback

    def note_keystroke(self) -> None:
        """The user typed. Called for every read from the real keyboard, so it has to stay
        this cheap."""
        self._last_keystroke = time.monotonic()

    def next_timeout(self, default: float) -> float:
        """How long the proxy loop may sleep. With something pending, no longer than the
        remaining quiet period, or W6 turns a 1.5s hold into a 1.5s + one-poll hold."""
        with self._lock:
            awaiting = self._awaiting
            if awaiting is not None:
                # An un-submitted paste is the tightest deadline there is: until the Enter
                # goes in, the instruction is sitting in the user's prompt doing nothing.
                return max(0.005, min(default, awaiting[0] - time.monotonic()))
            if not self._pending:
                return default
        return max(0.05, min(default, self._remaining()))

    def flush(self, master_fd: int) -> int:
        """Do whatever this pass is due. Returns how many instructions were *submitted*.

        Two phases, and never both for the same instruction in one pass — that gap is the
        entire point (module docstring). Phase one presses Enter on the paste already in the
        terminal, once its delay has elapsed; phase two starts the next instruction. Running
        them in this order means a single pass can finish one instruction and begin the next,
        so a queue of three does not cost three extra trips round `select`.
        """
        sent = 0

        with self._lock:
            awaiting = self._awaiting
        if awaiting is not None:
            deadline, message_id = awaiting
            if time.monotonic() < deadline:
                return 0  # still in the gap; `next_timeout` brings us back on time
            try:
                write_all(master_fd, SUBMIT)
            except OSError:
                # The pty is gone. Leave the id unrecorded — see the note below.
                with self._lock:
                    self._awaiting = None
                return 0
            sent = 1
            with self._lock:
                self._awaiting = None
                self._delivered.append(message_id)
            debug(f"submitted paste for message_id={message_id!r}")
            if self._on_delivered:
                self._on_delivered()

        with self._lock:
            if self._pending and self._remaining() <= 0:
                text, message_id = self._pending.pop(0)
            else:
                return sent
        try:
            if write_all(master_fd, encode_paste(text)):
                with self._lock:
                    self._awaiting = (time.monotonic() + self._submit_delay, message_id)
                debug(
                    f"typed paste for message_id={message_id!r}; "
                    f"awaiting submit in {self._submit_delay}s"
                )
        except OSError:
            # The pty is gone, which means the child is gone and the wrapper is on its way
            # out. Dropping it here is right: the ledger only records an id after a
            # successful write, so an undelivered instruction is still pending at the
            # backend and arrives in the next session. The same reasoning covers a wrapper
            # that dies in the gap between the paste and the Enter — the id was never
            # recorded, so the instruction comes back next session rather than being lost.
            pass
        return sent

    # -- from anywhere --

    def enqueue(self, text: str, *, message_id=None) -> int:
        """Queue one instruction. Returns the new pending count."""
        cleaned = sanitize(text)
        if not cleaned:
            return self.pending()
        with self._lock:
            self._pending.append((cleaned, message_id if message_id is None else str(message_id)))
            count = len(self._pending)
        debug(f"enqueued instruction (message_id={message_id!r}); {count} pending")
        if self._wake:
            self._wake()
        return count

    def pending(self) -> int:
        """Instructions not yet submitted — including one sitting in the terminal waiting
        out its submit delay, which `/audiochatty-status` should not call delivered."""
        with self._lock:
            return len(self._pending) + (1 if self._awaiting is not None else 0)

    def drain_delivered(self) -> list[str | None]:
        """The message ids typed in since the last call, and clears them.

        Drained rather than read so there is exactly one consumer and no id can be recorded
        twice. An id only appears here after a full, successful write into the pty — which
        is what makes it safe for the poller to treat this as "delivered".
        """
        with self._lock:
            typed, self._delivered = self._delivered, []
        return typed

    # -- internal --

    def _remaining(self) -> float:
        """Seconds still to wait. Zero when the user has been quiet long enough — including
        the case where they have never typed at all, which is a session sitting idle while
        its owner is somewhere else with their phone, i.e. the whole point of this."""
        if not self._last_keystroke:
            return 0.0
        return max(0.0, self._quiet_period - (time.monotonic() - self._last_keystroke))
