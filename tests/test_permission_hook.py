"""The blocking hook.

Everything else in this plugin is tested for *not* making the terminal wait. This one is
tested for the opposite, and for the thing that makes the opposite safe.

**The centre of this file is that every failure falls through.** Hold expired, backend
unreachable, session not bound, answer unparseable — each must produce exit 0 and an
*empty stdout*, because empty stdout is what makes Claude Code show its own dialog. There
is no third outcome available to a hook that has failed: printing `deny` would silently
block a tool call because somebody stepped away, and printing `allow` would approve a
destructive command nobody heard. Both are worse than asking at the keyboard. Every test
in `TestFallsThrough` is a variation on the same assertion for that reason.

The hold is driven down to fractions of a second by `AUDIOCHATTY_APPROVAL_HOLD` and
`AUDIOCHATTY_APPROVAL_POLL` — the tunables exist for the product, and the suite is the
first thing that needs them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import permission_hook  # noqa: E402
from test_hooks import UNROUTABLE, HookTestCase  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
PERMISSION = SCRIPTS / "permission_hook.py"

QUESTION_ID = "55555555-5555-5555-5555-555555555555"


class PermissionTestCase(HookTestCase):
    """`HookTestCase` plus the two things every test here needs: a payload shaped like a
    real `PermissionRequest`, and a way to run the hook with a hold short enough to sit
    inside a test suite."""

    def permission_payload(self, **overrides) -> dict:
        """A recorded `PermissionRequest` input. Note what is *not* in it: `tool_use_id`.
        It is absent from the live payload despite the published field list, so the fixture
        matches what actually arrives and `prompt_id` is the correlation key.
        """
        payload = {
            "session_id": "sess-1",
            "prompt_id": "550e8400-e29b-41d4-a716-446655440000",
            "cwd": "/Users/mike/repos/audiochat",
            "permission_mode": "default",
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf build/", "description": "Clear the build dir"},
            "permission_suggestions": ["Bash(rm:*)"],
        }
        payload.update(overrides)
        return payload

    def run_permission(self, payload=None, *, hold="2", poll="0.05", backend=None,
                       timeout=60, env_extra=None):
        env = {"AUDIOCHATTY_APPROVAL_HOLD": hold, "AUDIOCHATTY_APPROVAL_POLL": poll}
        env.update(env_extra or {})
        return self.run_hook(
            PERMISSION,
            payload if payload is not None else self.permission_payload(),
            backend=backend,
            timeout=timeout,
            env_extra=env,
        )

    def answer_with(self, option_id: str, after_polls: int = 1) -> None:
        """Queue `after_polls` pending replies, then the answer. The hook sees the question
        outstanding for a moment and then answered, which is the real sequence."""
        for _ in range(after_polls):
            self.backend.reply("/agent/question/:id", 200,
                               {"question_id": QUESTION_ID, "status": "pending",
                                "answer_option_id": None})
        self.backend.reply("/agent/question/:id", 200,
                           {"question_id": QUESTION_ID, "status": "answered",
                            "answer_option_id": option_id})

    def decision_from(self, result) -> dict:
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.strip(), f"expected a decision, got nothing: {result.stderr}")
        emitted = json.loads(result.stdout)
        specific = emitted["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PermissionRequest")
        return specific["decision"]


# -- the decision actually reaching Claude Code ------------------------------------------


class TestDecides(PermissionTestCase):
    def test_a_spoken_allow_approves_the_call(self):
        """The whole feature: a `Bash` prompt that never rendered, decided from a phone."""
        self.register("sess-1")
        self.answer_with("allow")

        result = self.run_permission()

        self.assertEqual(self.decision_from(result), {"behavior": "allow"})

    def test_a_spoken_deny_blocks_the_call_and_says_why(self):
        """The message matters: without it the model reads a bare denial as a malfunction
        and retries the same call."""
        self.register("sess-1")
        self.answer_with("deny")

        decision = self.decision_from(self.run_permission())

        self.assertEqual(decision["behavior"], "deny")
        self.assertIn("voice", decision["message"].lower())

    def test_the_question_carries_the_tool_and_a_readable_summary(self):
        self.register("sess-1")
        self.answer_with("allow")

        self.run_permission()

        posted = self.backend.last_request("/agent/question")["body"]
        self.assertEqual(posted["kind"], "permission")
        self.assertEqual(posted["tool_name"], "Bash")
        self.assertEqual(posted["claude_session_id"], "sess-1")
        # `prompt_id`, not `tool_use_id`.
        self.assertEqual(posted["prompt_id"], "550e8400-e29b-41d4-a716-446655440000")
        self.assertIn("rm -rf build/", posted["prompt_text"])
        self.assertIn("Clear the build dir", posted["prompt_text"])
        self.assertEqual([o["id"] for o in posted["options"]], ["allow", "deny"])

    def test_it_polls_until_the_answer_arrives(self):
        self.register("sess-1")
        self.answer_with("allow", after_polls=3)

        self.decision_from(self.run_permission())

        self.assertEqual(len(self.backend.requests_to("/agent/question/:id")), 4)

    def test_an_answered_question_is_not_also_expired(self):
        """`/expire` is the give-up call. Sending it after a decision would mark an
        answered question as abandoned in the inbox."""
        self.register("sess-1")
        self.answer_with("allow")

        self.run_permission()

        self.assertEqual(self.backend.requests_to("/agent/question/:id/expire"), [])

    def test_a_multi_choice_question_is_answered_without_the_picker(self):
        """The headline, end to end: no picker, and the model proceeds as though the
        option had been chosen at the keyboard."""
        self.register("sess-1")
        self.answer_with("opt-1")

        result = self.run_permission(
            self.permission_payload(tool_name="AskUserQuestion", tool_input=CHOICE_INPUT)
        )

        decision = self.decision_from(result)
        self.assertEqual(decision["behavior"], "allow")
        self.assertEqual(
            decision["updatedInput"]["answers"], {"Which store for the job queue?": "SQLite"}
        )
        self.assertEqual(self.backend.last_request("/agent/question")["body"]["kind"], "choice")

    def test_a_multi_question_payload_is_raised_one_at_a_time(self):
        self.register("sess-1")
        self.answer_with("opt-0")
        self.answer_with("opt-1")

        result = self.run_permission(
            self.permission_payload(
                tool_name="AskUserQuestion",
                tool_input={
                    "questions": [
                        CHOICE_INPUT["questions"][0],
                        {"question": "Deploy today?",
                         "options": [{"label": "Yes"}, {"label": "No"}]},
                    ]
                },
            )
        )

        self.assertEqual(len(self.backend.requests_to("/agent/question")), 2)
        self.assertEqual(
            self.decision_from(result)["updatedInput"]["answers"],
            {"Which store for the job queue?": "Postgres", "Deploy today?": "No"},
        )

    def test_half_a_multi_question_payload_is_no_answer_at_all(self):
        """Answer all of them or fall through, never half. The first answer is thrown away
        rather than applied on its own."""
        self.register("sess-1")
        self.answer_with("opt-0")  # the first question comes back; the second never does

        result = self.run_permission(
            self.permission_payload(
                tool_name="AskUserQuestion",
                tool_input={
                    "questions": [
                        CHOICE_INPUT["questions"][0],
                        {"question": "Deploy today?",
                         "options": [{"label": "Yes"}, {"label": "No"}]},
                    ]
                },
            ),
            hold="1",
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_a_plan_is_approved_by_voice_without_reading_it_out(self):
        self.register("sess-1")
        self.answer_with("approve")

        result = self.run_permission(
            self.permission_payload(
                tool_name="ExitPlanMode",
                tool_input={"plan": "# Rewrite the token route\n\n" + ("- a step\n" * 400)},
            )
        )

        self.assertEqual(self.decision_from(result), {"behavior": "allow"})
        prompt = self.backend.last_request("/agent/question")["body"]["prompt_text"]
        self.assertIn("Rewrite the token route", prompt)
        self.assertLess(len(prompt), permission_hook.MAX_SUMMARY_CHARS + 40)


# -- every failure falls through ---------------------------------------------------------


class TestFallsThrough(PermissionTestCase):
    def assert_fell_through(self, result) -> None:
        """Exit 0 and nothing on stdout. That combination is what makes Claude Code show
        its own dialog — see the module docstring for why there is no third option."""
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)

    def test_an_unregistered_session_is_never_frozen(self):
        """Permission prompts happen in every terminal on the machine. Only the ones
        connected to audiochatty may be held, and this is the check that decides."""
        started = time.monotonic()
        result = self.run_permission(hold="30")
        elapsed = time.monotonic() - started

        self.assert_fell_through(result)
        self.assertEqual(self.backend.requests_to("/agent/question"), [])
        self.assertLess(elapsed, 3.0, "an unregistered session must not pay the hold")

    def test_an_unpaired_machine_falls_through(self):
        self.register("sess-1")
        (self.home / "credentials.json").unlink()

        self.assert_fell_through(self.run_permission())
        self.assertEqual(self.backend.requests_to("/agent/question"), [])

    def test_an_unreachable_backend_falls_through(self):
        """And quickly. The hold is for a decision that might be coming; a backend that is
        not there has no decision coming."""
        self.register("sess-1")

        started = time.monotonic()
        result = self.run_permission(hold="30", backend=UNROUTABLE)
        elapsed = time.monotonic() - started

        self.assert_fell_through(result)
        self.assertLess(elapsed, 8.0, f"held {elapsed:.1f}s against a dead backend")

    def test_a_backend_that_dies_mid_hold_falls_through(self):
        """The question was raised, then the network went. Three failed polls in a row is
        the backend being gone rather than a hiccup on hotel wifi."""
        self.register("sess-1")
        for _ in range(6):
            self.backend.reply("/agent/question/:id", 500, {"error": "boom"})

        result = self.run_permission(hold="30")

        self.assert_fell_through(result)
        polls = len(self.backend.requests_to("/agent/question/:id"))
        self.assertEqual(polls, permission_hook.MAX_CONSECUTIVE_POLL_FAILURES)

    def test_a_rejected_question_falls_through(self):
        """A 404 is the ordinary answer for a session the user disconnected — the terminal
        must ask at the keyboard, not freeze waiting for a phone that will never ring."""
        self.register("sess-1")
        self.backend.reply("/agent/question", 404, {"error": "No active session"})

        result = self.run_permission(hold="30")

        self.assert_fell_through(result)
        self.assertEqual(self.backend.requests_to("/agent/question/:id"), [])

    def test_an_expired_hold_falls_through_and_stops_offering_the_question(self):
        """Nobody called in. An accepted limitation, and the one thing the hook still
        owes the inbox on its way out."""
        self.register("sess-1")

        result = self.run_permission(hold="0.4", poll="0.05")

        self.assert_fell_through(result)
        self.assertEqual(len(self.backend.requests_to("/agent/question/:id/expire")), 1)

    def test_an_unparseable_answer_falls_through(self):
        """From the other end: an option id nobody offered is not interpreted, not
        guessed at, and above all not treated as consent."""
        self.register("sess-1")
        for bogus in ("yes", "ALLOW", "", None, 7, {"behavior": "allow"}):
            with self.subTest(answer=bogus):
                self.backend.reply("/agent/question/:id", 200,
                                   {"question_id": QUESTION_ID, "status": "answered",
                                    "answer_option_id": bogus})
                self.assert_fell_through(self.run_permission())

    def test_a_question_expired_elsewhere_falls_through(self):
        self.register("sess-1")
        self.backend.reply("/agent/question/:id", 200,
                           {"question_id": QUESTION_ID, "status": "expired",
                            "answer_option_id": None})

        self.assert_fell_through(self.run_permission(hold="30"))

    def test_a_question_that_vanished_falls_through(self):
        self.register("sess-1")
        self.backend.reply("/agent/question/:id", 404, {"error": "No such question"})

        self.assert_fell_through(self.run_permission(hold="30"))

    def test_a_tool_call_with_no_name_falls_through(self):
        self.register("sess-1")

        self.assert_fell_through(self.run_permission(self.permission_payload(tool_name="")))
        self.assertEqual(self.backend.requests_to("/agent/question"), [])

    def test_garbage_on_stdin_is_survivable(self):
        self.register("sess-1")
        for raw in ("", "   ", "not json", "[]", "null"):
            with self.subTest(stdin=raw):
                result = subprocess.run(
                    [sys.executable, str(PERMISSION)],
                    input=raw,
                    env={**os.environ, "AUDIOCHATTY_HOME": str(self.home),
                         "AUDIOCHATTY_BACKEND_URL": self.backend.url,
                         "AUDIOCHATTY_APPROVAL_HOLD": "1"},
                    capture_output=True, text=True, timeout=30,
                )
                self.assert_fell_through(result)

    def test_an_open_breaker_falls_through_without_a_call(self):
        """A backend that was down a moment ago is not worth freezing a terminal to
        rediscover."""
        self.register("sess-1")
        (self.home / "state.json").write_text(
            json.dumps({"backend_down_until": time.time() + 60})
        )

        started = time.monotonic()
        result = self.run_permission(hold="30")
        elapsed = time.monotonic() - started

        self.assert_fell_through(result)
        self.assertEqual(self.backend.requests_to("/agent/question"), [])
        self.assertLess(elapsed, 2.0)


# -- shaping the question, without a network ---------------------------------------------


class TestPromptText(unittest.TestCase):
    """`prompt_text` is what a person hears before deciding whether a command runs, so it
    is worth pinning per tool rather than trusting a generic dump."""

    def test_bash_reads_its_description_then_its_command(self):
        text = permission_hook.prompt_text(
            "Bash", {"command": "git push --force", "description": "Force-push the branch"}
        )
        self.assertEqual(text, "Bash: Force-push the branch — git push --force")

    def test_bash_with_no_description_is_just_the_command(self):
        self.assertEqual(
            permission_hook.prompt_text("Bash", {"command": "git status"}),
            "Bash: git status",
        )

    def test_a_file_tool_reads_its_path(self):
        self.assertEqual(
            permission_hook.prompt_text("Edit", {"file_path": "/repo/app.py",
                                                 "old_string": "a", "new_string": "b"}),
            "Edit: /repo/app.py",
        )

    def test_an_unknown_tool_still_produces_something_answerable(self):
        text = permission_hook.prompt_text("mcp__thing__do", {"target": "prod", "force": True})
        self.assertTrue(text.startswith("mcp__thing__do: "))
        self.assertIn("prod", text)

    def test_a_tool_with_no_usable_input_is_just_its_name(self):
        for tool_input in ({}, None, "a string", {"nested": {"deep": 1}}):
            with self.subTest(tool_input=tool_input):
                self.assertEqual(permission_hook.prompt_text("Thing", tool_input), "Thing")

    def test_a_long_command_is_capped_for_the_ear_not_the_row(self):
        text = permission_hook.prompt_text("Bash", {"command": "echo " + "x" * 5_000})
        self.assertLess(len(text), permission_hook.MAX_SUMMARY_CHARS + 40)
        self.assertTrue(text.endswith("…"))

    def test_newlines_are_flattened(self):
        """A heredoc read aloud with its line breaks intact is unlistenable."""
        text = permission_hook.prompt_text("Bash", {"command": "line one\n\nline two"})
        self.assertEqual(text, "Bash: line one line two")


class TestDecide(unittest.TestCase):
    def decide(self, hook: dict, option_id: str):
        """Shape one ask the way `build_asks` would, then answer it."""
        asks = permission_hook.build_asks(hook)
        self.assertTrue(asks, "build_asks refused this payload")
        return permission_hook.decide(hook, [(asks[0], option_id)])

    def test_the_two_permission_ids_map_to_the_two_behaviours(self):
        hook = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
        self.assertEqual(self.decide(hook, "allow")["behavior"], "allow")
        self.assertEqual(self.decide(hook, "deny")["behavior"], "deny")

    def test_anything_else_is_no_decision_at_all(self):
        hook = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
        for option_id in ("Allow", "yes", "", "approve", "allow "):
            with self.subTest(option_id=option_id):
                self.assertIsNone(self.decide(hook, option_id))

    def test_a_plan_uses_its_own_vocabulary_for_the_same_behaviours(self):
        hook = {"tool_name": "ExitPlanMode", "tool_input": {"plan": "# Do the thing"}}
        self.assertEqual(self.decide(hook, "approve")["behavior"], "allow")
        self.assertEqual(self.decide(hook, "reject")["behavior"], "deny")
        # And the permission vocabulary is not silently accepted in its place.
        self.assertIsNone(self.decide(hook, "allow"))


# -- multi-choice and plan approvals -----------------------------------------------------


CHOICE_INPUT = {
    "questions": [
        {
            "question": "Which store for the job queue?",
            "header": "Job queue",
            "multiSelect": False,
            "options": [
                {"label": "Postgres", "description": "One more service to run"},
                {"label": "SQLite", "description": "No service, no concurrency"},
                {"label": "Redis"},
            ],
        }
    ]
}


class TestChoiceShaping(unittest.TestCase):
    def asks_for(self, tool_input, tool_name="AskUserQuestion"):
        return permission_hook.build_asks({"tool_name": tool_name, "tool_input": tool_input})

    def test_the_real_options_are_sent_not_allow_and_deny(self):
        asks = self.asks_for(CHOICE_INPUT)
        self.assertEqual(len(asks), 1)
        body = asks[0]["body"]
        self.assertEqual(body["kind"], "choice")
        self.assertEqual([o["id"] for o in body["options"]], ["opt-0", "opt-1", "opt-2"])
        self.assertEqual([o["label"] for o in body["options"]], ["Postgres", "SQLite", "Redis"])
        self.assertEqual(body["options"][0]["description"], "One more service to run")
        # The header leads: spoken, it is what tells you which question you are on.
        self.assertEqual(body["prompt_text"], "Job queue: Which store for the job queue?")

    def test_each_question_becomes_its_own_ask(self):
        asks = self.asks_for(
            {
                "questions": [
                    CHOICE_INPUT["questions"][0],
                    {"question": "Deploy today?", "options": [{"label": "Yes"}, {"label": "No"}]},
                ]
            }
        )
        self.assertEqual(len(asks), 2)
        self.assertEqual(asks[1]["question"], "Deploy today?")

    def test_a_multiselect_question_is_not_attempted(self):
        """An answer is one option id and there is no way to say "these two" in that
        vocabulary. Guessing at a single pick would answer a different question."""
        payload = json.loads(json.dumps(CHOICE_INPUT))
        payload["questions"][0]["multiSelect"] = True
        self.assertIsNone(self.asks_for(payload))

    def test_surprising_payloads_go_to_the_keyboard(self):
        for tool_input in (
            {},
            {"questions": []},
            {"questions": "not a list"},
            {"questions": [{"question": "One option?", "options": [{"label": "Only"}]}]},
            {"questions": [{"question": "", "options": [{"label": "A"}, {"label": "B"}]}]},
            {"questions": [{"question": f"Q{n}", "options": [{"label": "A"}, {"label": "B"}]}
                           for n in range(5)]},
        ):
            with self.subTest(tool_input=tool_input):
                self.assertIsNone(self.asks_for(tool_input))


class TestChoiceDecision(unittest.TestCase):
    def test_a_choice_allows_the_call_with_the_answer_written_in(self):
        """The reason the picker never renders."""
        hook = {"tool_name": "AskUserQuestion", "tool_input": CHOICE_INPUT}
        asks = permission_hook.build_asks(hook)

        decision = permission_hook.decide(hook, [(asks[0], "opt-1")])

        self.assertEqual(decision["behavior"], "allow")
        self.assertEqual(
            decision["updatedInput"]["answers"],
            {"Which store for the job queue?": "SQLite"},
        )
        # The whole input, not `answers` alone: an input that had lost its `questions`
        # would be a different call than the one that was approved.
        self.assertEqual(decision["updatedInput"]["questions"], CHOICE_INPUT["questions"])

    def test_every_question_in_a_multi_question_payload_is_answered(self):
        tool_input = {
            "questions": [
                CHOICE_INPUT["questions"][0],
                {"question": "Deploy today?", "options": [{"label": "Yes"}, {"label": "No"}]},
            ]
        }
        hook = {"tool_name": "AskUserQuestion", "tool_input": tool_input}
        asks = permission_hook.build_asks(hook)

        decision = permission_hook.decide(hook, [(asks[0], "opt-0"), (asks[1], "opt-1")])

        self.assertEqual(
            decision["updatedInput"]["answers"],
            {"Which store for the job queue?": "Postgres", "Deploy today?": "No"},
        )

    def test_an_unknown_option_id_is_no_decision(self):
        hook = {"tool_name": "AskUserQuestion", "tool_input": CHOICE_INPUT}
        asks = permission_hook.build_asks(hook)
        for option_id in ("opt-9", "SQLite", "allow", ""):
            with self.subTest(option_id=option_id):
                self.assertIsNone(permission_hook.decide(hook, [(asks[0], option_id)]))


class TestPlanSummary(unittest.TestCase):
    """`tool_input.plan` was 5,000+ characters in a real sample. What goes over the
    phone is enough to recognise *which* plan and how big it is; the plan itself is on the
    screen the person can look at."""

    def summary(self, plan: str) -> str:
        asks = permission_hook.build_asks(
            {"tool_name": "ExitPlanMode", "tool_input": {"plan": plan}}
        )
        return asks[0]["body"]["prompt_text"]

    def test_it_reads_the_headings_and_the_size_not_the_plan(self):
        plan = (
            "# Rewrite the token route\n\n"
            "Some prose that nobody needs read to them.\n\n"
            "## Phase 1 — the schema\n- add a table\n- add an index\n"
            "## Phase 2 — the endpoints\n- write them\n"
        )
        summary = self.summary(plan)

        self.assertIn("Rewrite the token route", summary)
        self.assertIn("Phase 1 — the schema", summary)
        self.assertIn("Phase 2 — the endpoints", summary)
        self.assertIn("3 steps", summary)
        self.assertNotIn("nobody needs read to them", summary)

    def test_an_enormous_plan_stays_short_enough_to_hear(self):
        summary = self.summary("# Big\n\n" + ("- a step\n" * 2_000))
        self.assertLess(len(summary), permission_hook.MAX_SUMMARY_CHARS + 40)

    def test_a_plan_with_no_headings_still_asks_something(self):
        self.assertIn("Approve the plan", self.summary("just do the thing"))

    def test_an_empty_plan_still_asks_something(self):
        self.assertIn("approve the plan", self.summary("").lower())

    def test_a_plan_is_offered_approve_and_reject(self):
        asks = permission_hook.build_asks(
            {"tool_name": "ExitPlanMode", "tool_input": {"plan": "# Thing"}}
        )
        self.assertEqual([o["id"] for o in asks[0]["body"]["options"]], ["approve", "reject"])
        self.assertEqual(asks[0]["body"]["kind"], "permission")


class TestTunables(unittest.TestCase):
    """A typo'd override must not silently turn the feature off — a zero hold is a hook
    that freezes nothing and answers nothing, which looks exactly like it being broken."""

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in
                       ("AUDIOCHATTY_APPROVAL_HOLD", "AUDIOCHATTY_APPROVAL_POLL")}

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_the_defaults_are_the_documented_ones(self):
        for key in self._saved:
            os.environ.pop(key, None)
        self.assertEqual(permission_hook.hold_seconds(), 600.0)
        self.assertEqual(permission_hook.poll_seconds(), 2.0)

    def test_an_override_is_honoured(self):
        os.environ["AUDIOCHATTY_APPROVAL_HOLD"] = "45"
        self.assertEqual(permission_hook.hold_seconds(), 45.0)

    def test_a_nonsense_override_falls_back(self):
        for raw in ("", "soon", "0", "-30", "abc"):
            with self.subTest(raw=raw):
                os.environ["AUDIOCHATTY_APPROVAL_HOLD"] = raw
                self.assertEqual(permission_hook.hold_seconds(), 600.0)


if __name__ == "__main__":
    unittest.main()
