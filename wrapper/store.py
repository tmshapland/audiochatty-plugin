"""The wrapper's state: what is on disk, and what `/bind` does to it.

`wrapper_return_path_plan.md` Phase 1 · W3, W8. Ported from `channel/server.ts:116`,
`:180`, `:338`, `:768`, `:878`, `:925`.

Two things live here rather than in `control.py`, on purpose. The rendezvous file is the
only state a wrapper keeps, and the bind/unbind rules are the only decisions it makes;
`control.py` is then nothing but HTTP plumbing, and these rules can be tested without
opening a socket.

The on-disk helpers are imported from `scripts/audiochat.py` rather than reimplemented.
`write_private_json` in particular is not a function to have two copies of — it creates the
file 0600 with `os.open` instead of chmod-ing afterwards, because on a shared machine that
window is the whole vulnerability.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import audiochat  # noqa: E402

config_dir = audiochat.config_dir
read_json = audiochat.read_json
write_private_json = audiochat.write_private_json
device_token = audiochat.device_token
safe_filename = audiochat._safe_filename
pid_is_alive = audiochat._pid_is_alive
now_iso = audiochat._now_iso

RENDEZVOUS_VERSION = 1
_PID_FILE = re.compile(r"^\d+\.json$")


_verbose = False


def set_verbose(value: bool) -> None:
    """`--verbose` reaches the same narration `AUDIOCHATTY_DEBUG=1` does, without setting the
    env var itself — `__main__.py`'s `child_env` is a copy of `os.environ`, and mutating that
    would leak the setting into the wrapped `claude` process too."""
    global _verbose
    _verbose = bool(value)


def debug(message: str) -> None:
    """`AUDIOCHATTY_DEBUG=1` or `--verbose` narrates the wrapper. Stderr only, and never
    stdout: stdout is the child's screen, and a stray line there is a corrupted TUI."""
    if _verbose or os.environ.get("AUDIOCHATTY_DEBUG"):
        print(f"[audiochatty wrapper] {message}", file=sys.stderr, flush=True)


def wrappers_dir() -> Path:
    """`~/.audiochatty/wrappers`, 0700. A different directory from the old `channels/` so a
    machine mid-upgrade cannot serve one kind of file to a reader expecting the other."""
    directory = config_dir() / "wrappers"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory


def rendezvous_path(pid: int | None = None) -> Path:
    return wrappers_dir() / f"{pid if pid is not None else os.getpid()}.json"


def read_all() -> list[dict]:
    """Every live wrapper on this machine. A file whose process is gone is skipped rather
    than deleted — a reader that garbage-collects another process's state is a reader that
    can delete a live session's file after a pid wraps around."""
    found: list[dict] = []
    try:
        entries = sorted(os.listdir(wrappers_dir()))
    except OSError:
        return found
    for entry in entries:
        if not _PID_FILE.match(entry):
            continue
        record = read_json(wrappers_dir() / entry)
        pid = record.get("pid")
        if isinstance(pid, int) and pid_is_alive(pid):
            found.append(record)
    return found


def prune_stale() -> None:
    """Delete rendezvous files belonging to processes that no longer exist.

    The normal exit path removes its own file, so this is for the abnormal one: a `kill -9`,
    a laptop that slept through a crash. A stale file is how `connect` binds to a corpse —
    it POSTs to a port nothing is listening on, or worse, to a port something else has
    since been given.
    """
    try:
        entries = os.listdir(wrappers_dir())
    except OSError:
        return
    for entry in entries:
        if not _PID_FILE.match(entry):
            continue
        pid = int(entry[:-5])
        if pid == os.getpid() or pid_is_alive(pid):
            continue
        try:
            (wrappers_dir() / entry).unlink()
            debug(f"pruned stale rendezvous for dead pid {pid}")
        except OSError:
            pass  # someone else got there first


