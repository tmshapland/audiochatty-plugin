#!/usr/bin/env python3
"""The `PermissionRequest` hook — Claude Code stops to ask, and audiochatty answers.

`voice_approval_plan.md` Phases 3 and 5.

Every other hook in this plugin reports something and gets out of the way. This one does
the opposite and it is the only one that does: it **blocks**, for up to ten minutes, while
the person who owns the terminal decides by voice. Claude Code lets a `PermissionRequest`
hook return the approve/deny decision itself and never show the dialog (D1), so what a
frozen terminal actually means here is "audiochatty is answering *in place of* the dialog",
not "audiochatty is waiting to type into it". Nothing else can answer the question while
this hook holds it, which is what makes a stale answer structurally impossible.

**One event covers all three cases** (D6). Phase 0 found `PermissionRequest` fires not just
for tools needing approval but for `AskUserQuestion` and `ExitPlanMode` too, with the full
question list and the full plan text in `tool_input`. So there is one hook, one table and
one hold, and the only thing that varies is the shape of what gets asked and how the answer
gets back:

    Bash, Edit, an MCP tool…   allow / deny        → `behavior`
    AskUserQuestion            the real options    → `allow` + `updatedInput.answers` (D9)
    ExitPlanMode               approve / reject    → `behavior`, on a *summary* of the plan

The `AskUserQuestion` route is the odd one and is worth knowing about before reading
`decide`: a multi-choice question is answered by **allowing the tool call with the answer
written into its input**, so the picker never renders and the model proceeds as though the
option had been chosen at the keyboard. Proven live in Phase 0.5.

Three rules govern the file, and the middle one is the one to read twice:

1. **Unregistered sessions cost one `stat`.** Like `stop_hook.py`, the first thing this
   does is look for the session's marker file. Permission prompts happen in every terminal
   on the machine; only the ones connected to audiochatty may be frozen by this.

2. **Every failure falls through. Never auto-deny, never auto-allow** (D3). Hold expired,
   backend unreachable, session not bound, answer unparseable — all of them exit 0 having
   printed *nothing*, and Claude Code shows its own dialog exactly as it would today.
   Silence is not approval: the docs are explicit that exit 0 with no output continues
   through the normal permission flow. A voice channel that silently denied a tool call
   because you stepped away would be worse than one that just asked at the keyboard, and
   one that silently allowed would be dangerous. This is the single most important safety
   property in the plan; the tests in `tests/test_permission_hook.py` exist for it.

3. **A decision is an option *id*, never a sentence** (D4). The voice agent picks from the
   list this hook sent and writes back one of those ids. An id this hook does not
   recognise is not interpreted, guessed at, or pattern-matched — it is rule 2.

The correlation key is `prompt_id`, not `tool_use_id`: Phase 0 found `PermissionRequest`
does not actually send `tool_use_id` despite the published field list saying it does.

`AUDIOCHATTY_DEBUG=1` puts the reasoning on stderr, which is where anyone wondering why
their terminal did or did not freeze should start.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audiochat  # noqa: E402

# How long the terminal is allowed to sit frozen. 👤's call (D2), and 600s because that is
# the documented default timeout for a `PermissionRequest` hook — the registration in
# `hooks/hooks.json` sets a longer one so the hold, not the harness, is what ends the wait.
DEFAULT_HOLD_SECONDS = 600.0
# Between polls. Two seconds is a compromise: the person answering has already spoken by
# the time this fires, so the felt latency is the round trip, and a 300-poll hold is
# nothing next to what the frozen terminal is already costing.
DEFAULT_POLL_SECONDS = 2.0

# Consecutive failed polls before giving up on the whole hold. One is a hiccup on hotel
# wifi and should not cost somebody their voice approval; three in a row is the backend
# being gone, and continuing to hold a terminal against a backend that is gone is just a
# slower way of falling through.
MAX_CONSECUTIVE_POLL_FAILURES = 3

# The summary that gets read aloud. Far below the backend's own 2,000-character cap on
# purpose — this is the sentence somebody hears in their kitchen before deciding whether a
# command runs, and the backend's cap is about storage while this one is about attention.
MAX_SUMMARY_CHARS = 400

# What a permission prompt offers. Ids are what come back (D4); labels are what get read
# out, and they are two words apart by ear rather than two letters.
ALLOW = "allow"
DENY = "deny"
PERMISSION_OPTIONS = [
    {"id": ALLOW, "label": "Allow it"},
    {"id": DENY, "label": "Don't allow it"},
]

# The plan-approval pair. Same two behaviours underneath, different words, because
# "allow it" is a strange thing to say about a plan and the read-out is the product.
APPROVE = "approve"
REJECT = "reject"
PLAN_OPTIONS = [
    {"id": APPROVE, "label": "Approve the plan"},
    {"id": REJECT, "label": "Don't approve it yet"},
]

# `AskUserQuestion`'s schema allows up to four questions and four options each. These are
# that, restated — a payload past them is not answered by voice at all (see `build_asks`).
MAX_CHOICE_QUESTIONS = 4
MAX_CHOICE_OPTIONS = 8

# Of the plan text, read aloud. `tool_input.plan` was 5,000+ characters in Phase 0's sample
# — far too long to hear — so what goes out is a *summary* and the plan itself stays on the
# screen the person can look at.
MAX_PLAN_HEADINGS = 5


def main() -> int:
    hook = _read_hook_input()
    claude_session_id = str(hook.get("session_id") or "")
    if not claude_session_id:
        return 0

    # Rule 1. Cheapest possible answer to "may this session be frozen".
    if not audiochat.load_marker(claude_session_id):
        _debug("session not registered; leaving the dialog alone")
        return 0

    asks = build_asks(hook)
    if not asks:
        _debug("could not shape a question from this tool call")
        return 0

    answers = collect_answers(claude_session_id, asks)
    if answers is None:
        return 0

    decision = decide(hook, answers)
    if not decision:
        # Rule 3: an id we did not offer. Nothing here tries to work out what was meant.
        _debug("the answer does not map to a decision; falling through")
        return 0

    _debug(f"decided {decision['behavior']}")
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": decision,
                }
            }
        )
    )
    return 0


# -- shaping the question --------------------------------------------------------------


def build_asks(hook: dict) -> list[dict] | None:
    """The `PermissionRequest` payload → the questions to raise, in the order to raise them.

    Each ask is `{"body": <POST /agent/question body>, "question": <the AskUserQuestion's
    own text, or "">}`. Almost always a list of one; `AskUserQuestion` is the exception,
    because it may carry several questions and each needs its own answer.

    Returns None when there is nothing worth asking about — no tool name, or a payload
    this hook will not attempt (below). That is rule 2's cheapest branch: the dialog
    renders and the person answers at the keyboard, which is what would have happened
    anyway.
    """
    tool_name = str(hook.get("tool_name") or "").strip()[:64]
    if not tool_name:
        return None

    if tool_name == "AskUserQuestion":
        return _choice_asks(hook, tool_name)

    if tool_name == "ExitPlanMode":
        return [_ask(hook, tool_name, "permission", _plan_prompt(hook.get("tool_input")),
                     PLAN_OPTIONS)]

    return [_ask(hook, tool_name, "permission",
                 prompt_text(tool_name, hook.get("tool_input")), PERMISSION_OPTIONS)]


def _ask(hook: dict, tool_name: str, kind: str, prompt: str, options: list[dict],
         question: str = "") -> dict:
    return {
        "question": question,
        "body": {
            "kind": kind,
            "tool_name": tool_name,
            # Present in the published field list, absent from the live payload (Phase 0).
            # Sent anyway, best-effort: the day it starts arriving it is the better key.
            "tool_use_id": str(hook.get("tool_use_id") or "")[:128],
            "prompt_id": str(hook.get("prompt_id") or "")[:128],
            "prompt_text": prompt,
            "options": options,
        },
    }


def _choice_asks(hook: dict, tool_name: str) -> list[dict] | None:
    """`AskUserQuestion` → one ask per question in it.

    **Two payloads are deliberately not attempted**, and in both cases falling through is
    the honest answer rather than a limitation worked around:

    * a `multiSelect` question. An answer here is one option id (D4), and there is no
      way to say "these two" in that vocabulary. Guessing at a single pick would be
      answering a different question than the one asked.
    * more than `MAX_CHOICE_QUESTIONS`, or a question with fewer than two options. Neither
      is something `AskUserQuestion`'s own schema produces, so a payload like that is a
      surprise, and surprises go to the keyboard.

    Several questions become several asks answered *in turn* — the plan's rule is answer
    all of them or fall through, never half (Phase 5), and `collect_answers` enforces that
    by only returning once every one of these has come back.
    """
    tool_input = hook.get("tool_input")
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    if not isinstance(questions, list) or not questions:
        return None
    if len(questions) > MAX_CHOICE_QUESTIONS:
        return None

    asks: list[dict] = []
    for entry in questions:
        if not isinstance(entry, dict) or entry.get("multiSelect"):
            return None
        text = _text(entry.get("question"))
        options = _choice_options(entry.get("options"))
        if not text or len(options) < 2:
            return None

        header = _text(entry.get("header"))
        # The header is the two-word chip the picker shows. Spoken, it is what tells you
        # which of three questions you are on, so it leads.
        prompt = f"{header}: {text}" if header else text
        asks.append(_ask(hook, "AskUserQuestion", "choice", _capped(prompt), options, text))
    return asks


def _choice_options(raw) -> list[dict]:
    """`tool_input.questions[].options[]` → the list read out over the phone.

    Ids are positional (`opt-0`, `opt-1`) because `AskUserQuestion`'s options have labels
    and no ids of their own, and the label is what has to go back in `answers` — so the
    position is the only stable handle, and `decide` maps it back through this same list.
    """
    if not isinstance(raw, list):
        return []
    options = []
    for index, entry in enumerate(raw[:MAX_CHOICE_OPTIONS]):
        if not isinstance(entry, dict):
            continue
        label = _text(entry.get("label"))
        if not label:
            continue
        option = {"id": f"opt-{index}", "label": _capped(label)}
        description = _text(entry.get("description"))
        if description:
            option["description"] = _capped(description)
        options.append(option)
    return options


def _plan_prompt(tool_input) -> str:
    """`ExitPlanMode` → something short enough to hear.

    `tool_input.plan` is full markdown and ran past 5,000 characters in Phase 0's sample.
    Reading that aloud would take minutes, and the person can see the plan on the screen
    anyway — what they need over the phone is enough to recognise *which* plan and how big
    it is. So: its headings, and its size.
    """
    plan = _text(tool_input.get("plan")) if isinstance(tool_input, dict) else ""
    if not plan:
        return "ExitPlanMode: approve the plan?"

    lines = [line.strip() for line in plan.splitlines() if line.strip()]
    headings = [line.lstrip("#").strip() for line in lines if line.startswith("#")]
    steps = sum(1 for line in lines if line.startswith(("- ", "* ", "1. ")))

    lead = headings[0] if headings else lines[0]
    rest = headings[1 : MAX_PLAN_HEADINGS + 1]

    parts = [f"Approve the plan “{lead}”?"]
    if rest:
        parts.append("It covers " + ", then ".join(rest) + ".")
    size = f"{len(plan):,} characters"
    parts.append(f"{size}, {steps} steps." if steps else f"{size}.")
    return _capped(" ".join(parts))


def prompt_text(tool_name: str, tool_input) -> str:
    """What gets read out: the tool, and enough of its input to decide on.

    Per-tool rather than generic because the interesting field is in a different place for
    each one, and "Bash wants to run something" is not a question anybody can answer. The
    generic branch is the fallback for MCP tools and anything added after this was written
    — it is deliberately dull rather than absent.
    """
    summary = _summarize(tool_name, tool_input)
    return f"{tool_name}: {summary}" if summary else tool_name


def _summarize(tool_name: str, tool_input) -> str:
    if not isinstance(tool_input, dict):
        return ""

    if tool_name == "Bash":
        command = _text(tool_input.get("command"))
        description = _text(tool_input.get("description"))
        # The description first when there is one: it is a sentence written to be read,
        # and the command follows it as the detail somebody may want to hear.
        if description and command:
            return _capped(f"{description} — {command}")
        return _capped(command or description)

    for key in ("file_path", "path", "url", "notebook_path", "pattern", "command", "query"):
        value = _text(tool_input.get(key))
        if value:
            return _capped(value)

    # Anything else: the whole input, compactly, with the long values left out. A tool
    # nobody anticipated still produces something a person can say yes or no to.
    try:
        compact = json.dumps(
            {
                str(key)[:40]: value
                for key, value in list(tool_input.items())[:8]
                if not isinstance(value, (dict, list))
            },
            ensure_ascii=False,
            default=str,
        )
    except (TypeError, ValueError):
        return ""
    return _capped("" if compact == "{}" else compact)


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _capped(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= MAX_SUMMARY_CHARS:
        return text
    return text[:MAX_SUMMARY_CHARS].rstrip() + "…"


# -- holding the terminal --------------------------------------------------------------


def collect_answers(claude_session_id: str, asks: list[dict]) -> list[tuple[dict, str]] | None:
    """Raise each ask and wait for it, sharing one hold across all of them.

    Returns `[(ask, option_id), …]` with an entry for **every** ask, or None. There is no
    partial result and that is the point: Phase 5's rule for a multi-question
    `AskUserQuestion` is answer all of them or fall through, never half. A hold that runs
    out on the second of three questions throws away the first answer and shows the
    dialog, which costs one wasted answer and keeps the invariant.

    One deadline spans the whole exchange rather than one per question — the thing being
    budgeted is how long the terminal is frozen, and that does not get more acceptable
    because the payload happened to carry three questions.
    """
    deadline = time.monotonic() + hold_seconds()
    answers: list[tuple[dict, str]] = []

    for ask in asks:
        reason, question_id = audiochat.post_question(claude_session_id, ask["body"])
        _debug(f"post_question -> {reason}")
        if reason != "raised":
            return None

        option_id = wait_for_answer(question_id, deadline)
        if not option_id:
            return None
        answers.append((ask, option_id))

    return answers


def wait_for_answer(question_id: str, deadline: float) -> str:
    """Poll until somebody answers, the hold runs down, or the backend stops responding.

    Returns the chosen option id, or `""` for every one of those other outcomes — the
    caller cannot tell them apart and does not need to, because rule 2 makes them the same
    thing: say nothing and let the dialog render.

    Sleeps *before* the first poll deliberately. The question was created microseconds ago
    and cannot have been answered yet; polling immediately would only spend a round trip
    proving it.
    """
    interval = poll_seconds()
    failures = 0

    while time.monotonic() < deadline:
        time.sleep(min(interval, max(deadline - time.monotonic(), 0)))

        reason, view = audiochat.poll_question(question_id)
        if reason == "gone" or reason == "not-paired":
            _debug(f"poll -> {reason}; falling through")
            return ""
        if reason != "ok":
            failures += 1
            _debug(f"poll -> {reason} ({failures})")
            if failures >= MAX_CONSECUTIVE_POLL_FAILURES:
                return ""
            continue

        failures = 0
        status = view.get("status")
        if status == "answered":
            answer = view.get("answer_option_id")
            return answer if isinstance(answer, str) else ""
        if status != "pending":
            # Expired or otherwise closed by something else. Nothing to wait for.
            _debug(f"question is {status}; falling through")
            return ""

    # The hold ran down. Stop offering the decision to somebody's phone — the dialog is
    # about to render and the answer belongs at the keyboard now.
    _debug("hold expired")
    audiochat.expire_question(question_id)
    return ""


def hold_seconds() -> float:
    return _positive_float("AUDIOCHATTY_APPROVAL_HOLD", DEFAULT_HOLD_SECONDS)


def poll_seconds() -> float:
    return _positive_float("AUDIOCHATTY_APPROVAL_POLL", DEFAULT_POLL_SECONDS)


def _positive_float(name: str, default: float) -> float:
    """An env override, ignored unless it is a positive number.

    A zero or negative hold would mean a hook that freezes nothing and answers nothing,
    which looks identical to the feature being broken. Falling back to the default is the
    honest reading of a typo.
    """
    try:
        value = float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# -- turning an option id into a decision ----------------------------------------------


def decide(hook: dict, answers: list[tuple[dict, str]]) -> dict | None:
    """The chosen option ids → what Claude Code is told, or None to fall through.

    None is rule 3 and it is the whole reason this is a separate function: the mapping
    from id to behaviour is a lookup in the list *this hook sent*, not an interpretation of
    whatever came back. An id that is not in that list is not nearly-an-answer; it is no
    answer, and no answer means the dialog.
    """
    if not answers:
        return None

    if str(hook.get("tool_name") or "") == "AskUserQuestion":
        return _choice_decision(hook, answers)

    # Everything else — permission prompts and plan approvals alike — is one answer that
    # maps to one behaviour. Two vocabularies, because "allow it" is a strange thing to say
    # about a plan, and the read-out is the product.
    if len(answers) != 1:
        return None
    ask, option_id = answers[0]
    if option_id not in {option["id"] for option in ask["body"]["options"]}:
        return None

    if option_id in (ALLOW, APPROVE):
        return {"behavior": "allow"}
    if option_id in (DENY, REJECT):
        return {
            "behavior": "deny",
            # Shown to the model, so it reads as a decision rather than a malfunction and
            # the session does not immediately retry the same call.
            "message": "Denied by voice through audiochatty.",
        }
    return None


def _choice_decision(hook: dict, answers: list[tuple[dict, str]]) -> dict | None:
    """A multi-choice question, answered by **allowing the tool call with the answer
    written into its input** (D9).

    `AskUserQuestion`'s own schema carries an `answers` field documented as "user answers
    collected by the permission component" — an object keyed by question text, valued with
    the chosen option's label. Filling it in and allowing the call is what makes the picker
    never render: Claude Code reports it in the transcript as the question having been
    answered, and the model proceeds. Proven live in Phase 0.5.

    `updatedInput` is the whole original input with `answers` merged in, not `answers`
    alone. The field is documented as "modified tool arguments", and an input that had lost
    its `questions` would be a different call than the one that was approved.
    """
    tool_input = hook.get("tool_input")
    if not isinstance(tool_input, dict):
        return None

    filled: dict[str, str] = {}
    for ask, option_id in answers:
        label = _label_for(ask, option_id)
        question = ask.get("question")
        if not label or not question:
            return None
        filled[question] = label

    # Never half (Phase 5). Every question in the payload has to have come back.
    if len(filled) != len(answers):
        return None

    return {
        "behavior": "allow",
        "updatedInput": {**tool_input, "answers": filled},
    }


def _label_for(ask: dict, option_id: str) -> str:
    """The label belonging to an option id we ourselves sent, or `""`.

    The label rather than the id, because that is what goes into `answers` — the ids are
    positional handles this hook invented so a spoken answer could name one, and Claude
    Code has never seen them.
    """
    for option in ask["body"]["options"]:
        if option["id"] == option_id:
            return str(option.get("label") or "")
    return ""


# -- plumbing --------------------------------------------------------------------------


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
    except KeyboardInterrupt:
        # Somebody gave up on the freeze and hit ctrl-c. Fall through to the dialog, which
        # is exactly what they were asking for.
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001
        # Rule 2, last line of defence. An exception here must not become an approval, a
        # denial, or a traceback in somebody's terminal — it becomes the ordinary dialog.
        _debug(f"unhandled: {exc}")
        sys.exit(0)
