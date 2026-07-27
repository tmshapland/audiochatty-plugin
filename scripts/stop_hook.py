#!/usr/bin/env python3
"""The `Stop` hook — one completed turn becomes one audiochatty message.

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

It therefore always exits 0, always silently. `AUDIOCHATTY_DEBUG=1` puts a line on stderr,
which is where anyone debugging this should start.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audiochat  # noqa: E402

# How much of the transcript to read. A long session's `.jsonl` runs to megabytes and
# this runs on every turn, so the file is read from the end and only this far back.
# Measured against 59 real sessions in this repo: the largest single turn was 856 KB of
# raw rows, so 4 MB holds any turn anyone here has actually produced, with room over.
MAX_TRANSCRIPT_TAIL = 4_000_000
MAX_TRANSCRIPT_LINES = 20_000
# Tool uses in one turn. Well past anything real; here so a malformed transcript cannot
# turn into an enormous request body.
MAX_TOOL_USES = 500

# -- the full-transcript caps ----------------------------------------------------------
#
# The turn's whole conversation goes in the payload so `ask_context` can answer from what
# actually happened rather than from the agent's closing paragraph. These caps are not a
# size policy — the backend has its own, and Gemini's window is the real ceiling — they
# exist so one pathological row (a 40 MB `cat`, a runaway loop) cannot turn a Stop hook
# into a stalled terminal.
#
# Per block, so a single enormous tool result is trimmed rather than costing the turn.
MAX_BLOCK_CHARS = 20_000
# The projected transcript as a whole. Real turns land far under this: the largest turn
# measured projects to 215 KB, which gzips to 48 KB on the wire.
MAX_TRANSCRIPT_CHARS = 1_500_000
TRUNCATION_MARKER = " … [truncated]"


def main() -> int:
    hook = _read_hook_input()
    claude_session_id = str(hook.get("session_id") or "")
    if not claude_session_id:
        return 0

    # Rule 1. Cheapest possible answer to "is this session registered", and the reason an
    # unregistered terminal costs a single `stat` per turn.
    marker = audiochat.load_marker(claude_session_id)
    if not marker:
        _debug("session not registered; nothing to send")
        return 0

    # The turn that ran `/audiochatty-connect` is the turn that wrote that marker, and its
    # only content is the registration being reported back. The user is holding the
    # terminal it was reported in; don't also send it to them as a message.
    if audiochat.consume_skip_next_turn(claude_session_id, marker):
        _debug("registration turn; not sending")
        return 0

    payload = build_payload(hook)
    if not payload["last_assistant_message"] and not payload["tool_calls"]:
        # `transcript` is deliberately not part of this test. A turn with neither a
        # closing message nor a tool call has nothing to say however many rows it has.
        # The backend rejects an empty turn with a 400 rather than queueing a job that can
        # only fail. Don't make it say so.
        _debug("turn is empty; not sending")
        return 0

    outcome = audiochat.post_turn(claude_session_id, payload)
    _debug(f"post_turn -> {outcome}")
    return 0


def build_payload(hook: dict) -> dict:
    """The five keys `POST /agent/turn` stores, and nothing else.

    The backend normalises and caps all of this again on arrival — it treats this hook as
    untrusted input, which it should. Sending the canonical shape anyway keeps the request
    small and means the contract is legible from either side.

    The transcript is read once and the three fields derived from it are built off that
    one read: it is now megabytes rather than kilobytes, and this runs on every turn.
    """
    rows = _turn_rows(hook.get("transcript_path"))
    return {
        # Documented as the field to use *instead of* re-reading the transcript for the
        # current turn's assistant text, which may still be lagging on disk here.
        "last_assistant_message": str(hook.get("last_assistant_message") or ""),
        "tool_calls": tool_calls_from_rows(rows),
        # Everything the turn actually did — see `transcript_from_rows`. This is what
        # `ask_context` reads when the blurb isn't enough, and it is the reason a
        # follow-up can name the file that changed and say what changed in it.
        "transcript": transcript_from_rows(rows),
        "stop_reason": str(hook.get("stop_reason") or ""),
        "cwd": str(hook.get("cwd") or ""),
    }


def _turn_rows(transcript_path) -> list[dict]:
    """The transcript rows belonging to the turn that just finished.

    **Best effort, by design.** Every failure here — a missing file, a truncated line, a
    shape this doesn't recognise — returns what was found so far rather than raising,
    because a turn is worth delivering without its transcript.

    The turn boundary is the last genuine user message: walk backwards from the end until
    a user row appears that is a real prompt rather than a carrier for tool results. That
    row is included, so the record starts with what was asked — the single most useful
    thing in it, and the one thing the old four-key payload had no room for at all.
    """
    if not transcript_path:
        return []
    try:
        rows = _tail_rows(str(transcript_path))
    except OSError:
        return []

    start = 0
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        if row.get("isSidechain"):
            continue
        if row.get("type") == "user" and _is_user_prompt(row):
            start = index
            break
    return rows[start:]


def tool_calls_from_transcript(transcript_path) -> list[str]:
    """`_turn_rows` + `tool_calls_from_rows`, for a caller holding only a path.

    `build_payload` does not use this — it reads the file once and derives both fields
    off that single read — but the two-step is worth keeping addressable on its own.
    """
    return tool_calls_from_rows(_turn_rows(transcript_path))


def tool_calls_from_rows(rows: list[dict]) -> list[str]:
    """What this turn did, as a raw list of tool names.

    Returned raw and unsorted — `/agent/turn` accepts a list of names and counts them
    itself, so there is exactly one place that decides the canonical form and it is not
    a released plugin binary.

    Kept even though `transcript` now carries every one of these calls in full: the
    counts go into the *blurb*, which is written from a short prompt and read aloud, and
    making that prompt re-derive them from a megabyte of rows would be paying Gemini to
    count. Subagent traffic is excluded here — it belongs to the subagent's own turn
    structure and would make the counts read as though the main session had done the
    work — while the transcript keeps it, flagged, because it is still what happened.
    """
    names: list[str] = []
    for row in rows:
        if row.get("isSidechain") or row.get("type") != "assistant":
            continue
        for block in _content_blocks(row):
            if block.get("type") == "tool_use" and block.get("name"):
                names.append(str(block["name"])[:64])
                if len(names) >= MAX_TOOL_USES:
                    return names
    return names


def transcript_from_rows(rows: list[dict]) -> list[dict]:
    """The turn's whole conversation, as the messages that make it up.

    **What this keeps:** every user and assistant message in the turn, in order, with
    every content block — prompt text, assistant prose, thinking, each tool call with its
    full input (an `Edit`'s `old_string`/`new_string` is a complete before-and-after of
    the change), and each tool result in full. Nothing about what the turn *did* is
    dropped.

    **What it leaves behind** is per-row plumbing that no summarizer can use: `uuid`,
    `parentUuid`, `sessionId`, `version`, `gitBranch`, the timestamp and cwd repeated on
    every row, the `toolUseResult` field that duplicates the tool result already in
    `content`, and the transcript's non-message rows (`attachment`, `file-history-*`,
    `mode`, `ai-title`). Measured over 195 real turns that is 74.5% of the bytes for none
    of the meaning — the largest turn here goes 856 KB raw → 215 KB projected → 48 KB
    gzipped on the wire, which is what keeps this inside a Stop hook's latency budget.
    """
    out: list[dict] = []
    budget = MAX_TRANSCRIPT_CHARS

    for row in rows:
        if row.get("type") not in ("user", "assistant"):
            continue
        blocks = _projected_blocks(row)
        if not blocks:
            continue

        entry = {"role": _role_of(row), "content": blocks}
        # Subagent traffic is part of the turn and gets summarized with it, but a reader
        # needs to know the main session did not run these itself.
        if row.get("isSidechain"):
            entry["sidechain"] = True

        cost = len(json.dumps(entry, ensure_ascii=False))
        if cost > budget:
            out.append({"role": "system", "content": [{"type": "text", "text": TRUNCATION_MARKER}]})
            break
        budget -= cost
        out.append(entry)

    return out


def _projected_blocks(row: dict) -> list[dict]:
    """One transcript row's content blocks, normalised and capped per block."""
    message = row.get("message")
    if not isinstance(message, dict):
        return []

    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": _capped(content)}] if content.strip() else []

    blocks: list[dict] = []
    for block in _content_blocks(row):
        kind = block.get("type")
        if kind == "text" and str(block.get("text") or "").strip():
            blocks.append({"type": "text", "text": _capped(block.get("text"))})
        elif kind == "thinking" and str(block.get("thinking") or "").strip():
            blocks.append({"type": "thinking", "text": _capped(block.get("thinking"))})
        elif kind == "tool_use":
            blocks.append(
                {
                    "type": "tool_use",
                    "name": str(block.get("name") or "")[:64],
                    "input": _capped_tree(block.get("input")),
                }
            )
        elif kind == "tool_result":
            result = {"type": "tool_result", "content": _capped_tree(block.get("content"))}
            if block.get("is_error"):
                result["is_error"] = True
            blocks.append(result)
    return blocks


