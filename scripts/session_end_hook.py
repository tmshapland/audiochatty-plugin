#!/usr/bin/env python3
"""The `SessionEnd` hook — retire a registration, but only for a real ending.

`coding_agent_build_plan.md` Phase 5 · `coding_agent_summary_plan.md` §8.

**This hook exists to be careful about one thing.** `SessionEnd` fires on `/clear` and on
`/resume`, and neither is the end of anything: the Claude Code session id survives both,
the terminal is still open, and the user still expects to hear from it. Marking the row
`ended` there would silently kill a registration someone is still using, and they would
find out by never hearing from it again — the worst kind of bug this feature can have,
because there is nothing to see.

So the branch is on `reason`, and it is written as an **allow-list of endings** rather
than a deny-list of non-endings. An unrecognised reason does not end the session. The two
failure modes are not symmetric: a session left `active` that has really gone shows a
stale name in a settings list and produces nothing, while a session ended that is really
alive removes a working feature from under someone. Claude Code can add a reason at any
time, and the safe default has to be the one that survives that.

Like the Stop hook, it always exits 0 and says nothing — `SessionEnd` hooks have no
decision control, so output would be discarded anyway.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audiochat  # noqa: E402

# Documented `reason` values, split by whether the session is actually over.
# Verified against the hooks reference, 2026-07-26.
ENDING_REASONS = frozenset(
    {
        "logout",  # the user signed out
        "prompt_input_exit",  # input exhausted in non-interactive mode
        "other",  # documented as "any other termination"
    }
)

CONTINUING_REASONS = frozenset(
    {
        "clear",  # /clear — same session id, same terminal, still live
        "resume",  # /resume — likewise
        "bypass_permissions_disabled",  # a permissions-mode change, not an ending
    }
)


def main() -> int:
    hook = _read_hook_input()
    claude_session_id = str(hook.get("session_id") or "")
    if not claude_session_id:
        return 0

    if not audiochat.load_marker(claude_session_id):
        _debug("session not registered; nothing to end")
        return 0

    reason = str(hook.get("reason") or "")
    if not is_ending(reason):
        _debug(f"reason={reason!r} is not an ending; leaving the registration alone")
        return 0

    outcome = audiochat.post_session_end(claude_session_id)
    # Local state last, and only for a real ending: the marker is what the Stop hook
    # reads, and a marker for a session that no longer exists can never fire again.
    audiochat.marker_path(claude_session_id).unlink(missing_ok=True)
    _debug(f"reason={reason!r} ended the session -> {outcome}")
    return 0


def is_ending(reason: str) -> bool:
    """Allow-list. Unknown reasons — including an absent one — are not endings."""
    return reason in ENDING_REASONS


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
    if os.environ.get("AUDIOCHATTY_DEBUG"):
        print(f"[audiochatty] {message}", file=sys.stderr)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        _debug(f"unhandled: {exc}")
        sys.exit(0)
