# Voice-approved permissions and questions

## What this is

Today a Claude Code session running under `audiochatty run` can *report* what it did — the
`Stop` hook posts each finished turn, you hear it read out, and you can speak an instruction
back. But when Claude stops to **ask** you something — "can I run this command?", "which of
these three approaches do you want?" — nothing is sent. The session sits blocked at a dialog
in a terminal you may not be looking at, and audiochatty never mentions it.

This plan closes that. Every point where Claude Code waits on a human decision becomes
something you can hear and answer by voice, including approvals for destructive commands
(👤's call, 2026-08-04). The terminal freezes while it waits — also 👤's call — so the person
at the keyboard sees a paused session rather than a dialog, and the answer arrives from the
phone instead.

The mechanism is a hook that **blocks**. Claude Code lets a `PermissionRequest` hook return
the approve/deny decision itself and skip showing the dialog at all. So instead of typing an
answer into the terminal, audiochatty answers *in place of* the terminal. That single fact is
what makes this tractable — see D1.

## Current state

**Built and working.** The outbound half. `hooks/hooks.json` registers three hooks:
`SessionStart` (connect a resumed session), `Stop` (post a finished turn), `SessionEnd`. The
`Stop` hook (`scripts/stop_hook.py:59-90`) posts to `POST /agent/turn`, which enqueues a row
in `agent_turn_jobs` for a background rewriter to turn into something speakable. The inbound
half exists too: `GET /agent/inbound` serves instructions spoken to a session, the wrapper's
poller (`wrapper/poller.py`) picks them up, and `wrapper/inject.py` types them into the
terminal as a bracketed paste.

**The voice side already knows about coding sessions.** The agent treats a session as a kind
of peer — `agent.py` has a `peer_kind == "agent"` fork, prompts `respond_agent.md`,
`ask_context_agent.md`, `session_reachable.md`, `session_unreachable.md`, and
`db.create_agent_instruction()` writes the spoken instruction back. None of that needs
rebuilding; questions ride alongside it.

**What is missing.** There is no path at all for "Claude is blocked on a decision." No hook is
registered for it, no table stores it, no endpoint accepts it, and the voice agent has no way
to return a *structured* answer — everything it sends today is prose destined to be typed into
a prompt box.

**Deployment.** The backend at `https://audiochat-backend.onrender.com` is live and serving
the existing agent routes. Nothing in this plan is deployed until Phase 7.

## Design decisions

**D1 — The answer comes back through the hook's own poll, not by typing into the terminal.**
Claude Code's `PermissionRequest` hook can return `{"behavior": "allow" | "deny"}` and the
permission dialog is never shown (verified: `code.claude.com/docs/en/hooks`, PermissionRequest
decision control). So the hook posts the question, waits, and returns the decision. The
alternative — let the dialog render and have the wrapper type the keystroke — was rejected: a
permission dialog is an arrow-key selector, not the prompt box the injector knows how to write
to, and a spoken answer arriving a minute later could land on a *different* dialog than the one
it was answering. Blocking inside the hook makes staleness structurally impossible, because
nothing else can answer the question while the hook holds it.

**D2 — The terminal freezes while waiting, up to 600 seconds.** 👤's explicit call. 600s is the
documented default timeout for a `PermissionRequest` hook. While the hook runs, the session
shows no dialog and makes no progress.

**D3 — Every failure of the voice channel falls through to the normal terminal dialog. Never
auto-deny, never auto-allow.** If the hold times out, the backend is unreachable, the session
isn't bound to audiochatty, or the answer is unparseable, the hook returns no decision and
Claude Code shows the dialog exactly as it would today. A voice channel that silently denied a
tool call because you stepped away would be worse than one that just asked at the keyboard,
and one that silently allowed would be dangerous. This is the single most important safety
property in the plan and Phase 3 tests it directly.

**D4 — Answers are structured choices, not prose.** A question carries a list of options, each
with an id; an answer is an **option id**, not a sentence. The voice agent's job is to pick one,
and the hook maps the id to allow/deny. Nothing anywhere infers "yes" from free speech. This
matters most for destructive commands: "no, wait — actually go ahead" must not be pattern-matched
into an approval by a transcription. If the agent can't resolve what the user said to exactly one
option, it asks again rather than guessing, and if it never resolves, D3 takes over.

**D5 — Questions bypass the turn-rewrite queue.** A finished turn goes into `agent_turn_jobs`
and waits for a background worker to rewrite it into speakable prose. A question can't afford
that latency — someone is sitting at a frozen terminal — and it doesn't need it, since the
question text is already short. Questions get their own table and are read out close to
verbatim.

**D6 — There is one mechanism, not two: `PermissionRequest` covers all three cases.** ~~Multi-choice
questions use a different mechanism than permissions, and Phase 0 decides which.~~ *Superseded by
Phase 0's findings, 2026-08-04.* The premise was wrong. `AskUserQuestion` **does** go through a
permission check — `PermissionRequest` fires for it with the full question and option list in
`tool_input`, and so does `ExitPlanMode` with the full plan text. So a single hook on a single
event handles a `Bash` approval, a multi-choice question, and a plan approval. `PreToolUse`
deny-with-reason is not needed and neither is keystroke injection. This is the single biggest
simplification the probe bought: Phase 5 collapses into Phase 3.

**D9 — A multi-choice question is answered by *allowing* the tool with the answer written into
its input.** `PermissionRequest` can return `updatedInput` alongside `behavior: "allow"`, and
`AskUserQuestion`'s own input schema carries an `answers` field documented as "user answers
collected by the permission component" — an object keyed by question text, valued with the
chosen option's label. Phase 0 observed exactly that field appearing in `tool_input` at
`PostToolUse` after a question was answered at the keyboard. So the hook answers a question by
allowing the call with `answers` pre-filled, and the picker never renders.

**PROVEN in Phase 0.5, 2026-08-04.** A hook returning `behavior: "allow"` with
`updatedInput.answers = {"<question text>": "<option label>"}` answered the question outright:
no picker rendered, and the model continued as though that option had been chosen. Claude Code
reported it in the transcript as `User answered Claude's questions: … → Plain text lines`
followed by `Allowed by PermissionRequest hook` — so the mechanism is not silent, and the
person at the keyboard can see that a hook answered rather than they did. That transparency is
worth keeping in mind for D2: a frozen terminal is not an unexplained one.

**D10 — `Notification` is not a usable trigger.** The obvious design — listen for
`notification_type: "permission_prompt"` — does not work. Phase 0 saw 9 permission requests
produce only 2 such notifications, each arriving about 6 seconds *after* the request. It is
debounced: it fires when a prompt has been sitting unanswered, not when it is raised. Anything
answered promptly never emits one. `PermissionRequest` is the trigger; `Notification` is at most
a later "they still haven't looked at it" signal, and nothing depends on it.

**D7 — A question is scoped to a session exactly like a turn.** Same device-token auth
(`require_device_token`), same `_lookup_session` resolution, same quiet-404 for an ended or
unknown session. A question for a session that isn't active is dropped and the hook falls
through per D3.

**D8 — No push notifications; the POC constraint stands.** You find out about a pending
question the same way you find out about anything else — it shows in the inbox, and when you
are in a call the agent raises pending questions before anything else. This is a real
limitation: if nobody calls in, the terminal stays frozen for the full 600s. Accepted for now;
noted in Open questions.

## Sequencing

Parallel means several terminals working on this one checkout at the same time — no worktrees,
no second clone of the repo.

Run Phase 0 first, by itself — it decides part of the design — then Phase 0.5, which is a
two-minute experiment but gates all of Phase 5. Then run Phase 1, also by itself, because it
changes the database shape everything else reads. Once that's applied, run Phases 2, 3, 4, and 6
at the same time, each in its own terminal. When Phase 3 is finished, run Phase 5 by itself (it
edits the same files Phase 3 does). Phase 7 is last and alone.

- **Phase 0** is a fact-finding phase. It answers which hooks actually fire when Claude Code
  asks a question, and D6 can't be settled without it. It only writes a throwaway logging
  script, so nothing depends on it except the decisions it produces. **Done** — see its findings
  table.
- **Phase 0.5** tests one claim (D9) that Phase 5 is entirely built on. It is separated from
  Phase 0 because it is the first hook that *decides* something rather than observing, and
  because getting a false answer here would be discovered late and expensively.
- **Phase 1** adds a database table and the rules controlling who can read it. Every other
  phase reads or writes that table, so it has to land alone, first, and 👤 has to apply it to
  the live project before anything can be tested against it.
- **Phases 2, 3, 4, and 6 run together safely.** They touch four separate services and share no
  files: Phase 2 is only `audiochat-backend/app/routes_agent.py`; Phase 3 is only
  `audiochat-plugin/`; Phase 4 is only `audiochat-agent/`; Phase 6 is only
  `audiochat-frontend/`. Only Phase 2 needs a local server on port 8000. They agree with each
  other through the endpoint contract written down in Phase 2's items, which is why that
  contract is spelled out in this file rather than discovered by reading code.
- **Phase 5** edits `scripts/permission_hook.py` and `hooks/hooks.json` — the same two files
  Phase 3 creates. Two terminals editing those would clobber each other with no warning, so it
  waits.
- **Phase 7** is end-to-end verification and deploy. It needs all the others finished, and a
  deploy is exactly the kind of one-at-a-time, hard-to-undo step that shouldn't overlap with
  anything.

**Never run at the same time:** Phase 1 (it changes what every later phase depends on) and
Phase 7 (a deploy, plus it's the only phase that exercises all four services against each
other).

### Phase 0 — Find out what actually fires

**Goal:** real hook payloads for every "Claude is waiting on you" state, so D6 stops being a
guess.

- [x] 🤖 Write `scripts/probe_hook.py` — appends its stdin JSON plus the event name to `/tmp/audiochatty-probe.jsonl`, exits 0, prints nothing
- [x] 🤖 Register it in a *local* `.claude/settings.json` for `PermissionRequest`, `PreToolUse`, `Notification`, and `Stop` — not in `hooks/hooks.json`, this is throwaway (registered for `PermissionRequest`, `Notification`, and `PreToolUse`/`PostToolUse` matching `AskUserQuestion|ExitPlanMode`; `Stop` was dropped, `stop_hook.py` already proves that path works)
- [x] 👤 Run a session in manual mode and trigger a plain tool permission prompt (e.g. a `Bash` command), then answer it at the keyboard
- [x] 👤 In the same session, trigger an `AskUserQuestion` (ask Claude something that makes it offer you options) and a plan-mode approval
- [x] 🤖 Read the captured JSONL and write findings into this file under Phase 0 — which events fired for each case, in what order, and what `tool_input` / `metadata` carried
- [x] 🤖 Settle D6 in this file from those findings (superseded; added D9 and D10). Probe registration now lives in `.claude/settings.local.json` and is removed at the end of Phase 0.5, not here — the spike needs it

**Done when:** this file states, from observed payloads, exactly which hook carries the
question text for each of the three cases. — **Done.**

**Findings (18 events captured, 2026-08-04).** All three cases run through one event:

| Case | Events, in order | Where the readable text is |
|---|---|---|
| `Bash` / `Edit` approval | `PermissionRequest` | `tool_input.command` / `tool_input.file_path` |
| `AskUserQuestion` | `PreToolUse` → `PermissionRequest` → *(answered)* → `PostToolUse` | `tool_input.questions[].question` + `.options[].label` / `.description` |
| Plan approval | `PreToolUse` → `PermissionRequest` → *(approved)* → `PostToolUse` | `tool_input.plan` (full markdown) |

- **`PermissionRequest` fires for `AskUserQuestion` and `ExitPlanMode`**, not just for tools
  that need approval. This is what killed the old D6.
- **`PreToolUse` always precedes `PermissionRequest`** for the same call, and carries nothing
  extra. Nothing needs to hook it.
- **`PostToolUse` for `AskUserQuestion` shows `answers` merged into `tool_input`** alongside
  `questions` and `annotations` — the observation D9 rests on.
- **`PermissionRequest` does not carry `tool_use_id`**, though the published field list says it
  does. Observed keys: `tool_name`, `tool_input`, `permission_mode`, `cwd`, `effort`,
  `prompt_id`, `session_id`, `transcript_path`, and `permission_suggestions` (only for real
  approvals — absent on `AskUserQuestion` and `ExitPlanMode`). `PreToolUse`/`PostToolUse` *do*
  carry `tool_use_id`. Correlation must therefore use `prompt_id` + `session_id`.
- **`permission_suggestions`** carries the "always allow this" rules the dialog would offer.
  Not used yet; it is the natural raw material for the open question about tools that should
  skip voice approval.

### Phase 0.5 — Prove the answer mechanism (D9)

**Goal:** confirm that allowing `AskUserQuestion` with `answers` pre-filled actually answers it
and suppresses the picker. Everything about multi-choice depends on this one fact.

- [x] 🤖 Write `scripts/probe_answer_spike.py` — on `PermissionRequest` for `AskUserQuestion` only, return `allow` with `updatedInput` carrying `answers` mapping each question's text to its *first* option label; fall through silently for every other tool
- [x] 🤖 Register it as a second `PermissionRequest` hook in `.claude/settings.local.json`
- [x] 👤 Trigger one `AskUserQuestion` and report what happened: did the picker appear, and did Claude proceed as though the first option was chosen? — **no picker; first option applied**
- [x] 🤖 Record the result here; if it failed, revert D9 and reinstate deny-with-reason as D6's mechanism — D9 proven, no revert needed
- [x] 🤖 Remove both probe scripts and the whole `hooks` block from `.claude/settings.local.json` — `probe_hook.py`, `probe_report.py`, `probe_answer_spike.py` and both `/tmp` logs deleted; the `permissions` block in that file was left alone

**Done when:** D9 is marked proven or disproven in this file, from an observed session. — **Done: proven.**

### Phase 1 — The questions table

**Goal:** a place to store a pending question and its answer, readable by the people who should
see it and nobody else.

- [x] 🤖 Add `agent_questions` to `migrations/schema.sql` — id, `session_id` (→ `agent_sessions`, cascade), `kind` (`permission` | `choice`), `tool_name`, `tool_use_id`, `prompt_text`, `options` jsonb, `status` (`pending` | `answered` | `expired`), `answer_option_id`, `answered_by`, timestamps. Also carries `workspace_id` and `owner_profile_id` denormalised from the session, so the policy never depends on another table's RLS to be the boundary
- [x] 🤖 Add an index on `(session_id, status)` — every read is "the pending ones for this session" — plus `(owner_profile_id, status)` for the inbox's cross-session scan
- [x] 🤖 Add row-level security to `migrations/policies.sql` so a user can read questions only for sessions in their own workspace, and write nothing; the backend and agent use the service-role key and bypass it
- [x] 🤖 Explicitly `enable row level security` on the table even though users get read-only access — an unmentioned table is a public table, not a protected one
- [x] 🤖 Extend `migrations/verify_rls.py`: anon sees nothing; the adversary can neither read nor answer Mike's question; Mike *can* read his own but still cannot answer it from the browser; and the probe row is cleaned up (it hangs off the seeded session, so it would otherwise persist)
- [x] 👤 Apply `schema.sql` and `policies.sql` to the live Supabase project
- [x] 👤 Re-run `migrations/verify_rls.py` and confirm the new table survives the adversarial boundary check — 73/73, 2026-08-04

**Done when:** `agent_questions` exists live, and a signed-in user from another workspace
cannot read a row in it.

### Phase 2 — Backend endpoints

**Goal:** the three calls the hook and the voice agent need, on the existing agent blueprint.

- [x] 🤖 Add `POST /agent/question` to `app/routes_agent.py` — device-token auth, resolves the session per D7, inserts a `pending` row, returns `{question_id}` and 202; quiet 404 for an ended or unknown session
- [x] 🤖 Add `GET /agent/question/<id>` — the hook's poll; returns status and, once answered, `answer_option_id`
- [x] 🤖 Add `POST /agent/question/<id>/answer` — service-role only, called by the voice agent; rejects an option id that isn't in the stored `options`, and rejects a second answer to an already-answered question. **See D11** — the agent does not in fact call it
- [x] 🤖 Add `POST /agent/question/<id>/expire` — the hook calls it when it gives up, so a stale question stops showing as pending in the inbox
- [x] 🤖 Cap and normalise `prompt_text` and `options` on arrival the way `_normalize_payload` already does for turns — this is untrusted input from a hook
- [x] 🤖 Add tests in `audiochat-backend/tests/` covering: answer with a bogus option id is rejected, double-answer is rejected, question for an ended session 404s — 17 tests, and the whole backend suite is 205 green
- [x] 👤 Confirm the endpoint shapes read sensibly before Phases 3 and 4 build against them, and settle D11

**Done when:** `pytest` passes and a question can be posted, polled, and answered against local
Flask with `curl`.

**D11 — `/answer` has no caller, and authenticates with the service-role key itself.**
The plan asks for a "service-role only" endpoint here, but Phase 4 has the voice agent
answer through `src/db.py` — and the standing rule (`CLAUDE.md`, Architecture) is that the
agent never calls the backend. So the route was built as specified and is real, but nothing
in this plan uses it: it is the `curl` surface and the contract, not the path. Its
credential is the service-role key presented as a bearer token — not a new shared secret,
just the credential its notional caller holds by definition. Two ways to settle this: drop
the route, or keep it as the documented side door. Left standing for 👤 to decide, because
a route nobody calls is cheap and an approval endpoint nobody audited is not.

### Phase 3 — The blocking permission hook

**Goal:** a permission prompt in a bound session is decided by voice, and every failure falls
through to the terminal dialog.

- [x] 🤖 Add `post_question()` and `poll_question()` to `scripts/audiochat.py`, alongside the existing `post_turn()`, reusing its device token, gzip, and circuit-breaker conventions — plus `expire_question()` and the first `get()` this client has needed
- [x] 🤖 Write `scripts/permission_hook.py` — reads the `PermissionRequest` payload, returns immediately with no decision if the session has no marker (unregistered), posts the question, then polls until answered or the hold expires
- [x] 🤖 Carry `prompt_id` rather than `tool_use_id` as the correlation key — Phase 0 found `PermissionRequest` does not actually send `tool_use_id` despite the published field list
- [x] 🤖 Build the option list from the tool call: allow / deny, with the tool name and a readable summary of `tool_input` as the prompt text
- [x] 🤖 Emit the decision as `hookSpecificOutput.decision.behavior` on exit 0; emit *nothing* on any failure path so Claude Code shows its own dialog (D3)
- [x] 🤖 Make the hold duration and poll interval tunable by env var, defaulting to 600s and 2s, and call `/expire` on the way out when the hold runs down — `AUDIOCHATTY_APPROVAL_HOLD` / `AUDIOCHATTY_APPROVAL_POLL`
- [x] 🤖 Register `PermissionRequest` in `hooks/hooks.json` with `timeout` set above the hold so the hook is never killed mid-poll — 660s
- [x] 🤖 Add tests in `tests/` covering the four fall-through cases: unregistered session, backend unreachable, hold expired, unparseable answer — each must produce no decision. `tests/test_permission_hook.py`, and `TestFallsThrough` covers eleven of them, not four
- [x] 👤 Confirm the frozen-terminal experience is acceptable in practice, including what it looks like when you *don't* answer

**A fifth failure mode, added in the build: the backend dying mid-hold.** The question was
raised and then the network went. Three consecutive failed polls is treated as the backend
being gone rather than a hiccup on hotel wifi, and it falls through like the rest — one
blip should not cost somebody their voice approval, and holding a terminal against a
backend that is not there is just a slower way of doing the same thing.

**Done when:** in a bound session, a `Bash` permission prompt never renders and is decided by a
row written to `agent_questions`; and with the backend stopped, the same prompt renders normally.

### Phase 4 — The voice side

**Goal:** you hear the pending question and your spoken choice resolves it.

- [x] 🤖 Add `pending_questions_for_session()` and `answer_question()` to `audiochat-agent/src/db.py`, on `WorkspaceDB` so they can't skip the workspace filter
- [x] 🤖 Write `prompts/answer_question.md` — reads the question and its options out, and states that the reply must resolve to exactly one option or be asked again (D4)
- [x] 🤖 Add an `answer_question` tool to `agent.py` taking an option id, wired into the existing `peer_kind == "agent"` fork's tool list — no new `Agent` subclass, no new deployed agent
- [x] 🤖 Load pending questions before connecting, alongside the existing agent-session context, and raise them ahead of ordinary turns in the opening instruction — a new `{pending_questions}` slot in `respond_agent.md`, empty when nothing is waiting
- [x] 🤖 Handle the "answered while you were speaking" race — a question answered elsewhere is dropped from the list rather than re-read. Two guards: `db.answer_question` is a compare-and-swap on `status = 'pending'` and returns False when it loses, and `CallState.answered_question_ids` stops the model answering the same one twice and being told *its own* answer landed elsewhere
-  [x] 👤 Test in `console` mode with a hand-inserted `agent_questions` row and confirm the read-out is unambiguous for a destructive command

**`answer_question` is offered on both coding-agent forks, reachable or not** — the one
place this work does *not* follow `send_to_session`'s rule. The two travel by different
roads: an instruction is typed into the terminal by the wrapper, which is what
`channel_verified_at` is the proof of, while an answer is collected by the blocked hook's
own poll. A session with no verified return path can still be frozen on a decision, and
withholding the tool there would leave it frozen for the full ten minutes over a road the
answer never uses.

**Stale questions are aged out at 15 minutes** (`db.MAX_QUESTION_AGE`). The hook's
`/expire` call is best effort, so a killed hook leaves a `pending` row forever; reading one
out would ask somebody to approve a command that stopped waiting for them and was since
decided at the keyboard. The frontend's Phase 6 read uses the same window and says so.

**Done when:** `uv run python src/agent.py console` reads a pending question aloud and a spoken
choice writes `answer_option_id`.

### Phase 5 — Multi-choice and plan approvals

**Goal:** `AskUserQuestion` and `ExitPlanMode` are answerable by voice too.

*Shrunk by Phase 0. This was a separate mechanism; D6 collapsed it into the same hook Phase 3
builds, so what is left is the shaping of the question and the answer, not a second code path.*

- [x] 🤖 In `scripts/permission_hook.py`, branch on `tool_name`: `AskUserQuestion` posts `kind: "choice"` with the real options from `tool_input.questions[].options[].label`; everything else posts `kind: "permission"` with allow/deny
- [x] 🤖 Return a choice as `behavior: "allow"` plus `updatedInput` carrying `answers` keyed by question text (D9), rather than as a bare allow — and carrying the *whole* original input with `answers` merged in, since `updatedInput` is documented as "modified tool arguments" and an input that had lost its `questions` would be a different call than the one approved
- [x] 🤖 Handle the multi-question case — `tool_input.questions` is a list and an `AskUserQuestion` may carry more than one; either answer all of them or fall through, never half. Each question is raised in turn, all sharing one hold, and a decision is emitted only once every one has come back
- [x] 🤖 Give `ExitPlanMode` its own summary: `tool_input.plan` is full markdown and was 5,000+ characters in Phase 0's sample, far too long to read aloud verbatim — send a short summary as `prompt_text` and keep approve/reject as the options. The summary is the plan's headings and its size, which is what identifies *which* plan without reading it
- [x] 🤖 Extend the Phase 3 tests to cover the choice path, an option list longer than two, and a multi-question payload — 46 tests in the file now
- [x] 👤 Confirm a multi-choice question read over voice is actually distinguishable by ear — three similar options may not be

**One payload is deliberately not attempted: `multiSelect`.** An answer is a single option
id (D4) and there is no way to say "these two" in that vocabulary, so a multi-select
question falls through to the keyboard rather than being answered as though it were a
single pick. Same for a payload with more than four questions or a question with fewer than
two options — neither is something `AskUserQuestion`'s own schema produces, so a payload
like that is a surprise, and surprises go to the keyboard.

**Done when:** an `AskUserQuestion` in a bound session is answered by voice without the picker
appearing, and a plan is approved by voice without its full text being read out.

### Phase 6 — Surfacing it in the app

**Goal:** a pending question is visible, not just audible.

- [x] 🤖 Add a pending-question read to `audiochat-frontend/lib/` alongside the existing inbox query, using the anon key and the user's session — `lib/questions.ts`
- [x] 🤖 Show pending questions distinctly from unread messages in the inbox — a frozen terminal is more urgent than a report. Its own destructive-coloured line above the list, a bordered row, the command itself rather than "waiting on you", and a **Decide** button
- [x] 🤖 Let the existing 5s inbox poll pick them up rather than adding a second timer — the two reads are one `Promise.all` sharing the poll's abort controller
- [x] 👤 Confirm the distinction reads clearly and doesn't look like just another unread badge

**Done when:** posting a question makes it appear in the inbox within one poll interval.

**A session with a question and no messages still gets a row.** It froze before it
reported anything, which is the case the user most needs to see, so the merge *creates* an
inbox entry rather than only decorating one that unread messages found. `UnreadSender.count`
can therefore be 0 — nothing downstream may assume a sender has unread messages.

### Phase 7 — End to end and deploy

**Goal:** the whole loop works against deployed services.

- [ ] 👤 Run all four locally and approve one real destructive command by voice, end to end
- [ ] 👤 Confirm the deny path actually stops the command running
- [ ] 👤 Deploy the backend to Render
- [ ] 👤 Redeploy the agent to LiveKit Cloud — note the standing `lk agent update-secrets` obligation from `PLAN.md` §7 Phase 10 still applies and is unrelated to this work
- [x] 🤖 Update `CLAUDE.md`: the plugin section, and the stale claim that the backend's "three endpoints is the finished surface" — `routes_agent.py` and `routes_device.py` already made that untrue
- [x] 🤖 Add the new sharp edges to `CLAUDE.md` — the blocking hook, the 600s freeze, and D3's fall-through rule, plus three the build turned up: the option-id rule living in three places, `MAX_QUESTION_AGE` being duplicated across two languages, and an installed plugin being a copy rather than a symlink

**Done when:** a permission prompt raised on a laptop is approved from a phone against deployed
services, and denying one demonstrably blocks the command.

## Open questions

- **Nobody is told the terminal is frozen (D8).** If you don't happen to call in, the session
  sits for 600s and then falls through. Whether that's acceptable in daily use is a 👤 judgment
  after Phase 3 — the fix, if it needs one, is a notification, which the POC has deliberately
  avoided so far.
- **Should some tools skip voice approval?** Right now every permission prompt goes to voice.
  There may be a class — a one-line `git status` — where the freeze costs more than the
  approval is worth. 👤 decides after living with Phase 3.
- **Concurrent questions from several sessions.** Fourteen terminals open, three of them
  frozen, is a plausible Tuesday. The read-out order and how you disambiguate "which session am
  I answering" is unresolved; Phase 4 assumes one at a time.
- **Whether the `PreToolUse` deny-with-reason mechanism (D6) confuses the model** over a long
  session — a transcript full of blocked tool calls that were actually answered questions may
  degrade behaviour. Watch for it in Phase 5.
