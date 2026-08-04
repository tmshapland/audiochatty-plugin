# Onboarding: connecting a Claude Code session to audiochatty

This walks through the first-time setup for the `audiochatty` plugin, in the order a new
user actually hits it, based on what `commands/`, `scripts/audiochat.py`, `wrapper/`, and the
plugin's own `README.md` do. It's a companion to the README, not a replacement — read that
too, especially the "What this actually does to your machine" table.

## 0. What you're turning on

Two independent, opt-in directions:

- **Session → you**: after each completed turn, `scripts/stop_hook.py` posts a short
  summary to your audiochatty inbox, which reads it back to you as speech.
- **You → session**: what you say into audiochatty is typed into that same terminal, at the
  prompt — because that is literally what happens, not an analogy. This is the half worth
  pausing on: it can edit files and run commands on the machine the session is running on.

Both halves stop the moment you disconnect or disable the plugin.

**Scope, up front, because it's the part people get wrong:** install, `/audiochatty-login`,
and making `audiochatty` a command are one-time, per machine. Starting the session with
`audiochatty run` is **not** — it repeats for *every single Claude Code launch* you want
audiochatty in at all, with no way to make it stick.

But it is the *only* thing that repeats. `audiochatty run` connects the session itself, so
there is no second command to remember and nothing to type into the session. If you used an
earlier version of this plugin and are looking for `/audiochatty-connect`, section 4 explains
what became of it.

It's also all-or-nothing: a session started with plain `claude` sends nothing and receives
nothing — full stop, not "listen-only." There is no listen-only state at all. Launching
through audiochatty starts both directions at once; launching with `claude` starts neither.
See sections 3 and 4.

## 1. Install the plugin (once per machine)

```bash
claude plugin marketplace add tmshapland/audiochatty-plugin
claude plugin install audiochatty@audiochatty
```

Until you pair (next step), the plugin is inert — no background process, no network
calls.

## 2. Pair the machine — `/audiochatty-login` (once per machine)

Start a Claude session. Then, run it once to get a pairing code:

```
> /audiochatty-login

  Open https://audiochatty.com/link and enter this code:

        WXYZ-1234

  It expires in 10 minutes.
  Once you've entered it, run /audiochatty-login again to finish.
  After you enter the code, run /audiochatty:audiochatty-login again here in Claude Code.
```

