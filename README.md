# audiochatty for Claude Code

## TL;DR

This plugin lets you talk to a Claude Code session with voice.

It works by wrapping Claude Code in a pseudo-terminal. `audiochatty run` opens a pseudo-terminal, starts `claude` inside it, and sits as a layer
between you and the terminal running Claude Code. Every key you actually press still passes straight through to Claude Code,
but the wrapper can also type into it on its own. 

Messages get routed through a backend that both ends poll or push to. When a turn finishes, a hook on your laptop sends a short summary
of it to the backend. The frontend web app fetches it from there and reads
it out. When you talk back, that same web app sends a tidied version of what you said to the backend. The wrapper on your laptop polls the
backend, sees it, and types it into the pseudo-terminal. Permission prompts work the same way,
just synchronous and blocking: the terminal freezes, the question goes to the backend, you
answer from the web app, and the answer comes back the same path. The plugin code itself never talks to
the web app directly. Instead it only ever talks to whatever backend URL it's configured with.

**There's a hosted version of the frontend and voice agent at audiochatty.com**, and pairing
against it is the default if you don't configure anything else. But that backend is just an
implementation of a documented HTTP protocol (see "Exactly what the hook sends" and "Pointing
at a different backend" below). Nothing about the plugin requires it. Point
`AUDIOCHATTY_BACKEND_URL` at your own backend and pair against that instead, and you can build
your own frontend and voice agent behind it.

---

Talk to a Claude Code session from wherever you are. When a session you've opted in finishes
a turn, a short spoken summary of what it did shows up in your
[audiochatty](https://github.com/tmshapland) inbox. You listen to it, ask follow-up
questions out loud — and say what to do next, which arrives in that session as a prompt. And
when Claude Code stops to ask *you* something — a permission prompt, a multiple-choice
question, a plan to approve — the terminal freezes and audiochatty asks instead, so the
decision can come from wherever you have audiochatty open rather than whoever happens to be at the keyboard.

```
you, away from your desk                  this laptop
─────────────────────────                 ────────────────────────────────────────
  hear what it did       ◀── audiochatty ◀── scripts/stop_hook.py       ◀─ the turn it just finished
  say what to do next    ──▶ audiochatty ──▶ audiochatty run           ──▶ typed into your session
  decide, out loud       ◀─▶ audiochatty ◀─▶ scripts/permission_hook.py ◀─▶ a blocked permission prompt
```

All three are opt-in per session, and all three stop when you disconnect. The second row is
the one to read carefully: **what you say into audiochatty is typed into a coding agent that
edits files and runs commands on this machine.** It is exactly as powerful as typing the same
words into the terminal yourself, which is the point, and it is worth knowing before you
connect a session. The third row goes further still — it doesn't type a request, it
**decides**: allow or deny a tool call, approve or reject a plan, pick one of a handful of
options, on your say-so and without anyone touching the keyboard.

---

## What this actually does to your machine

This is short on purpose. Read it before you install, and read it again after — everything
below is in this repo, in the files it names.

| | |
| --- | --- |
| **Runtime** | Python 3, standard library only. No pip, no venv, no build step, nothing to install beyond the plugin itself. |
| **Background processes** | One, and you start it yourself: `audiochatty run`, which is how you launch Claude Code once you want a session you can talk to. It runs for as long as that session does, and it connects that session itself — on a paired machine it registers and starts polling at launch. On a machine you haven't paired yet it does nothing at all: no polling, no network. The hooks are still short-lived processes Claude Code spawns, waits on, and discards — all except `PermissionRequest`, which is short-lived in the same sense but not fast on purpose: it can hold the terminal open for up to 10 minutes while a decision comes back by voice. |
| **What that process can do** | Type into your Claude Code session, as if you had typed it. That is the whole feature and the whole risk; `wrapper/README.md` spells out the limits. |
| **What the blocking hook can do** | Decide a permission prompt, a multiple-choice question, or a plan approval — *in place of* the dialog Claude Code would otherwise show, so it never renders. Every failure (backend unreachable, hold expired, session not connected, an answer that doesn't map to an offered option) falls through to that same dialog in silence; it never auto-allows and never auto-denies. `scripts/permission_hook.py`'s docstring is the detail. |
| **On disk** | `~/.audiochatty/` (mode 0700): a credentials file (0600) with your device token, one marker file per registered session, one tombstone per session you disconnected by hand, and one rendezvous file (0600) per running wrapper. Nothing else — a pending question lives on the backend, not on this machine. |
| **Network** | Your audiochatty backend, and nowhere else — POSTs out from `scripts/audiochat.py`, and a poll for anything addressed to this session from `audiochatty run`. Nothing accepts a connection from off this machine. |
| **When it's off** | Until you pair the machine, the plugin is inert — `audiochatty run` on an unpaired machine makes no network call at all. A session started with plain `claude` sends nothing and receives nothing, ever. |
| **Platform** | macOS and Linux. The pseudo-terminal `audiochatty run` is built on doesn't exist on Windows. |

---

## Install

The easiest path is to use the audiochatty.com frontend. Click the '+' button and follow the directions for connecting an agent.

```bash
claude plugin marketplace add tmshapland/audiochatty-plugin
claude plugin install audiochatty@audiochatty
```

Then, once per machine:

```
> /audiochatty-pair-start

  Open https://audiochatty.com/link and enter this code:

        WXYZ-1234

  It expires in 10 minutes.

  After you enter the code, run /audiochatty:audiochatty-pair-finish here in Claude Code.
```

Go back to audiochatty in a browser, click the link for the pairing page, and type the code. Then run the second half:

```
> /audiochatty-pair-finish

  Next, let's create a shortcut command for starting an audiochatty session. Quit
  Claude Code and add this line to your shell profile (vi ~/.zshrc or vi ~/.bashrc).

      alias audiochatty="/path/to/audiochat-plugin/wrapper/audiochatty"

  After you add the shortcut to your shell profile, open a
  new terminal so it takes effect.

  To connect Audiochatty to a Claude Code session, start Claude Code with the shortcut:
      audiochatty run --name [name]

  That connects the session for you — there's nothing else to run inside it.
```

**Why two commands, and why a code instead of a token.** A slash command's output is
substituted into the prompt *after* the command exits, so a single command that minted a
code and then waited would only show you the code once it had already given up waiting.

And the reason it's a code at all: anything typed into a Claude Code prompt is written to
the session `.jsonl` on disk and loaded into the model's context. A pasted token would
live there forever. So the terminal displays something short and disposable, and the
long-lived token travels back over a path the transcript never sees from the backend
straight into a 0600 file. 

**Why you start it with `audiochatty run` and not `claude`.** The second arrow in the
diagram above has to type into a session that is already running, and nothing *inside* a
session can do that. So `audiochatty run` starts Claude Code inside a pseudo-terminal it
owns, passes your keystrokes straight through, and types in what you speak into audiochatty.
Claude Code can't tell: there's no plugin to load, no launch flag, and no warning dialog.
From the session's point of view, somebody typed something. Everything after `run` is passed
to `claude` unchanged, so `audiochatty run --model claude-opus-4-8` and `audiochatty run -- --resume`
work the way you'd expect. `wrapper/README.md` is the detail, including what that process can
and can't do.

## Use

```
$ audiochatty run                      # or: audiochatty run --name billing-refactor
```

One command, and the session shows up in audiochatty under the current folder's
name. There is no second step and nothing to type into the session.

**It connects silently, on purpose.** The terminal belongs to Claude Code's interface, and a
line printed into it is a corrupted screen. The confirmation is the session appearing in
your inbox. The flip side is that a connect which *fails* (audiochatty unreachable, a revoked
device) is also invisible: `/audiochatty-status` is where that reason surfaces, and it's the
first thing to run if a session never shows up.

Work normally. The terminal looks no different, which is measured rather than hoped: about
34 microseconds per keystroke and nothing at all on redraw throughput, and each completed
turn shows up in your inbox under that name. The one visible change is that permission
prompts, `AskUserQuestion`s, and plan approvals stop rendering in this session and freeze the
terminal instead, waiting for you to decide by voice. See "Exactly what happens when Claude
Code stops to ask you something" below.

| Command | What it does |
| --- | --- |
| `/audiochatty-pair-start` | Get a pairing code for this machine. Once per machine. |
| `/audiochatty-pair-finish` | Finish that pairing, once you've entered the code in the browser. |
| `/audiochatty-connect [name]` | Rarely needed — sessions connect themselves. Run it to retry a connect that failed, to rename this session, or to reconnect one you disconnected. Refuses if the session wasn't started with `audiochatty run`. |
| `/audiochatty-status` | Is this machine paired, is this session connected, can it be talked to. Entirely local — no network call. |
| `/audiochatty-disconnect` | Stop sending and stop receiving. The machine stays paired and the session keeps running, and it stays disconnected until you run `/audiochatty-connect` — nothing automatic brings it back. |

Every other terminal you have open does nothing at all: the `Stop` and `PermissionRequest`
hooks are both global, so they run everywhere, look for a marker file for that session, find
none, and exit. A permission prompt in an unconnected terminal renders exactly as it always
has. A session started with plain `claude` has no return path to begin with, and any other
`audiochatty run` you have open sits unbound and idle.

To turn everything off: `claude plugin disable audiochatty`, and go back to starting sessions
with `claude`. To remove it: `claude plugin uninstall audiochatty` and `rm -rf ~/.audiochatty`.
To kill a machine you no longer have, revoke its token
from **Settings → Linked devices** in audiochatty.

---

## Exactly what the hook sends

Once per completed turn, in a connected session, `scripts/stop_hook.py` POSTs this and
nothing else:

```json
{
  "claude_session_id": "b3ea4f55-4ab6-48f7-8ba6-5fa8f3d2d81e",
  "last_assistant_message": "I finished the refactor and the tests pass.",
  "tool_calls": ["Edit", "Edit", "Bash", "Read"],
  "stop_reason": "end_turn",
  "cwd": "/Users/mike/repos/audiochat"
}
```

- **`last_assistant_message`** — the final assistant text of the turn, which Claude Code
  hands the hook directly. It is what the spoken summary is mostly written from.
- **`tool_calls`** — the names of the tools this turn used, read out of the session
  transcript. Names only: no arguments, no file contents, no command lines, no output.
- **`stop_reason`**, **`cwd`** — how the turn ended, and which repo it was.

The payload is built from scratch out of those keys, so nothing else from the hook's input is ever transmitted. Your prompts are not
sent. Your code is not sent. The backend caps every field again on arrival.

**Where it goes afterwards.** The backend queues it, and a worker rewrites it into
something worth hearing out loud using a third-party model (Gemini). The raw payload above
is stored alongside the summary so that follow-up questions can be answered from it. If
that isn't acceptable for your work, don't connect those sessions.

`SessionEnd` sends only `{"claude_session_id": …}`, and only when a session has genuinely
ended — never on `/clear` or `/resume`, which keep the same session id and the same open
terminal.

## Exactly what the wrapper types into your session

The other direction, and the same promise. What arrives is not an event, a notification, or
anything with a wrapper of its own. It is your words, at the prompt:

```
> change that back, and use the other helper instead
```

That's the whole of it. What arrives is a tidied rendering of what you said, not a verbatim
transcript: audiochatty's voice agent transcribes it, then cleans up false starts and joins
fragments the way you'd expect from something meant to be read cold. It is instructed to add
nothing, resolve nothing on your behalf, and leave nothing out. It goes in exactly where your
own typing goes, so Claude Code treats it as your own typing because that is what it is.

Three things about *how* it is typed, all of which look like bugs and aren't:

- **It arrives as a paste, then one Enter.** A spoken instruction routinely runs to several
  paragraphs. Typed raw, the prompt would submit at the first newline and treat the rest as a
  second instruction, so it goes in wrapped in the terminal's bracketed-paste markers instead.
  Escape sequences are stripped from the text first, so nothing inside a message can end the
  paste early and start issuing keystrokes of its own.
- **It waits for you to stop typing.** If you're mid-sentence when something arrives, the
  wrapper holds it until you've paused (1.5s by default). An instruction that seems slow is
  usually this working, not something stuck.
- **It may take up to 30 seconds to show up.** The wrapper polls; it checks every 5 seconds
  while a session is active and slows down when nothing has arrived for a while. Nothing is
  pushed to this machine.

**No tool is added and there is deliberately no reply tool** — the `Stop` hook is already the
reply path, so the way you hear what happened is the summary of the turn your instruction
caused. `wrapper/README.md` documents the rest: the poll, the dedupe, and the limits on what
that process can do.

## Exactly what happens when Claude Code stops to ask you something

This is the third direction, and it works differently from the other two: instead of
reporting or typing, `scripts/permission_hook.py` **answers in place of the dialog**, so the
dialog never renders at all. It covers everything Claude Code would otherwise stop and wait
for a keypress on — a `Bash` or `Edit` approval, an `AskUserQuestion`, an `ExitPlanMode` plan because all three fire the same `PermissionRequest` hook under the hood.

In a connected session, the terminal freezes and audiochatty sends you the question:

- **A tool approval** reads out the tool name and a short summary of what it would do (the
  command for `Bash`, the path for a file edit) and offers two options: allow it, or don't.
- **A multiple-choice question** (`AskUserQuestion`) reads out the real options from the
  question itself, one at a time if there's more than one. Answering doesn't just approve
  the tool call. The chosen option is written into it, so Claude proceeds exactly as if
  you'd picked it at the keyboard and the picker never appears.
- **A plan approval** (`ExitPlanMode`) doesn't read the whole plan aloud, but instead it reads a short summary (the headings and the size) and asks you to
  approve or hold off.

You answer by picking one of the options out loud, the same way you'd answer any other
audiochatty question. The terminal un-freezes the moment your answer arrives.

**The freeze lasts up to 10 minutes**, and it can end four other ways, all of which fall
through to Claude Code's own dialog exactly as if this hook didn't exist: the session isn't
connected, the backend can't be reached, the hold runs out before you answer, or the answer
doesn't match one of the options offered. 
The hook never auto-allows and never auto-denies. Silence from this hook means "show the
normal dialog," nothing else, and that rule is deliberately the same one `hooks/hooks.json`
documents for every other hook in this plugin.

**A permission prompt in *every other* terminal on the machine is untouched.** Like the
`Stop` hook, this one's first move is checking for that session's marker file; a session
started with plain `claude`, or one you've disconnected, never freezes and never sees this
hook do anything.

---

## When something isn't working

Start here:

```
> /audiochatty-status
```

It answers all of this locally, including which half is broken. Then, in order of
likelihood:

- **The session was started with `claude`, not `audiochatty run`.** There is nothing that can
  type into it, so nothing connected it. `/audiochatty-connect` refuses outright rather
  than half-connecting, printing the command to start again with. This is the common one, and
  `audiochatty run` is per session: a new terminal needs it again.
- **The connect failed at launch and said nothing.** It can't say anything. The screen belongs
  to Claude Code. `/audiochatty-status` names the reason (unreachable backend, revoked device),
  and `/audiochatty-connect` retries without restarting the session.
- **The session was disconnected earlier.** It stays that way deliberately, including across a
  `/clear`. `/audiochatty-connect` is the only thing that brings it back.
- **An instruction hasn't appeared yet.** Give it 30 seconds — that's the slow end of the
  poll — and check you aren't mid-way through typing a line, which makes the wrapper wait.
- **The backend is asleep or down.** Turns are dropped silently. That is deliberate, since
  a hook that waits on the network is a hook you feel on every turn. After one failure the
  plugin skips the network entirely for 60 seconds rather than paying the timeout again.
  Nothing is retried; a turn that couldn't be delivered is gone. Instructions coming the
  other way are not lost, though: they wait, and arrive when the backend answers again.
- **The device was revoked.** Everything gets a 401 and stays silent. Run
  `/audiochatty-pair-start` and `/audiochatty-pair-finish` to pair again.
- **A permission prompt isn't freezing, and you expected it to.** Check the same things as
  above — is this session connected, is the backend reachable — and remember it's expected
  behaviour for a payload this hook won't attempt: a `multiSelect` question, more than four
  questions at once, or a question with fewer than two options. Those fall through to the
  normal dialog on purpose.
- **A permission prompt froze and nothing ever answered it.** After 10 minutes it gives up
  and shows the dialog anyway. If it comes back
  before then, someone (or something) else already answered it.

To see what the hook is actually deciding:

```bash
echo '{"session_id":"<your-session-id>","last_assistant_message":"test"}' \
  | AUDIOCHATTY_DEBUG=1 python3 ~/.claude/plugins/.../scripts/stop_hook.py

echo '{"session_id":"<your-session-id>","tool_name":"Bash","tool_input":{"command":"rm -rf build"}}' \
  | AUDIOCHATTY_DEBUG=1 python3 ~/.claude/plugins/.../scripts/permission_hook.py
```

`AUDIOCHATTY_DEBUG=1` puts one line on stderr per hook run and changes nothing else — for
`permission_hook.py` that includes *why* it fell through, which is the fastest way to tell
"nobody answered in time" from "this session isn't connected" from "the backend didn't
respond." The same variable makes `/audiochatty-connect` explain which wrapper it found and
why it accepted or refused it, and makes `audiochatty run` narrate its own connect at launch
plus what it polls and types in. Stderr only, never stdout — for `SessionStart` in
particular, stdout would be fed to the model as context.

### Pointing at a different backend

```bash
AUDIOCHATTY_BACKEND_URL=http://localhost:8000 python3 scripts/audiochat.py login
```

`--backend-url` does the same for one command. Once paired, the URL you paired against is
remembered in `~/.audiochatty/credentials.json`, because a device token is only valid at the
backend that minted it. The wrapper resolves the same URL the same way when it connects itself,
so it never needs configuring separately.

---

## Development

```bash
python3 -m unittest discover -s tests     # no network, no Claude Code needed
claude plugin validate .                  # manifests
```

The tests run every script as a subprocess against a stub HTTP server, and the wrapper as a
real subprocess with a real pseudo-terminal and a fake `claude`, with `AUDIOCHATTY_HOME`
pointed at a temp directory — so they're safe to run on a machine that is already paired, and
they assert on what was actually sent over the wire and what actually reached the child.

## License

MIT. See [LICENSE](LICENSE).
