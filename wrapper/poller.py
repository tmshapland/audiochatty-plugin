"""Fetching what you said, and getting it typed in.

The numbers below are the old numbers and the reasoning is the old reasoning — nothing
about the queue changed when the delivery mechanism moved from the old channel to this
wrapper. They were tuned against a real backend once, which is the reason to trust a number
like the 5s/30s/60s triple.

## How a laptop behind NAT hears anything

It doesn't — it asks. Once a session is bound, `GET /agent/inbound` every 5s while
something could plausibly be in flight, backing off to 30s once it has been quiet, and
skipping the network entirely for a minute after a failure. That last part is the same
circuit-breaker discipline `stop_hook.py` uses, for the same reason: a sleeping Render
service must not become a hot loop.

## Why the order in `_deliver` is not negotiable

    type it in  →  write the ledger  →  ack the backend

A crash between the first two replays into a duplicate; a crash between the last two
replays into a dedupe. Delivery is at-least-once and the ledger is what turns that
into exactly-once, so the ledger has to be the *earlier* of the two records. An instruction
edits files on someone's machine — a duplicate is the expensive direction to fail in.

The one difference from the old channel is where "typed it in" comes from. `mcp.notification()`
resolved when the bytes hit the transport, so the old code knew synchronously. Here the
bytes go into a pty on the *proxy* thread, and possibly not for a while: an injection is
held back until the user has stopped typing. So this loop hands text to the
`Injector` and waits to be told it landed (`Injector.drain_delivered`), and nothing reaches
the ledger — or the ack — until it actually did. An instruction still queued in the
injector is deliberately in neither, which is why a wrapper killed mid-hold re-delivers it
rather than losing it.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from wrapper import inject
from wrapper.store import debug, read_json, safe_filename, wrappers_dir, write_private_json

# -- the numbers, unchanged from the old channel -----------------------------------------

#: While something could plausibly be in flight.
POLL_ACTIVE = 5.0
#: Once it has been quiet. A message is only written when the caller hangs up, so half a
#: minute of latency on a session nobody is talking to costs nothing anyone can feel.
POLL_IDLE = 30.0
#: Empty polls before backing off — 6 × 5s, so a minute of silence.
IDLE_AFTER_EMPTY_POLLS = 6
#: After a failed request, skip the network entirely for this long. Same value and same
#: reasoning as `BREAKER_COOLDOWN` in `audiochat.py`: one attempt pays the timeout, the next
#: twelve pay nothing, and a Render service asleep for hours costs us twelve requests an
#: hour instead of seven hundred.
BREAKER_COOLDOWN = 60.0
#: Nothing here is interactive, but nothing here may hang either.
REQUEST_TIMEOUT = 10.0
#: Ids kept in the delivered ledger. Far more than a session will ever see; the cap is here
#: so a long-lived session cannot grow the file without bound.
MAX_DELIVERED_IDS = 500

MAX_SENDER_CHARS = 128
MAX_CREATED_AT_CHARS = 64
MAX_ID_CHARS = 128

USER_AGENT = "audiochatty-wrapper/0.1.0"


def _tunable(name: str, default: float) -> float:
    """A cadence, overridable from the environment.

    This exists for the test suite, which cannot spend a minute per assertion waiting out a
    real circuit breaker. Not documented as a user-facing option for the same reason
    `--claude-bin` is not: the numbers above are the ones that were reasoned about.
    """
    try:
        value = float(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default
    return value if value > 0 else default


class BackendError(Exception):
    """The backend did not answer, or answered with a status. One class for both, because
    from here they are the same event: sit out a cooldown and try again."""


# -- what came back ----------------------------------------------------------------------


def parse_inbound(payload) -> list[dict]:
    """Normalise whatever came back into the four fields we use.

    The dropping matters: a malformed row must never turn into an actual keystroke. A row
    with no id cannot be deduped, and a row with no text has nothing to say, so neither is
    worth the risk of guessing about.
    """
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("messages"), list):
        rows = payload["messages"]
    else:
        rows = []

    messages: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        message_id = str(row.get("id") or "")[:MAX_ID_CHARS]
        text = str(row.get("text") or "")[:inject.MAX_CONTENT_CHARS]
        if not message_id or not text.strip():
            continue
        messages.append(
            {
                "id": message_id,
                "text": text,
                "sender_name": str(row.get("sender_name") or "")[:MAX_SENDER_CHARS],
                "created_at": str(row.get("created_at") or "")[:MAX_CREATED_AT_CHARS],
            }
        )
    return messages


# -- the delivered ledger ----------------------------------------------------------------


class DeliveredLedger:
    """Which instructions have already been typed into this session.

    **Keyed by Claude Code session, not by pid**, and that is the requirement rather than a
    detail: a crash partway through delivering a message must not type it twice,
    and a restart is a new pid — so a pid-keyed ledger would be empty exactly when it is
    needed. What survives a restart is the session: the user relaunches, runs
    `/audiochatty-connect`, and the same `claude_session_id` binds again. The rendezvous
    file stays pid-keyed, because that one really is about a process.

    It lives beside the rendezvous files rather than in a directory of its own, which means
    nothing prunes it — `store.prune_stale` only matches `<pid>.json`. That is the old
    behaviour and it is the right trade: a few hundred bytes per session, against the
    possibility of deleting the one file that stops a duplicate.
    """

    def __init__(self, claude_session_id: str):
        self.path = wrappers_dir() / f"{safe_filename(claude_session_id)}.delivered.json"
        stored = read_json(self.path).get("message_ids")
        self.ids: list[str] = [str(value) for value in stored] if isinstance(stored, list) else []
        self._known = set(self.ids)

    def __contains__(self, message_id: str) -> bool:
        return message_id in self._known

    def add(self, message_id: str) -> None:
        if message_id in self._known:
            return
        self._known.add(message_id)
        self.ids.append(message_id)

    def persist(self) -> None:
        self.ids = self.ids[-MAX_DELIVERED_IDS:]
        self._known = set(self.ids)
        try:
            write_private_json(Path(self.path), {"message_ids": self.ids})
        except OSError as err:
            # Losing the ledger risks a duplicate instruction after a crash. It is not
            # worth dropping the message that is already in the terminal over.
            debug(f"could not persist delivered ids: {err}")


# -- the loop ----------------------------------------------------------------------------


class Poller:
    """One poll loop per binding, started by `/bind` and stopped by `/unbind`.

    Constructed once, in `__main__.cmd_run`, and handed to `store.WrapperState` — which
    calls `start`, `refresh` and `stop` as its bind rules decide. The dependency goes that
    way round on purpose: this module knows nothing about HTTP routes or rendezvous files,
    and `store` knows nothing about the backend.
    """

    def __init__(self, injector):
        self._injector = injector
        self._lock = threading.Lock()
        self._active = _tunable("AUDIOCHATTY_POLL_ACTIVE", POLL_ACTIVE)
        self._idle = _tunable("AUDIOCHATTY_POLL_IDLE", POLL_IDLE)
        self._cooldown = _tunable("AUDIOCHATTY_POLL_COOLDOWN", BREAKER_COOLDOWN)
        self._timeout = _tunable("AUDIOCHATTY_POLL_TIMEOUT", REQUEST_TIMEOUT)

        self._binding: dict | None = None
        self._generation = -1
        self._closing = False
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()

        self._ledger: DeliveredLedger | None = None
        #: Handed to the injector but not yet typed. Neither delivered nor forgettable —
        #: without this a second poll would enqueue the same instruction again while the
        #: first copy was still waiting out the quiet period.
        self._inflight: set[str] = set()
        #: Typed, in the ledger, but the backend has not confirmed the ack. Retried.
        self._unacked: set[str] = set()
        self._verified_reported = True

        # A delivery is worth waking the loop for: the ack is what clears the message from
        # the phone's inbox, and waiting out an idle 30s to send it would show the user a
        # message still in flight that is already on their screen at home. Bound to a method
        # rather than to `self._wake.set`, because `start` replaces the event.
        injector.set_on_delivered(self._nudge)

    # -- what `store.WrapperState` calls --

    def start(self, binding: dict, generation: int) -> None:
        """A fresh bind. Everything per-binding is rebuilt here, including the ledger."""
        with self._lock:
            self._binding = dict(binding)
            self._generation = generation
            self._ledger = DeliveredLedger(binding["claude_session_id"])
            self._inflight = set()
            self._unacked = set()
            # The verification loose end. `/audiochatty-connect` posts
            # `/agent/session/verified` itself and is told not to fail hard if that call does
            # not land, so unless it says otherwise the wrapper assumes the backend has not
            # heard and keeps trying. The default is the safe direction: the cost of being
            # wrong is one redundant POST, and the cost of the other default is a reachable
            # session the user is told they cannot talk to.
            self._verified_reported = bool(binding.get("verified_reported"))
            self._wake = threading.Event()
            self._thread = threading.Thread(
                target=self._loop,
                args=(generation, self._wake),
                name="audiochatty-poll",
                daemon=True,
            )
            self._thread.start()

    def refresh(self, binding: dict) -> None:
        """A re-bind of the *same* session — `/audiochatty-connect` run twice, or run again
        after re-pairing with a newer token. The loop keeps running and the ledger is
        untouched: same session, same conversation."""
        with self._lock:
            if self._binding is None:
                return
            self._binding.update(binding)

    def stop(self) -> None:
        """`/unbind`. The loop exits at its next tick, and the wake makes that now rather
        than up to 30 seconds from now."""
        self._collect_confirmations()
        with self._lock:
            self._binding = None
            self._wake.set()

    def close(self) -> None:
        """The wrapper is exiting. Last chance to record anything that was typed."""
        self._collect_confirmations()
        with self._lock:
            self._closing = True
            self._binding = None
            self._wake.set()
            thread = self._thread
        if thread:
            thread.join(timeout=2)

    def polling(self) -> bool:
        with self._lock:
            return bool(self._binding and self._thread and self._thread.is_alive())

    def _nudge(self) -> None:
        """Cut the current sleep short. Called from the proxy thread when something was
        actually typed, so the ack follows the typing closely."""
        with self._lock:
            self._wake.set()

    # -- the loop --

    def _loop(self, mine: int, wake: threading.Event) -> None:
        """Ask for messages, forever, until the binding ends.

        **Started by the bind, and no longer by a handshake — this is a reversal, and the
        comment it replaces was bought with a real session.** The old channel deliberately
        waited for `audiochatty_ack` before its first poll, for two reasons. The first was
        cosmetic and is gone with the handshake itself: a handshake event and an instruction
        arriving in the same batch read to the model as one notification with two paragraphs,
        and it answered the handshake and ignored the instruction. The second was real —
        *never inject into a session that has not proven it can receive* — and that failure
        mode no longer exists. A channel could not tell whether its notifications were being
        honoured; this process owns the pty, so a bind that succeeded *is* the proof. Waiting
        would now buy nothing and cost the user a delay on the one instruction most likely to
        be already queued: the one they spoke to a session that has since restarted.
        """
        empty_polls = 0
        debug("poll loop started")

        while True:
            # Clear *before* re-reading the state, never after. `stop` and `_nudge` both
            # mutate what they are signalling about and only then set the event, so a signal
            # that lands in the gap is still visible to the check below — and one that lands
            # after it re-sets the event and skips the next sleep. The other order loses a
            # stop and sits out a full 30-second wait on the way to noticing.
            wake.clear()
            binding = self._current(mine)
            if binding is None:
                break
            delay = self._idle if empty_polls >= IDLE_AFTER_EMPTY_POLLS else self._active

            try:
                payload = self._get(
                    binding,
                    f"/agent/inbound?session_id={quote(str(binding['agent_session_id']))}",
                )
                messages = parse_inbound(payload)
                if messages:
                    empty_polls = 0
                    self._deliver(messages)
                else:
                    empty_polls += 1
            except BackendError as err:
                # Every failure is the same failure from here: the backend did not answer,
                # and nothing about that is the terminal's problem. Sit out a minute.
                debug(f"poll failed ({err}); backing off {self._cooldown}s")
                empty_polls = 0
                wake.wait(self._cooldown)
                continue

            self._collect_confirmations()
            if self._pending_acks():
                self._ack(binding)
            if not self._verified_reported:
                self._report_verified(binding)

            wake.wait(delay)

        debug("poll loop stopped")

    def _current(self, mine: int) -> dict | None:
        """The binding this loop belongs to, or `None` if it has been superseded.

        The generation check is what stops a loop that is asleep inside a 30-second wait:
        it wakes, finds a number that is not its own, and exits without touching anything
        the new binding owns.
        """
        with self._lock:
            if self._closing or self._binding is None or self._generation != mine:
                return None
            return dict(self._binding)

    # -- delivery --

    def _deliver(self, messages: list[dict]) -> None:
        """Hand anything new to the injector, and re-ack anything the backend re-served.

        The retry-on-next-check is the last block.
        """
        with self._lock:
            ledger = self._ledger
        if ledger is None:  # pragma: no cover - `start` always builds one
            return

        queued = 0
        for message in messages:
            message_id = message["id"]
            with self._lock:
                if message_id in ledger or message_id in self._inflight:
                    continue
            text = inject.sanitize(message["text"])
            if not text:
                # Nothing typeable survived the sanitiser — a message of control characters
                # and nothing else. Acking it is the only way to stop the backend serving
                # it forever, and there is nothing to type, so record it as done.
                with self._lock:
                    ledger.add(message_id)
                    self._unacked.add(message_id)
                continue
            # Enqueue before recording it in flight, so a failure to enqueue leaves the
            # message pending at the backend rather than silently owned by nobody.
            self._injector.enqueue(text, message_id=message_id)
            with self._lock:
                self._inflight.add(message_id)
            queued += 1

        if queued:
            debug(f"queued {queued} instruction(s) for injection")

        # The retry-on-next-check: anything the backend served that we have already typed
        # means an ack that never landed. Say it again rather than letting the same
        # instruction come back forever.
        with self._lock:
            for message in messages:
                if message["id"] in ledger:
                    self._unacked.add(message["id"])

    def _collect_confirmations(self) -> None:
        """Move anything the injector actually typed into the ledger, then persist.

        This is the "record it as delivered" step, and it runs *before* any ack — see the
        module docstring. It is called from the poll thread and, on the way out, from
        `stop`/`close`; `Injector.drain_delivered` is the synchronisation point.
        """
        typed = [str(value) for value in self._injector.drain_delivered() if value]
        if not typed:
            return
        with self._lock:
            ledger = self._ledger
            if ledger is None:
                return
            for message_id in typed:
                self._inflight.discard(message_id)
                ledger.add(message_id)
                self._unacked.add(message_id)
        # Outside the lock: this writes a file, and `_nudge` runs on the proxy thread — the
        # one thread in this process that must never wait on anything.
        ledger.persist()
        debug(f"typed {len(typed)} instruction(s) into the session")

    def _pending_acks(self) -> bool:
        with self._lock:
            return bool(self._unacked)

    def _ack(self, binding: dict) -> None:
        """"I put these in front of Claude." A failure leaves them in `_unacked` and the
        next poll says it again — the backend keeps serving them until it hears, and the
        ledger keeps them from being typed twice in the meantime.

        Deliberately does not trip the breaker: by the time this runs the instruction is
        already in the terminal, so a failure here is bookkeeping, not delivery. If the
        backend really is down the next `GET` will trip it a moment later anyway.
        """
        with self._lock:
            ids = sorted(self._unacked)
        try:
            self._post(binding, "/agent/inbound/ack", {"message_ids": ids})
        except BackendError as err:
            debug(f"ack failed ({err}); will retry")
            return
        with self._lock:
            for message_id in ids:
                self._unacked.discard(message_id)
        debug(f"acked {len(ids)} message(s)")

    def _report_verified(self, binding: dict) -> None:
        """`POST /agent/session/verified`, retried from here until it lands.

        The phone-side inbox reads `channel_verified_at` directly (`canReply`), so a
        reachable session the backend never heard about is a session the user is wrongly
        told they cannot talk to. Same as the ack: a failure here is not a delivery failure
        and does not trip the breaker.
        """
        try:
            self._post(
                binding,
                "/agent/session/verified",
                {"claude_session_id": binding["claude_session_id"]},
            )
        except BackendError as err:
            debug(f"could not record verification: {err}")
            return
        self._verified_reported = True
        debug("verification recorded with the backend")

    # -- talking to the backend --

    def _get(self, binding: dict, path_and_query: str):
        return self._request(binding, "GET", path_and_query, None)

    def _post(self, binding: dict, route: str, body: dict):
        return self._request(binding, "POST", route, body)

    def _request(self, binding: dict, method: str, path: str, body: dict | None):
        url = f"{binding['backend_url']}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("User-Agent", USER_AGENT)
        request.add_header("Authorization", f"Bearer {binding['token']}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise BackendError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BackendError(str(exc)) from exc
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return {}