Enter that code at **audiochatty → Settings → Link a coding agent** in your browser
(you're already signed in there). Then run the same command again in Claude to complete pairing:

```
> /audiochatty-login
  Linked to <your workspace> as <your name>.

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

**The alias is one-time; `audiochatty run` is not.** Add the alias (or put
`wrapper/audiochatty` on your `PATH`) once and never think about it again. But the command
itself applies only to the one session it starts: close this terminal, open a new one, and
that new session needs `audiochatty run` again, every time, for as long as you want
audiochatty connected to it. Pairing and the alias are the only parts of this that are truly
once-per-machine.

**Why a short code instead of a pasted token:** a token typed into the Claude Code prompt
would be written into the session's `.jsonl` transcript and loaded into the model's
context — permanently. So the long-lived device token never touches the terminal; it
travels from the backend straight into a `0600` file at `~/.audiochatty/credentials.json`.
The plugin will never ask you to paste one, and no command here accepts one as an
argument.

## 3. Why you start it with `audiochatty run`

**In a session started with plain `claude`, audiochatty will not connect at all, ever, and
this repeats for every new session.** Everything below explains why, but that's the one
sentence to not miss.

The receive direction has to type into a session that is *already running*, and nothing inside
a session can do that — not a plugin, not a hook, not a slash command. So the thing that types
has to be the session's parent. `audiochatty run` opens a pseudo-terminal, starts the real
`claude` inside it, and sits in the middle passing bytes both ways; when you speak an
instruction from your phone, it types it in.

What that buys, and what it costs:

- **It looks and feels identical.** Claude Code asks its terminal how wide it is, sets raw
  mode, and draws, and gets the same answers it always did. Resizes, Ctrl-C, plan mode,
  `/clear`, VS Code's `/ide` and diff viewer and `@`-autocomplete all work unchanged. Measured
  overhead: about 34 microseconds per keystroke, and nothing at all on redraw throughput.
- **No warning dialog, no flag, nothing to approve.** An earlier version of this plugin used a
  Claude Code channel, which during the research preview needed
  `--dangerously-load-development-channels` and the full-screen warning that comes with it.
  That is gone — the wrapper needs no cooperation from Claude Code at all, because Claude Code
  can't tell it's there.
- **It can type into your terminal.** That is the real cost and it is not hypothetical: the
  same mechanism that types an instruction could type anything, and Claude Code would act on
  it with whatever permissions that session already has. `wrapper/README.md` has the limits in
  a table — who can make it type (a local process that can both reach a loopback port and read
  your credentials file), and what reaches it from outside (only messages your own workspace
  addressed to this session, fetched by asking, never pushed).
- **macOS and Linux only.** Pseudo-terminals don't exist on Windows.

Everything after `run` goes to `claude` unchanged, so `audiochatty run --model opus` works,
and `audiochatty run -- --resume` handles the flags that would otherwise clash.

## 4. What happens when you launch it (nothing for you to do)

```
$ audiochatty run
```

That's the whole of it. The session registers itself under the current folder's name and is
ready to talk to before you've finished reading this sentence. To choose the name:

```
$ audiochatty run --name billing-refactor
```

The name is how the session shows up in your inbox.

**It connects silently, and that's deliberate.** Nothing is printed about it — the screen
belongs to Claude Code's interface, and a line written into it is a corrupted display. So the
confirmation is the session appearing in your inbox, and `/audiochatty-status` is the local
answer if you want one.

The flip side is worth knowing: a connect that *fails* is equally invisible. If audiochatty is
unreachable or this machine's device token has been revoked, the terminal looks exactly the
same and the session simply never appears on your phone. `/audiochatty-status` names the cause,
and `/audiochatty-connect` retries it without restarting anything.

**There is no handshake to wait for.** An earlier version had one — a nonce injected into the
session, a tool call to answer it, a retry if it went unanswered — because a channel genuinely
could not tell whether its events were being honoured. The wrapper owns the terminal, so
connecting *is* the proof: your phone shows the session as reachable immediately.

### What became of `/audiochatty-connect`

It used to be a required second step, run inside every session. It isn't any more, and it isn't
needed for a normal launch — run it and it will most likely just tell you the session is
already connected. It survives for three things, none of which needs you to restart Claude
Code:

| | |
| --- | --- |
| **Retry** | audiochatty was down when the session launched, so the connect failed. |
| **Rename** | `/audiochatty-connect auth-bug` renames this session in your inbox. |
| **Reconnect** | you ran `/audiochatty-disconnect` earlier and want it back. |

It still **refuses outright** in a session that wasn't started with `audiochatty run`, and
prints the command to start again with:

```
> /audiochatty-connect
  This session has no audiochatty return path, so it can't be talked to — and a
  session you can't talk to isn't worth registering.

  Start Claude Code through audiochatty instead:

      audiochatty run

  That is the same Claude Code you already use — same terminal, no plugin to load,
  no warning — and it connects the session itself, so there's nothing to run after
  it.

  If `audiochatty` isn't a command on this machine yet, that is one line in your
  shell profile:

      alias audiochatty="/path/to/audiochat-plugin/wrapper/audiochatty"
```

There is one other refusal, rarer and worth recognising: if you run plain `claude` from
*inside* a wrapped session — a Bash tool call, a nested shell — that inner session inherits the
outer wrapper's environment without owning it. Nothing connects it automatically, and
`/audiochatty-connect` refuses it too, because connecting it would type your instructions into
the outer terminal instead of the one you're looking at. The same applies to a forked session
(`/fork`, `/branch`, `--fork-session`).

### One case where a hook does the connecting

`audiochatty run` knows the session id because it chooses it. With `--resume`, `--continue`, or
`/resume`, the session id is not its to choose, so a small `SessionStart` hook does the connect
from inside the session instead. Nothing about this is visible to you — `audiochatty run
--resume` behaves exactly like a fresh launch — but it's why the plugin registers a
`SessionStart` hook at all.

## 5. Everyday commands

| Command | What it does | Network call? |
| --- | --- | --- |
| `/audiochatty-login` | Pair this machine. Once per machine. | Yes |
| `/audiochatty-connect [name]` | Rarely needed — see section 4. Retry a failed connect, rename this session, or reconnect a disconnected one. | Yes |
| `/audiochatty-status` | Is this machine paired, is this session connected, can it be talked to, and why not. | No — entirely local |
| `/audiochatty-disconnect` | Stop sending and receiving for this session. The machine stays paired and the session keeps running. | Yes — after the local teardown, and a failure is swallowed |

The one command that isn't in this table is the one that matters most: **`audiochatty run`**,
which is how you start a session at all.

Every other terminal you have open is unaffected — the `Stop` hook runs globally but
finds no marker file for an unconnected session and exits; the `SessionStart` hook does one
environment-variable check and exits; and a session started with plain `claude` has nothing
listening to it.

## 6. Turning it off

- **For one session:** `/audiochatty-disconnect`. The session carries on; it just goes quiet
  in both directions. **It stays quiet** — including across a `/clear`, which re-runs the
  thing that normally connects a session. Only `/audiochatty-connect` brings it back.
- **Disable everywhere:** `claude plugin disable audiochatty`, and go back to starting sessions
  with `claude`.
- **Remove entirely:** `claude plugin uninstall audiochatty && rm -rf ~/.audiochatty`
- **Lost or stolen machine:** revoke its token from audiochatty → **Settings → Linked
  devices**. The next thing that machine sends gets a `401`, and the next thing it polls
  for gets nothing.

## What actually crosses the network

Worth knowing before you connect a sensitive session — see the README for the full
payload shapes:

- **Session → audiochatty**, once per completed turn: the final assistant message, the
  *names* of tools used (no arguments, no file contents, no command output), how the turn
  ended, and the working directory. Prompts and code are never sent.
- **audiochatty → session**: whatever you dictated, transcribed verbatim, typed into the
  prompt as a bracketed paste followed by one Enter. Claude Code treats it as your own typing
  because that is what it is.
- If the backend is unreachable, outgoing turns are dropped silently (not retried) and the
  plugin backs off for 60 seconds — a hook that blocks on the network is a hook you'd feel
  on every turn. Incoming instructions are *not* lost the same way; they queue and arrive
  once the backend answers again.
- Nothing is ever pushed to this machine. The wrapper asks the backend whether anything is
  waiting, every 5 seconds while a session is active, slowing to every 30 when nothing has
  arrived for a while.

## Troubleshooting

Start with `/audiochatty-status` — it's local-only and diagnoses which half is broken. In
order of likelihood:

1. **Started with `claude`, not `audiochatty run`.** Nothing connected the session, and
   nothing can. `/audiochatty-status` says so, and `/audiochatty-connect` refuses and prints
   the command to start again with — use it verbatim. `audiochatty run` is per session, so a
   fresh terminal needs it again.
2. **The connect failed at launch, silently.** It has no way to tell you (section 4).
   `/audiochatty-status` names the cause — audiochatty unreachable, or a revoked device — and
   `/audiochatty-connect` retries without restarting the session.
3. **You disconnected it earlier.** That is sticky by design and survives a `/clear`.
   `/audiochatty-status` will say so; `/audiochatty-connect` undoes it.
4. **An instruction hasn't arrived yet.** Give it 30 seconds, the slow end of the poll. Also
   check you aren't mid-way through typing a line: the wrapper deliberately waits for you to
   pause rather than typing over you, so a slow instruction is often that working.
5. **Backend asleep/down.** Silent drop by design; wait, or check `/audiochatty-status`.
6. **Device revoked.** Everything 401s silently — re-run `/audiochatty-login`.

Debug logging: set `AUDIOCHATTY_DEBUG=1` for one stderr line per hook run, more detail from
`/audiochatty-connect` about which wrapper it found and why it accepted or refused it, and a
narration from `audiochatty run` of what it polls and types in.
