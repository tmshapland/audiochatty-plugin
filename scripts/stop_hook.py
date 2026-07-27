#!/usr/bin/env python3
"""The `Stop` hook — one completed turn becomes one AudioChat message.

`coding_agent_build_plan.md` Phase 5 · `coding_agent_summary_plan.md` §4.1, §8.

Two rules govern everything in this file, and both come from the plan's sharp edges:

1. **Stop hooks are global.** This runs at the end of every turn in every Claude Code
   session on the machine, registered or not. So the very first thing it does — before
   reading credentials, before touching the network, before parsing anything — is check
   for this session's marker file and exit. Without that check, all fifteen terminals
   open that day start talking.

2. **It must never make the terminal wait.** Every failure is swallowed, the timeout is
   short, and a backend that failed once is skipped entirely for the next minute (the
   circuit breaker in `audiochat.py`). This hook has no way to report a problem that
   would not also be a problem: the user is mid-conversation.

It therefore always exits 0, always silently. `AUDIOCHAT_DEBUG=1` puts a line on stderr,
which is where anyone debugging this should start.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audiochat  # noqa: E402

# How much of the transcript to read looking for this turn's tool calls. A long session's
# `.jsonl` runs to megabytes and this runs on every turn, so the file is read from the
# end and only this far back. A turn longer than this loses the earliest of its tool
# calls, which costs a detail in one blurb.
MAX_TRANSCRIPT_TAIL = 1_000_000
MAX_TRANSCRIPT_LINES = 3_000
# Tool uses in one turn. Well past anything real; here so a malformed transcript cannot
# turn into an enormous request body.
MAX_TOOL_USES = 500


def main() -> int:
    hook = _read_hook_input()
    claude_session_id = str(hook.get("session_id") or "")
    if not claude_session_id:
        return 0

    # Rule 1. Cheapest possible answer to "is this session registered", and the reason an
    # unregistered terminal costs a single `stat` per turn.
    if not audiochat.load_marker(claude_session_id):
        _debug("session not registered; nothing to send")
        return 0

    payload = build_payload(hook)
    if not payload["last_assistant_message"] and not payload["tool_calls"]:
        # The backend rejects an empty turn with a 400 rather than queueing a job that can
        # only fail. Don't make it say so.
        _debug("turn is empty; not sending")
        return 0

    outcome = audiochat.post_turn(claude_session_id, payload)
    _debug(f"post_turn -> {outcome}")
    return 0


def build_payload(hook: dict) -> dict:
    """The four keys `POST /agent/turn` stores, and nothing else.

    The backend normalises and caps all of this again on arrival — it treats this hook as
    untrusted input, which it should. Sending the canonical shape anyway keeps the request
    small and means the contract is legible from either side.
    """
    return {
        # Documented as the field to use *instead of* re-reading the transcript for the
        # current turn's assistant text, which may still be lagging on disk here.
        "last_assistant_message": str(hook.get("last_assistant_message") or ""),
        "tool_calls": tool_calls_from_transcript(hook.get("transcript_path")),
        "stop_reason": str(hook.get("stop_reason") or ""),
        "cwd": str(hook.get("cwd") or ""),
    }


def tool_calls_from_transcript(transcript_path) -> list[str]:
    """What this turn actually did, as a raw list of tool names.

    Returned raw and unsorted — `/agent/turn` accepts a list of names and counts them
    itself, so there is exactly one place that decides the canonical form and it is not
    a released plugin binary.

    **Best effort, by design.** The turn's text is what the blurb is written from; the
    tool list is what turns "I finished the refactor" into "fourteen edits and the tests
    passed". Every failure here — a missing file, a truncated line, a shape this doesn't
    recognise — returns what was found so far rather than raising, because a turn is
    worth delivering without its tool list.

    The turn boundary is the last genuine user message: walking backwards from the end,
    collect `tool_use` blocks until a user row appears that is a real prompt rather than
    a carrier for tool results.
    """
    if not transcript_path:
        return []
    try:
        rows = _tail_rows(str(transcript_path))
    except OSError:
        return []

    names: list[str] = []
    for row in reversed(rows):
        # Subagent traffic. It belongs to the subagent's own turn structure and would
        # make the counts read as though the main session had done the work.
        if row.get("isSidechain"):
            continue

        kind = row.get("type")
        if kind == "assistant":
            for block in _content_blocks(row):
                if block.get("type") == "tool_use" and block.get("name"):
                    names.append(str(block["name"])[:64])
                    if len(names) >= MAX_TOOL_USES:
                        return list(reversed(names))
        elif kind == "user" and _is_user_prompt(row):
            break

    return list(reversed(names))


def _content_blocks(row: dict) -> list:
    message = row.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _is_user_prompt(row: dict) -> bool:
    """True for something a person typed; False for the transcript's other user-role rows.

    A tool result is recorded as a user-role message, and so are the meta rows Claude Code
    injects. Neither starts a turn. Getting this wrong in the safe direction — deciding
    something is *not* a prompt — over-collects tool calls from the previous turn; getting
    it wrong the other way silently drops this turn's.
    """
    if row.get("isMeta"):
        return False
    message = row.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content
        )
    return False


def _tail_rows(path: str) -> list[dict]:
    """The last `MAX_TRANSCRIPT_LINES` parseable JSON objects, reading at most
    `MAX_TRANSCRIPT_TAIL` bytes off the end of the file."""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - MAX_TRANSCRIPT_TAIL)
        handle.seek(start)
        raw = handle.read()

    lines = raw.split(b"\n")
    if start > 0 and lines:
        # The seek almost certainly landed mid-line; that fragment is not valid JSON.
        lines = lines[1:]

    rows = []
    for line in lines[-MAX_TRANSCRIPT_LINES:]:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


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
    if os.environ.get("AUDIOCHAT_DEBUG"):
        print(f"[audiochat] {message}", file=sys.stderr)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        # Rule 2, last line of defence. A traceback out of a Stop hook is a traceback in
        # somebody's terminal, and nothing this file does is worth that.
        _debug(f"unhandled: {exc}")
        sys.exit(0)
