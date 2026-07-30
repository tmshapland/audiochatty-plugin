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
`audiochatty run` and then `/audiochatty-connect` are **not** — they repeat for *every single
Claude Code launch* you want audiochatty in at all, with no way to make it stick.

It's also all-or-nothing *at the point of connecting*: in a session started with plain
`claude`, `/audiochatty-connect` refuses outright rather than half-connecting, so that session
sends nothing and receives nothing — full stop, not "listen-only." There is no listen-only
state at all any more: a successful connect starts both directions at once, and a refused one
starts neither. See sections 3 and 4.

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
```

Enter that code at **audiochatty → Settings → Link a coding agent** in your browser
(you're already signed in there). Then run the same command again in Claude to complete pairing:

```
> /audiochatty-login
  Linked to <your workspace> as <your name>.

  One more step, and it's per session rather than per machine: a session can only
  be talked to if you start Claude Code through audiochatty. Start it with:

      audiochatty run

  then run /audiochatty-connect there. It's the same Claude Code you already use —
  same terminal, nothing to load, no warning — with a return path attached. Under
  plain `claude` there is nothing to tell that session, and /audiochatty-connect
  refuses outright rather than registering a session you can't reach.

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

**In a session started with plain `claude`, audiochatty will not connect at all —
`/audiochatty-connect` refuses outright, every time, and this repeats for every new session.**
Everything below explains why, but that's the one sentence to not miss.

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

## 4. Connect a session — `/audiochatty-connect [name]` (once per session)

Launch Claude Code through audiochatty, then connect:

```
> audiochatty run
> /audiochatty-connect billing-refactor
  This session is now "billing-refactor" in audiochatty.
  You can hear what it does, and tell it what to do next, from audiochatty.
```

The name is how the session shows up in your inbox; it defaults to the current folder
name if you omit it.

**There is no handshake to wait for.** An earlier version had one — a nonce injected into the
session, a tool call to answer it, a retry if it went unanswered — because a channel genuinely
could not tell whether its events were being honoured. The wrapper owns the terminal, so
connecting *is* the proof: `/audiochatty-connect` tells the backend this session is reachable
in the same breath, and your phone shows it as reachable immediately.

`/audiochatty-connect` is **per session, not per machine** — a new terminal tab is a new
session and needs its own connect. It also **refuses outright** (rather than half-connecting)
in a session that wasn't started with `audiochatty run`, and prints the command to start again
with. This is what that refusal looks like — it's the single most common thing you'll hit,
usually from forgetting that the launch command repeats every time:

```
> /audiochatty-connect
  This session has no audiochatty return path, so it can't be talked to — and a
  session you can't talk to isn't worth registering.

  Start Claude Code through audiochatty instead:

      audiochatty run

  That is the same Claude Code you already use — same terminal, no plugin to load,
  no warning — with a return path attached. Then run /audiochatty-connect again.

  If `audiochatty` isn't a command on this machine yet, that is one line in your
  shell profile:

      alias audiochatty="/path/to/audiochat-plugin/wrapper/audiochatty"
```

There is one other refusal, rarer and worth recognising: if you run plain `claude` from
*inside* a wrapped session — a Bash tool call, a nested shell — that inner session inherits the
outer wrapper's environment without owning it. `/audiochatty-connect` detects that and refuses,
because connecting it would type your instructions into the outer terminal instead of the one
you're looking at.

## 5. Everyday commands

| Command | What it does | Network call? |
| --- | --- | --- |
| `/audiochatty-login` | Pair this machine. Once per machine. | Yes |
| `/audiochatty-connect [name]` | Connect *this* session, both directions. | Yes |
| `/audiochatty-status` | Is this machine paired, is this session connected, can it be talked to. | No — entirely local |
| `/audiochatty-disconnect` | Stop sending and receiving for this session. The machine stays paired and the session keeps running. | No |

Every other terminal you have open is unaffected — the `Stop` hook runs globally but
finds no marker file for an unconnected session and exits; any other `audiochatty run` you
have open sits idle and unbound.

## 6. Turning it off

- **For one session:** `/audiochatty-disconnect`. The session carries on; it just goes quiet
  in both directions.
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

1. **Started with `claude`, not `audiochatty run`.** `/audiochatty-connect` will refuse and
   print the command to start again with — use it verbatim.
2. **Session never connected.** Connect is per-session; a fresh terminal needs its own
   `/audiochatty-connect`.
3. **An instruction hasn't arrived yet.** Give it 30 seconds, the slow end of the poll. Also
   check you aren't mid-way through typing a line: the wrapper deliberately waits for you to
   pause rather than typing over you, so a slow instruction is often that working.
4. **Backend asleep/down.** Silent drop by design; wait, or check `/audiochatty-status`.
5. **Device revoked.** Everything 401s silently — re-run `/audiochatty-login`.

Debug logging: set `AUDIOCHATTY_DEBUG=1` for one stderr line per hook run, more detail from
`/audiochatty-connect` about which wrapper it found and why it accepted or refused it, and a
narration from `audiochatty run` of what it polls and types in.
