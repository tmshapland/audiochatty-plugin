# Onboarding: connecting a Claude Code session to audiochatty

This walks through the first-time setup for the `audiochatty` plugin, in the order a new
user actually hits it, based on what `commands/`, `scripts/audiochat.py`, and the plugin's
own `README.md` do. It's a companion to the README, not a replacement — read that too,
especially the "What this actually does to your machine" table.

## 0. What you're turning on

Two independent, opt-in directions:

- **Session → you**: after each completed turn, `scripts/stop_hook.py` posts a short
  summary to your audiochatty inbox, which reads it back to you as speech.
- **You → session**: what you say into audiochatty arrives back in that same terminal as
  a prompt — exactly as if you'd typed it yourself. This is the half worth pausing on:
  it can edit files and run commands on the machine the session is running on.

Both halves stop the moment you disconnect or disable the plugin.

**Scope, up front, because it's the part people get wrong:** install and `/audiochatty-login`
are one-time, per machine. The channel flag and `/audiochatty-connect` are **not** — they
repeat for *every single Claude Code launch* you want audiochatty in at all, with no way to
make it stick.

It's also all-or-nothing *at the point of connecting*: without the flag, `/audiochatty-connect`
refuses outright rather than half-connecting, so a session started without it sends nothing
and receives nothing — full stop, not "listen-only." A genuine listen-only state does exist,
but it's a *different* moment: right after a successful connect (flag present, session
registered), sending starts immediately while receiving waits on the handshake ack landing
(§4) — that narrow window, not a missing flag, is what "you can listen, but not talk to it"
actually describes. See sections 3 and 4.

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

  One more step, and it's per session rather than per machine: audiochatty needs
  Claude Code's channel flag before a session can be registered at all. Start
  Claude Code with:

      claude --dangerously-load-development-channels plugin:audiochatty@audiochatty

  then run /audiochatty-connect there. Without the flag, /audiochatty-connect
  refuses outright — the session sends nothing and receives nothing.
```

**This is not a one-time step.** The flag does not persist — it applies only to the one
`claude` process it's passed to. Close this terminal, open a new one, and that new session
needs `claude --dangerously-load-development-channels plugin:audiochatty@audiochatty` again,
every time, for as long as you want audiochatty connected to it. Pairing (`/audiochatty-login`,
above) is the only part of this that's truly once-per-machine.

**Why a short code instead of a pasted token:** a token typed into the Claude Code prompt
would be written into the session's `.jsonl` transcript and loaded into the model's
context — permanently. So the long-lived device token never touches the terminal; it
travels from the backend straight into a `0600` file at `~/.audiochatty/credentials.json`.
The plugin will never ask you to paste one, and no command here accepts one as an
argument.

## 3. The flag, and why it looks alarming

**Without this flag, audiochatty will not connect to this session at all — `/audiochatty-connect`
refuses outright, every time, and this repeats for every new session.** Everything below
explains why, but that's the one sentence to not miss.

> ⚠️ **`--dangerously-load-development-channels`** is a real Claude Code safety gate, not
> plugin decoration. The return path (audiochatty → session) is a [Claude Code
> channel](https://code.claude.com/docs/en/channels), and during the research preview a
> channel must be on an Anthropic-curated allowlist to register automatically. This one
> isn't — and putting it in the community marketplace doesn't change that. Claude Code
> shows a full-screen warning when you pass this flag. **Read that warning; it's doing its
> job.**
>
> There is no way around needing the flag short of an Anthropic partner listing, or a
> Team/Enterprise admin adding this plugin to `allowedChannelPlugins`.
>
> What the flag actually buys you: `/audiochatty-connect` checks for it *before*
> registering anything, and refuses outright if it's missing (§4) — no marker file gets
> written for the session. The `Stop` hook only ever sends for a session it finds a
> marker for, so a session started without the flag never even reaches send-only: it
> sends nothing and receives nothing, full stop, until you relaunch with the flag and
> connect again.

Once persisted in `~/.claude/settings.json` under `enabledPlugins`, the plugin itself
loads on every plain `claude` launch — the flag isn't needed for that. It's specifically
what makes the *channel* (the receive side) live for a given launch of the CLI.

## 4. Connect a session — `/audiochatty-connect [name]` (once per session)

Launch Claude Code with the flag, then connect:

```
> claude --dangerously-load-development-channels plugin:audiochatty@audiochatty
> /audiochatty-connect billing-refactor
  This session is now "billing-refactor" in audiochatty.
  You can hear what it does, and tell it what to do next, from audiochatty.
