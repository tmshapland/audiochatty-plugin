"""Connecting the session this wrapper started, without anyone typing anything.

Until this existed, `audiochatty run` opened a return path and then waited to be told it
was wanted: a user had to run `/audiochatty-connect` inside the session, every session,
forever. That step never did any work the wrapper couldn't do — it was only the thing that
happened to know the Claude Code session id. The wrapper knows it first, because it mints
the uuid and passes `--session-id` (`__main__.py`), so this module closes the gap.

**The protocol is not reimplemented here.** `audiochat.connect_session` owns the order
(register → bind → verified → marker) and the rollback, and it takes the bind as a
callable so it never has to know which side of the pty it is running on. What is left here
is three decisions:

1. **When to stay out of it.** Not paired → nothing at all, and no network call; the
   machine hasn't opted in yet and `audiochatty run` has to behave exactly like `claude`.
   No minted session id (`--resume`, `--continue`, an explicit `--session-id`) → nothing
   either, because we cannot know the session id before the session exists. That case
   belongs to `scripts/session_start_hook.py`, which runs *inside* the session and can
   read its id off the hook payload. The two halves are disjoint by construction, so they
   can never race to register the same session.

2. **Silence.** There is nowhere to print: stdout is the child's screen and a stray line
   there is a corrupted TUI, and stderr is no better once the TUI owns the display. So the
   confirmation is the session appearing on the user's phone, and `AUDIOCHATTY_DEBUG=1` is
   what narrates it locally.

3. **Where a failure goes instead.** Because of (2), a failed connect has no voice at all
   — so the reason is written into the rendezvous file and `/audiochatty-status` reads it
   out. "Connected fine" and "failed at launch" must never look the same to a user.

**Off the critical path, on a thread.** The child is already drawing by the time this runs.
A registration POST against a sleeping Render backend can take its full timeout, and the
terminal must not wait a millisecond for it.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import audiochat  # noqa: E402

from wrapper.store import WrapperState, debug  # noqa: E402


def in_process_bind(state: WrapperState, *, claude_session_id: str, base: str, token: str,
                    name: str):
    """The bind, as `connect_session` wants it: one callable that raises on failure.

    No loopback hop — this *is* the wrapper, so it calls its own bind rules directly. The
    session check inside them is satisfied by construction here (we are binding the id
    we minted), and `ApiError` is raised for a refusal so the caller's rollback path is the
    same one the slash command takes.
    """

    def bind(agent_session_id: str) -> None:
        status, payload = state.bind(
            {
                "agent_session_id": agent_session_id,
                "claude_session_id": claude_session_id,
                "backend_url": base,
                "token": token,
                "session_name": name,
                # Deliberately not `verified_reported`: `connect_session` posts
                # `/agent/session/verified` after this returns, and if that call is lost we
                # want the poll loop to keep trying. One redundant POST is the cost of being
                # wrong this way; a session the user is wrongly told they can't talk to is
                # the cost of the other way.
            }
        )
        if status >= 400:
            raise audiochat.ApiError(status, payload)

    return bind


def connect(state: WrapperState, *, name: str | None = None, repo_path: str | None = None,
            base_url: str | None = None) -> str:
    """Do the connect, synchronously. Returns `""` on success or an error code.

    Separate from `start` so the whole of it is testable without a thread.
    """
    snapshot = state.snapshot()
    claude_session_id = str(snapshot.get("expected_session_id") or "")
    if not claude_session_id:
        # `--resume` and friends. The SessionStart hook has this one.
        debug("no minted session id, so leaving the connect to the SessionStart hook")
        return "deferred"

    token = audiochat.device_token()
    if not token:
        # Not paired. Zero network calls, nothing on disk, no error recorded — this is not
        # a failure, it is a machine that has not opted in, and `/audiochatty-status`
        # already says so from the credentials file.
        debug("not paired, so not connecting")
        return "not_paired"

    repo = repo_path or os.getcwd()
    session_name = (name or "").strip() or audiochat.default_session_name(repo)
    base = base_url or audiochat.backend_url()

    result = audiochat.connect_session(
        claude_session_id=claude_session_id,
        repo_path=repo,
        name=session_name,
        token=token,
        base_url=base,
        bind=in_process_bind(
            state, claude_session_id=claude_session_id, base=base, token=token,
            name=session_name,
        ),
        wrapper_pid=snapshot.get("pid"),
        wrapper_port=snapshot.get("port"),
        # There is no connect *turn* to swallow here — the next turn this session ends
        # is the user's real first piece of work, and they should hear about it.
        skip_next_turn=False,
    )

    if not result.ok:
        state.record_connect_error(result.error or "failed")
        return result.error or "failed"

    state.clear_connect_error()
    debug(f'connected at launch as "{result.name}"')
    return ""


def start(state: WrapperState, *, name: str | None = None, repo_path: str | None = None,
          base_url: str | None = None) -> threading.Thread:
    """Kick `connect` off in the background and hand the terminal straight back."""

    def run() -> None:
        try:
            connect(state, name=name, repo_path=repo_path, base_url=base_url)
        except Exception as err:  # pragma: no cover - belt and braces
            # A crash in here must never take the wrapper with it: the user is typing into
            # a terminal, and losing the return path is survivable where losing the session
            # is not.
            debug(f"connect at launch raised: {err!r}")
            try:
                state.record_connect_error("failed")
            except Exception:
                pass

    thread = threading.Thread(target=run, name="audiochatty-connect", daemon=True)
    thread.start()
    return thread
