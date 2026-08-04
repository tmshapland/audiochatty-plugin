# audiochatty for Claude Code

Talk to a Claude Code session from wherever you are. When a session you've opted in finishes
a turn, a short spoken summary of what it did shows up in your
[audiochatty](https://github.com/tmshapland) inbox. You listen to it, ask follow-up
questions out loud — and say what to do next, which arrives in that session as a prompt.

```
you, away from your desk                  this laptop
─────────────────────────                 ────────────────────────────────────────
  hear what it did       ◀── audiochatty ◀── scripts/stop_hook.py ◀─ the turn it just finished
  say what to do next    ──▶ audiochatty ──▶ audiochatty run     ──▶ typed into your session
```

Both halves are opt-in per session, and both stop when you disconnect. The second half is
the one to read carefully: **what you say into audiochatty is typed into a coding agent that
edits files and runs commands on this machine.** It is exactly as powerful as typing the same
words into the terminal yourself, which is the point, and it is worth knowing before you
connect a session.

---

## What this actually does to your machine

This is short on purpose. Read it before you install, and read it again after — everything
below is in this repo, in the files it names.

| | |
| --- | --- |
| **Runtime** | Python 3, standard library only. No pip, no venv, no build step, nothing to install beyond the plugin itself. |
| **Background processes** | One, and you start it yourself: `audiochatty run`, which is how you launch Claude Code once you want a session you can talk to. It runs for as long as that session does, and it connects that session itself — on a paired machine it registers and starts polling at launch. On a machine you haven't paired yet it does nothing at all: no polling, no network. The hooks are still short-lived processes Claude Code spawns, waits on, and discards. |
| **What that process can do** | Type into your Claude Code session, as if you had typed it. That is the whole feature and the whole risk; `wrapper/README.md` spells out the limits. |
| **On disk** | `~/.audiochatty/` (mode 0700): a credentials file (0600) with your device token, one marker file per registered session, one tombstone per session you disconnected by hand, and one rendezvous file (0600) per running wrapper. Nothing else. |
| **Network** | Your audiochatty backend, and nowhere else — POSTs out from `scripts/audiochat.py`, and a poll for anything addressed to this session from `audiochatty run`. Nothing accepts a connection from off this machine. |
| **When it's off** | Until you run `/audiochatty-login`, the plugin is inert — `audiochatty run` on an unpaired machine makes no network call at all. A session started with plain `claude` sends nothing and receives nothing, ever. |
| **Platform** | macOS and Linux. The pseudo-terminal `audiochatty run` is built on doesn't exist on Windows. |

---

## Install

```bash
claude plugin marketplace add tmshapland/audiochatty-plugin
claude plugin install audiochatty@audiochatty
```

Then, once per machine:

```
> /audiochatty-login

  Open https://audiochatty.com/link and enter this code:

        WXYZ-1234

  It expires in 10 minutes.
  Once you've entered it, run /audiochatty-login again to finish.
  After you enter the code, run /audiochatty:audiochatty-login again here in Claude Code.
```

Open audiochatty in a browser — you're already signed in — go to **Settings → Link a coding
agent**, and type the code. Then run `/audiochatty-login` again:

```
> /audiochatty-login
  Linked to Mike's Workspace as Mike.

  One more step, and it's per session rather than per machine: a session can only
  be talked to if you start Claude Code through audiochatty. Start it with:

      audiochatty run

  That's the whole of it — it connects the session for you, so there's nothing to
  run afterwards. It's the same Claude Code you already use: same terminal, nothing
  to load, no warning, with a return path attached. A session started with plain
  `claude` has no return path, so there is nothing there to tell it what to do.

  If `audiochatty` isn't a command on this machine yet, that is one line in your
  shell profile:

      alias audiochatty="/path/to/audiochat-plugin/wrapper/audiochatty"
```

**Why two runs, and why a code instead of a token.** A slash command's output is
substituted into the prompt *after* the command exits, so a single command that minted a
code and then waited would only show you the code once it had already given up waiting.
And the reason it's a code at all: anything typed into a Claude Code prompt is written to
the session `.jsonl` on disk and loaded into the model's context. A pasted token would
live there forever. So the terminal displays something short and disposable, and the
long-lived token travels back over a path the transcript never sees — from the backend
straight into a 0600 file. **The plugin will never ask you to paste a token, and no
command here accepts one as an argument.**

**Why you start it with `audiochatty run` and not `claude`.** The second arrow in the
diagram above has to type into a session that is already running, and nothing *inside* a
session can do that. So `audiochatty run` starts Claude Code inside a pseudo-terminal it
owns, passes your keystrokes straight through, and types in what you speak from your phone.
Claude Code can't tell: there's no plugin to load, no launch flag, and no warning dialog —
from the session's point of view, somebody typed something. Everything after `run` is passed
to `claude` unchanged, so `audiochatty run --model opus` and `audiochatty run -- --resume`
work the way you'd expect. `wrapper/README.md` is the detail, including what that process can
and can't do.

## Use

```
$ audiochatty run                      # or: audiochatty run --name billing-refactor
```

That's it — one command, and the session shows up in audiochatty under the current folder's
name. There is no second step and nothing to type into the session.

**It connects silently, on purpose.** The terminal belongs to Claude Code's interface, and a
line printed into it is a corrupted screen — so the confirmation is the session appearing in
your inbox. The flip side is that a connect which *fails* (audiochatty unreachable, a revoked
device) is also invisible: `/audiochatty-status` is where that reason surfaces, and it's the
first thing to run if a session never shows up.

Work normally — the terminal looks no different, which is measured rather than hoped: about
34 microseconds per keystroke and nothing at all on redraw throughput — and each completed
turn shows up in your inbox under that name.

| Command | What it does |
| --- | --- |
| `/audiochatty-login` | Pair this machine. Once per machine. |
| `/audiochatty-connect [name]` | Rarely needed — sessions connect themselves. Run it to retry a connect that failed, to rename this session, or to reconnect one you disconnected. Refuses if the session wasn't started with `audiochatty run`. |
| `/audiochatty-status` | Is this machine paired, is this session connected, can it be talked to. Entirely local — no network call. |
| `/audiochatty-disconnect` | Stop sending and stop receiving. The machine stays paired and the session keeps running, and it stays disconnected until you run `/audiochatty-connect` — nothing automatic brings it back. |

Every other terminal you have open does nothing at all: the `Stop` hook is global, so it
runs everywhere, looks for a marker file for that session, finds none, and exits. A session
started with plain `claude` has no return path to begin with, and any other `audiochatty run`
you have open sits unbound and idle.

To turn everything off: `claude plugin disable audiochatty`, and go back to starting sessions
with `claude`. To remove it: `claude plugin uninstall audiochatty` and `rm -rf ~/.audiochatty`.
To kill a machine you no longer have — a stolen laptop, an old work machine — revoke its token
from **Settings → Linked devices** in audiochatty; the next thing it sends gets a 401 and the
next thing it polls for gets nothing.

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

The payload is built from scratch out of those keys, so nothing else from the hook's input
— not `transcript_path`, not `permission_mode` — is ever transmitted. Your prompts are not
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
anything with a wrapper of its own — it is your words, at the prompt:

```
> change that back, and use the other helper instead
```

That's the whole of it. The text is what you dictated, verbatim — audiochatty transcribed it,
nobody rewrote it — and it goes in exactly where your own typing goes, so Claude Code treats
it as your own typing because that is what it is.

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

---

## When something isn't working

Start here:

```
> /audiochatty-status
```

It answers all of this locally, including which half is broken. Then, in order of
likelihood:

- **The session was started with `claude`, not `audiochatty run`.** There is nothing that can
  type into it, so nothing connected it — and `/audiochatty-connect` refuses outright rather
  than half-connecting, printing the command to start again with. This is the common one, and
  `audiochatty run` is per session: a new terminal needs it again.
- **The connect failed at launch and said nothing.** It can't say anything — the screen belongs
  to Claude Code. `/audiochatty-status` names the reason (unreachable backend, revoked device),
  and `/audiochatty-connect` retries without restarting the session.
- **The session was disconnected earlier.** It stays that way deliberately, including across a
  `/clear`. `/audiochatty-connect` is the only thing that brings it back.
- **An instruction hasn't appeared yet.** Give it 30 seconds — that's the slow end of the
  poll — and check you aren't mid-way through typing a line, which makes the wrapper wait.
- **The backend is asleep or down.** Turns are dropped silently — that is deliberate, since
  a hook that waits on the network is a hook you feel on every turn. After one failure the
  plugin skips the network entirely for 60 seconds rather than paying the timeout again.
  Nothing is retried; a turn that couldn't be delivered is gone. Instructions coming the
  other way are not lost, though: they wait, and arrive when the backend answers again.
- **The device was revoked.** Everything gets a 401 and stays silent. Run `/audiochatty-login`
  to pair again.

To see what the hook is actually deciding:

```bash
echo '{"session_id":"<your-session-id>","last_assistant_message":"test"}' \
  | AUDIOCHATTY_DEBUG=1 python3 ~/.claude/plugins/.../scripts/stop_hook.py
```

`AUDIOCHATTY_DEBUG=1` puts one line on stderr per hook run and changes nothing else. The
same variable makes `/audiochatty-connect` explain which wrapper it found and why it accepted
or refused it, and makes `audiochatty run` narrate its own connect at launch plus what it polls
and types in. Stderr only, never stdout — for `SessionStart` in particular, stdout would be fed
to the model as context.

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

Three things worth knowing if you change this code:

- **`scripts/stop_hook.py` must never be slow and must never raise.** It runs at the end of
  every turn in every session on the machine. The marker-file check comes first for that
  reason, and there's a test that fails if an unreachable backend costs more than one
  timeout.
- **A slash command finds its wrapper through one inherited environment variable**, not by
  inspecting processes. `AUDIOCHATTY_WRAPPER_PORT` and `AUDIOCHATTY_WRAPPER_PID` are set in the
  environment `claude` is started with, so everything the session runs can read them. The
  wrapper also mints its child's session id up front and publishes it, which is what lets
  `/audiochatty-connect` refuse to bind a *nested* plain `claude` that inherited those
  variables without owning them.
- **A session name containing a double quote won't survive** the shell line in
  `commands/audiochatty-connect.md`, since `$ARGUMENTS` is substituted as text. Use plain
  names.

## License

MIT. See [LICENSE](LICENSE).