```

The name is how the session shows up in your inbox; it defaults to the current folder
name if you omit it.

**The handshake.** Connecting also opens the return path, so a `kind="handshake"` channel
event usually arrives in the same turn, carrying a one-time nonce. Claude is expected to
call the `audiochatty_ack` tool with that nonce and do nothing else. This is the only way
audiochatty can confirm the session is actually listening — an event it sends that never
gets honoured is dropped silently, with no error on either side. If the ack lands,
audiochatty shows the session as reachable; if not, it honestly reports that you can't
talk to it yet.

`/audiochatty-connect` is **per session, not per machine** — a new terminal tab is a new
session and needs its own connect. It also **refuses outright** (rather than
half-connecting) if the session wasn't started with the channel flag, and prints the
exact relaunch command when it does. This is what that refusal looks like — it's the
single most common thing you'll hit, usually from forgetting the flag repeats every launch:

```
> /audiochatty-connect
  This session was started without Claude Code's channel flag, so audiochatty
  will not connect to it at all — not even to listen. Nothing was registered.

  Start it again with the channel flag:

      claude --dangerously-load-development-channels plugin:audiochatty@audiochatty

  Claude Code will show a warning about development channels — that is expected;
  channels are in research preview. Then run /audiochatty-connect again.
```

## 5. Everyday commands

| Command | What it does | Network call? |
| --- | --- | --- |
| `/audiochatty-login` | Pair this machine. Once per machine. | Yes |
| `/audiochatty-connect [name]` | Connect *this* session, both directions. | Yes |
| `/audiochatty-status` | Is this machine paired, is this session connected, can it be talked to. | No — entirely local |
| `/audiochatty-disconnect` | Stop sending and receiving for this session. The machine stays paired. | No |

Every other terminal you have open is unaffected — the `Stop` hook runs globally but
finds no marker file for an unconnected session and exits; other channel processes sit
idle and unbound.

## 6. Turning it off

- **Disable everywhere:** `claude plugin disable audiochatty`
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
- **audiochatty → session**: whatever you dictated, transcribed verbatim, delivered as a
  channel event Claude treats as if you'd typed it.
- If the backend is unreachable, outgoing turns are dropped silently (not retried) and the
  plugin backs off for 60 seconds — a hook that blocks on the network is a hook you'd feel
  on every turn. Incoming instructions are *not* lost the same way; they queue and arrive
  once the backend answers again.

## Troubleshooting

Start with `/audiochatty-status` — it's local-only and diagnoses which half is broken. In
order of likelihood:

1. **Session never connected.** Connect is per-session; a fresh terminal needs its own
   `/audiochatty-connect`.
2. **Started without the flag.** `/audiochatty-connect` will refuse and print the relaunch
   command — use it verbatim.
3. **Handshake unanswered.** audiochatty reports the session as unreachable until the ack
   lands; finish a turn and re-check, or re-run `/audiochatty-connect`.
4. **Backend asleep/down.** Silent drop by design; wait, or check `/audiochatty-status`.
5. **Device revoked.** Everything 401s silently — re-run `/audiochatty-login`.

Debug logging: set `AUDIOCHATTY_DEBUG=1` for one stderr line per hook run, and more
detail from `/audiochatty-connect` about which channel it found and why.