def _role_of(row: dict) -> str:
    message = row.get("message")
    if isinstance(message, dict) and message.get("role"):
        return str(message["role"])[:32]
    return str(row.get("type") or "user")[:32]


def _capped(text) -> str:
    text = str(text or "")
    if len(text) <= MAX_BLOCK_CHARS:
        return text
    return text[:MAX_BLOCK_CHARS] + TRUNCATION_MARKER


def _capped_tree(value, _depth: int = 0):
    """A tool input or result, with every string in it capped.

    Tool inputs are arbitrary JSON and tool results are sometimes a list of blocks, so
    the cap has to reach inside rather than stringify the whole thing — an `Edit`'s
    `new_string` and its `file_path` are both strings in the same dict and only one of
    them is ever long.
    """
    if isinstance(value, str):
        return _capped(value)
    if _depth >= 8:
        return _capped(json.dumps(value, ensure_ascii=False, default=str))
    if isinstance(value, dict):
        return {str(k)[:128]: _capped_tree(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_capped_tree(v, _depth + 1) for v in value[:200]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _capped(str(value))


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
    if os.environ.get("AUDIOCHATTY_DEBUG"):
        print(f"[audiochatty] {message}", file=sys.stderr)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        # Rule 2, last line of defence. A traceback out of a Stop hook is a traceback in
        # somebody's terminal, and nothing this file does is worth that.
        _debug(f"unhandled: {exc}")
        sys.exit(0)
