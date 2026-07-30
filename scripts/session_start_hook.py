#!/usr/bin/env python3
"""The `SessionStart` hook — connect the one kind of session the wrapper can't name itself.

`wrapper_return_path_plan.md` Phase 6.5 · W13.

`audiochatty run` connects its own session (`wrapper/connect.py`), because it mints the
session id and passes `--session-id`. That covers every ordinary launch. It does not cover
`--resume`, `--continue`, or an explicit `--session-id`, where the id is the user's to
decide and the wrapper cannot know it before the session exists. This hook runs *inside*
the session, where the id arrives on stdin, and closes that gap.

**The two halves are disjoint, not redundant, and that is deliberate.** This hook acts only
when the wrapper published no `expected_session_id` — i.e. exactly the case the wrapper
skipped. Overlapping them instead would mean two processes racing to register the same
session at launch, which is not wrong so much as pointless.

**Verified against the hooks reference, 2026-07-29:**

- `source` is one of `startup`, `resume`, `clear`, `compact`, `fork`. We branch on none of
  them, because the state on disk answers the question better than the reason does — bound
  already means nothing to do, whatever fired us.
- `SessionStart` **cannot block a session from starting**, so nothing here can cost the
  user their terminal.
- **stdout is added to the model's context** for this hook. That makes silence a
  correctness requirement rather than a style choice: one stray line would be injected into
  every session on this machine, forever. Everything here goes to stderr, under
  `AUDIOCHATTY_DEBUG` only, and the exit code is always 0.
- It fires on **every** session on the machine, and the docs say plainly to keep it fast.
  So the first thing below is one environment-variable read, and an unwrapped session is
  gone before it touches the disk.

Two cases it deliberately declines:

- **A `/clear` after `/audiochatty-disconnect`.** `SessionStart` fires again on `/clear` in
  the same session, so without the tombstone a user who deliberately went quiet would be
  reconnected a minute later by a hook. Automatic reconnection is exactly wrong there; a
  typed `/audiochatty-connect` is how you come back.
- **A forked session** (`--fork-session`, `/fork`, `/branch`). It gets a new session id and
  inherits the wrapper's environment, so `find_wrapper` reports `session_mismatch` — the
  same W3 refusal a nested plain `claude` gets, for the same reason: the terminal belongs
  to the original session.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    # Cheapest possible exit for the fourteen other terminals open on this machine: one
    # environment read, no import cost that matters, no file touched.
    if not os.environ.get("AUDIOCHATTY_WRAPPER_PORT", "").strip():
        return 0

    import audiochat

    hook = _read_hook_input()
    claude_session_id = str(hook.get("session_id") or "")
    if not claude_session_id:
        return 0
    source = str(hook.get("source") or "")

    if audiochat.session_was_disconnected(claude_session_id):
        _debug(f"session was disconnected by hand; not reconnecting it on {source!r}")
        return 0

    if audiochat.load_marker(claude_session_id):
        _debug("already registered; nothing to do")
        return 0

    wrapper, problem = audiochat.find_wrapper(claude_session_id)
    if wrapper is None:
        # `session_mismatch` is the fork/nested case; `none` is a stale variable from a
        # wrapper that has exited. Neither is ours to fix, and neither is worth a word to a
        # user who did not ask.
        _debug(f"no wrapper for this session ({problem or 'none'})")
        return 0

    if str(wrapper.get("expected_session_id") or ""):
        # The wrapper minted this session's id, so it connects itself. Not our half.
        _debug("wrapper owns this session's connect; standing down")
        return 0

    if str(wrapper.get("claude_session_id") or "") == claude_session_id:
        _debug("wrapper is already bound to this session")
        return 0

    token = audiochat.device_token()
    if not token:
        _debug("machine not paired; nothing to connect with")
        return 0

    repo_path = str(hook.get("cwd") or "") or os.getcwd()
    name = audiochat.default_session_name(repo_path)
    base = audiochat.backend_url()

    result = audiochat.connect_session(
        claude_session_id=claude_session_id,
        repo_path=repo_path,
        name=name,
        token=token,
        base_url=base,
        bind=lambda agent_session_id: audiochat.bind_wrapper(
            wrapper,
            agent_session_id=agent_session_id,
            claude_session_id=claude_session_id,
            base=base,
            token=token,
            name=name,
        ),
        wrapper_pid=wrapper.get("pid"),
        wrapper_port=wrapper.get("port"),
        # Same reasoning as the wrapper's own connect: there is no `/audiochatty-connect`
        # turn to swallow, so the next turn this session finishes is real work.
        skip_next_turn=False,
    )
    _debug(
        f'source={source!r} connect -> {"ok" if result.ok else result.error}'
        + (f' as "{result.name}"' if result.ok else "")
    )
    return 0


def _read_hook_input() -> dict:
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _debug(message: str) -> None:
    """Stderr only. See the docstring: this hook's stdout becomes model context."""
    if os.environ.get("AUDIOCHATTY_DEBUG"):
        print(f"[audiochatty] {message}", file=sys.stderr)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        _debug(f"unhandled: {exc}")
        sys.exit(0)