class WrapperState:
    """One wrapper's binding, and the rendezvous file that advertises it.

    Every method is safe to call from the control server's threads; the proxy loop only
    reads `snapshot()`. The lock is an `RLock` because `bind` writes the file while holding
    it.
    """

    def __init__(
        self,
        *,
        pid: int,
        port: int,
        child_pid: int,
        expected_session_id: str | None,
        injector=None,
        poller=None,
    ):
        self._lock = threading.RLock()
        self._injector = injector
        # Phase 2. The dependency goes this way — bind rules start the poll loop, and the
        # poller knows nothing about rendezvous files — so `poller.py` can import from here
        # without a cycle.
        self._poller = poller
        self._cleaned = False
        self.path = rendezvous_path(pid)
        self.generation = 0
        self.binding: dict | None = None
        self._record: dict = {
            "version": RENDEZVOUS_VERSION,
            "kind": "wrapper",
            "pid": pid,
            "child_pid": child_pid,
            "port": port,
            "started_at": now_iso(),
            "expected_session_id": expected_session_id,
            "generation": 0,
            "bound": False,
            "verified": False,
            "claude_session_id": None,
            "agent_session_id": None,
            "session_name": None,
            "backend_url": None,
            "bound_at": None,
            "verified_at": None,
            # W13. Connecting happens at launch and prints nothing — stdout is the child's
            # screen — so a *failed* connect would otherwise vanish without trace. These two
            # are where the reason goes, and `/audiochatty-status` is what reads them out.
            "connect_error": None,
            "connect_error_at": None,
        }
        self._write()

    # -- the file --

    def _write(self, **patch) -> None:
        with self._lock:
            self._record.update(patch)
            if self._cleaned:
                # W13 made this reachable. The connect runs on a background thread, so a
                # wrapper that exits while it is still in flight can have a write land
                # *after* `cleanup()` unlinked the file — recreating a rendezvous file for a
                # process that no longer exists, which is the one thing `cleanup` exists to
                # prevent. Readers skip dead pids and the next launch prunes it, so the cost
                # was small; the guard is smaller.
                debug("dropping a rendezvous write after cleanup")
                return
            try:
                write_private_json(self.path, self._record)
            except OSError as err:
                debug(f"could not write rendezvous: {err}")

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._record)

    def cleanup(self) -> None:
        """A rendezvous file for a process that has exited is worse than no file at all, so
        every way out of the wrapper goes through here."""
        with self._lock:
            if self._cleaned:
                return
            self._cleaned = True
        try:
            self.path.unlink()
        except OSError:
            pass

    def record_connect_error(self, error: str) -> None:
        """W13. The launch tried to connect and couldn't; leave the reason where
        `/audiochatty-status` will find it.

        Only the machine-readable code is stored. The sentence a user reads belongs with the
        command that talks to users (`audiochat._connect_error_line`), so the same failure
        does not end up worded two ways.
        """
        self._write(connect_error=error or None, connect_error_at=now_iso() if error else None)
        debug(f"connect at launch failed: {error}")

    def clear_connect_error(self) -> None:
        with self._lock:
            if not self._record.get("connect_error"):
                return
        self._write(connect_error=None, connect_error_at=None)

    # -- the decisions --

    def bind(self, body: dict) -> tuple[int, dict]:
        """`POST /bind` — the only way this wrapper starts doing anything.

        Ported from `handleBind` (`channel/server.ts:768`) with the handshake probe removed
        (W8) and one check added (W3, `session_mismatch`).
        """
        agent_session_id = str(body.get("agent_session_id") or "").strip()
        claude_session_id = str(body.get("claude_session_id") or "").strip()
        backend_url = str(body.get("backend_url") or "").strip().rstrip("/")
        token = str(body.get("token") or "")
        session_name = str(body.get("session_name") or "")[:128]
        # Optional, and absent means "no". See the note in `Poller.start`: the caller is
        # telling us whether it has already posted `/agent/session/verified` itself, and if
        # it has not — or does not say — the poll loop keeps trying until the backend
        # confirms. Phase 3's `cmd_connect` is the only caller that will set it.
        verified_reported = bool(body.get("verified_reported"))

        if not (agent_session_id and claude_session_id and backend_url and token):
            return 400, {"error": "missing_fields"}

        # The token must match this machine's credentials, so another local process — a
        # stray script, something that wandered in — cannot point this wrapper at a session
        # it does not own.
        stored = device_token()
        if not stored or stored != token:
            debug("bind refused: token does not match this machine")
            return 403, {"error": "token_mismatch"}

        with self._lock:
            expected = self._record.get("expected_session_id")
            if expected and claude_session_id != expected:
                # W3. Not the session this wrapper started — almost certainly a plain
                # `claude` run inside a wrapped one, which inherited our port from the
                # environment. Binding it would aim someone's spoken instructions at a
                # terminal they cannot see.
                debug(f"bind refused: {claude_session_id} is not our session")
                return 409, {
                    "error": "session_mismatch",
                    "expected_session_id": expected,
                }

            if self.binding and self.binding["claude_session_id"] != claude_session_id:
                # A wrapper that can be redirected mid-session is a wrapper that can put
                # one person's instruction in another person's terminal.
                return 409, {
                    "error": "already_bound",
                    "claude_session_id": self.binding["claude_session_id"],
                    # Always false here — the same session takes the rebind path below —
                    # but stated rather than implied, because the caller's two cases
                    # ("this is mine, refresh it" and "this is somebody else's") are the
                    # whole difference and it should not have to infer one from a status
                    # code.
                    "same_session": False,
                    "verified": bool(self._record["verified"]),
                }

            if self.binding:
                # The *same* session binding again is not that: it is
                # `/audiochatty-connect` run twice in the same terminal, or run again after
                # re-pairing, in which case the token it carries is newer than the one in
                # memory. Refresh in place and say so. The delivered ledger (Phase 2) is
                # untouched — same session, same conversation.
                self.binding.update(
                    agent_session_id=agent_session_id,
                    backend_url=backend_url,
                    token=token,
                    session_name=session_name,
                )
                if self._poller is not None:
                    self._poller.refresh(self.binding)
                self._write(
                    agent_session_id=agent_session_id,
                    backend_url=backend_url,
                    session_name=session_name or None,
                    bound_at=now_iso(),
                )
                debug("rebound the same session")
                return 200, {
                    "status": "rebound",
                    "pid": self._record["pid"],
                    "claude_session_id": claude_session_id,
                    "agent_session_id": agent_session_id,
                    "verified": True,
                }

            self.binding = {
                "agent_session_id": agent_session_id,
                "claude_session_id": claude_session_id,
                "backend_url": backend_url,
                "token": token,
                "session_name": session_name,
                "verified_reported": verified_reported,
            }
            self.generation += 1
            stamp = now_iso()
            # W8: `verified` is set here, with no handshake and no round trip. The old
            # plugin could not tell whether its notifications were honoured, so it had to
            # prove reachability by sending a nonce and waiting for it to come back. This
            # process owns the pty. Binding *is* the proof.
            self._write(
                generation=self.generation,
                bound=True,
                bound_at=stamp,
                verified=True,
                verified_at=stamp,
                claude_session_id=claude_session_id,
                agent_session_id=agent_session_id,
                session_name=session_name or None,
                backend_url=backend_url,
            )
            # Phase 2, W-note in `Poller._loop`: polling starts here, at the bind, and no
            # longer waits for a handshake to prove the session can be reached.
            if self._poller is not None:
                self._poller.start(self.binding, self.generation)
            debug(f"bound to session {claude_session_id}")
            return 200, {
                "status": "bound",
                "pid": self._record["pid"],
                "claude_session_id": claude_session_id,
                "agent_session_id": agent_session_id,
                "verified": True,
            }

    def unbind(self, body: dict) -> tuple[int, dict]:
        """`POST /unbind` — `/audiochatty-disconnect`'s half of the same handshake.

        Bumping the generation is what stops a Phase 2 poll loop that is currently asleep
        inside a 30s wait: it wakes, sees a number that is not its own, and exits.
        """
        stored = device_token()
        token = str(body.get("token") or "")
        if stored and token != stored:
            return 403, {"error": "token_mismatch"}

        if self._poller is not None:
            # Before the generation moves, so anything already typed is recorded and acked
            # by the loop that typed it rather than orphaned.
            self._poller.stop()

        with self._lock:
            was = self.binding["claude_session_id"] if self.binding else None
            self.binding = None
            self.generation += 1
            self._write(
                generation=self.generation,
                bound=False,
                bound_at=None,
                verified=False,
                verified_at=None,
                claude_session_id=None,
                agent_session_id=None,
                session_name=None,
                backend_url=None,
            )
        debug(f"unbound from {was or 'nothing'}")
        return 200, {"status": "unbound", "claude_session_id": was}

    def status(self) -> tuple[int, dict]:
        """Cheap enough to be worth having when someone is debugging a wrapper that is
        running but silent. It says nothing a reader of the rendezvous file cannot already
        see, and in particular says nothing about the token."""
        with self._lock:
            record = self._record
            return 200, {
                "pid": record["pid"],
                "child_pid": record["child_pid"],
                "port": record["port"],
                "bound": bool(self.binding),
                "verified": bool(record["verified"]),
                "generation": self.generation,
                "claude_session_id": record["claude_session_id"],
                "agent_session_id": record["agent_session_id"],
                "expected_session_id": record["expected_session_id"],
                "pending_injections": self._injector.pending() if self._injector else 0,
                # "bound but silent" is the question someone debugging this actually has,
                # and it is one the rendezvous file cannot answer.
                "polling": self._poller.polling() if self._poller else False,
            }

    def inject(self, body: dict) -> tuple[int, dict]:
        """`POST /inject` — hand text to the injector, which types it into the pty once the
        user has stopped typing (W6).

        Not in the plan's three-endpoint list; see the deviation note in `__main__.py`. The
        token check is the whole of the security here, and `not_bound` keeps the "an
        unbound wrapper does nothing at all" property the old channel had.
        """
        text = str(body.get("text") or "")
        token = str(body.get("token") or "")
        if not text.strip():
            return 400, {"error": "missing_fields"}

        stored = device_token()
        if not stored or stored != token:
            return 403, {"error": "token_mismatch"}

        with self._lock:
            if not self.binding:
                return 409, {"error": "not_bound"}

        if self._injector is None:  # pragma: no cover - only in unit-level construction
            return 409, {"error": "no_injector"}
        pending = self._injector.enqueue(text, message_id=body.get("message_id"))
        return 202, {"status": "queued", "pending": pending}
