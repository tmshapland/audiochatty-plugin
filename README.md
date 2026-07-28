# audiochatty for Claude Code

Talk to a Claude Code session from wherever you are. When a session you've opted in finishes
a turn, a short spoken summary of what it did shows up in your
[audiochatty](https://github.com/tmshapland) inbox. You listen to it, ask follow-up
questions out loud — and say what to do next, which arrives in that session as a prompt.

```
you, away from your desk                  this laptop
─────────────────────────                 ────────────────────────────────────────
  hear what it did       ◀── audiochatty ◀── scripts/stop_hook.py ◀─ the turn it just finished
  say what to do next    ──▶ audiochatty ──▶ channel/server.ts   ──▶ your session, as a prompt
```

Both halves are opt-in per session, and both stop when you disconnect. The second half is
the one to read carefully: **what you say into audiochatty becomes a prompt in a coding
agent that edits files and runs commands on this machine.** It is exactly as powerful as
typing the same words into the terminal yourself, which is the point, and it is worth
knowing before you connect a session.

---

## What this actually does to your machine

This is short on purpose. Read it before you install, and read it again after — everything
below is in this repo, in the files it names.

| | |
| --- | --- |
| **Runtime** | Python 3, standard library only, for everything that pushes out. The return path is [Bun](https://bun.sh) and needs it installed; its dependencies are committed, so there is still no build step. |
| **Background processes** | One: `channel/server.ts`, which Claude Code starts and stops with each session. Until `/audiochatty-connect` binds it, it does nothing at all — no polling, no network. The hooks are still short-lived processes Claude Code spawns, waits on, and discards. |
| **On disk** | `~/.audiochatty/` (mode 0700): a credentials file (0600) with your device token, one marker file per registered session, and one rendezvous file per running channel. Nothing else. |
| **Network** | Your audiochatty backend, and nowhere else — POSTs out from `scripts/audiochat.py`, and a poll for anything addressed to this session from `channel/server.ts`. |
| **When it's off** | Until you run `/audiochatty-login`, the plugin is inert. Until you run `/audiochatty-connect` in a session, that session sends nothing and receives nothing. |

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
```

Open audiochatty in a browser — you're already signed in — go to **Settings → Link a coding
agent**, and type the code. Then run `/audiochatty-login` again:

```
> /audiochatty-login
  Linked to Mike's Workspace as Mike.

  One more step, and it's per session rather than per machine: audiochatty needs
  Claude Code's channel flag to talk back to a session. Start Claude Code with:

      claude --dangerously-load-development-channels plugin:audiochatty@audiochatty

  then run /audiochatty-connect there. The flag is what lets you tell that session
  what to do next from audiochatty; without it you can only listen.
```

**Why two runs, and why a code instead of a token.** A slash command's output is
substituted into the prompt *after* the command exits, so a single command that minted a
code and then waited would only show you the code once it had already given up waiting.
And the reason it's a code at all: anything typed into a Claude Code prompt is written to
the session `.jsonl` on disk and loaded into the model's context. A pasted token would
live there forever. So the terminal displays something short and disposable, and the
long-lived token travels back over a channel the transcript never sees — from the backend
straight into a 0600 file. **The plugin will never ask you to paste a token, and no
command here accepts one as an argument.**

**Why that alarming flag.** The return path is a [Claude Code
channel](https://code.claude.com/docs/en/channels), and during the research preview a
channel has to be on an Anthropic-curated allowlist to register. This one isn't, and
submitting it to the community marketplace wouldn't change that. So it needs the
development flag, and Claude Code shows a full-screen warning about it at startup — that
warning is doing its job, and you should read it. There is no way around it short of an
Anthropic partner listing or a Team/Enterprise admin adding this plugin to
`allowedChannelPlugins`. What the flag buys is the second arrow in the diagram above.

## Use

```
> claude --dangerously-load-development-channels plugin:audiochatty@audiochatty
> /audiochatty-connect billing-refactor
  This session is now "billing-refactor" in audiochatty.
  You can hear what it does, and tell it what to do next, from audiochatty.
```

That's it. Work normally — the terminal looks no different — and each completed turn shows
up in your inbox under that name. The registration turn itself is not one of them: you
just watched it happen, so it isn't also sent to you as a message.

| Command | What it does |
| --- | --- |
| `/audiochatty-login` | Pair this machine. Once per machine. |
| `/audiochatty-connect [name]` | Connect *this* session, both directions. The name defaults to the folder name. |
| `/audiochatty-status` | Is this machine paired, is this session connected, can it be talked to. Entirely local — no network call. |
| `/audiochatty-disconnect` | Stop sending and stop receiving. The machine stays paired. |

Every other terminal you have open does nothing at all: the `Stop` hook is global, so it
runs everywhere, looks for a marker file for that session, finds none, and exits. Every
other channel process sits unbound and idle.

To turn everything off: `claude plugin disable audiochatty`. To remove it:
`claude plugin uninstall audiochatty` and `rm -rf ~/.audiochatty`. To kill a machine you no
longer have — a stolen laptop, an old work machine — revoke its token from **Settings →
Linked devices** in audiochatty; the next thing it sends gets a 401 and the next thing it
polls for gets nothing.

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

## Exactly what the channel sends into your session

The other direction, and the same promise: one kind of event, and this is what it looks
like when it arrives.

```
<channel source="plugin:audiochatty:audiochatty"
         message_id="8f3c…" sender_name="Mike" sent_at="2026-07-27T18:04:11Z">
change that back, and use the other helper instead
</channel>
```

Your terminal renders that as a one-line `← audiochatty` summary. The body is what you
dictated, verbatim — audiochatty transcribed it, nobody rewrote it — and Claude is told to
treat it exactly as if you had typed it there yourself.

There is one other event, sent once when a session connects: a handshake asking Claude to
call the `audiochatty_ack` tool with a nonce. It is the only way this plugin can find out
whether its events are actually being honoured, since an unhonoured one is dropped with no
error. If the ack comes back, audiochatty shows the session as one you can talk to; if it
doesn't, audiochatty says you can't, which is the honest answer.

And that is the whole list. `audiochatty_ack` is the only tool this plugin adds, and there
is deliberately **no reply tool** — the `Stop` hook is already the reply path.
`channel/README.md` documents the rest: the poll, the dedupe, and why the handshake exists.

---

## When something isn't working

Start here:

```
> /audiochatty-status
```

It answers all of this locally, including which half is broken. Then, in order of
likelihood:

- **The session was never connected.** `/audiochatty-connect` is per session, not per
  machine, and a new terminal is a new session.
- **It was started without the channel flag.** `/audiochatty-connect` refuses outright in
  that case rather than half-connecting, and prints the command to start again with.
- **audiochatty says you can't talk to this session.** The handshake went unanswered. It is
  answered on the turn that connects, so finish a turn and check again; if it stays that
  way, run `/audiochatty-connect` again.
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
same variable makes `/audiochatty-connect` explain which channels it found and why it
picked or refused one, and makes the channel server log what it polls and injects.

### Pointing at a different backend

```bash
AUDIOCHATTY_BACKEND_URL=http://localhost:8000 python3 scripts/audiochat.py login
```

`--backend-url` does the same for one command. Once paired, the URL you paired against is
remembered in `~/.audiochatty/credentials.json`, because a device token is only valid at the
backend that minted it. The channel is handed that same URL when `/audiochatty-connect`
binds it, so it never needs configuring separately.

---

## Development

```bash
python3 -m unittest discover -s tests     # 111 tests, no network, no Claude Code needed
claude plugin validate .                  # manifests
```

The tests run every script as a subprocess against a stub HTTP server, and the channel as a
real subprocess speaking MCP over stdio, with `AUDIOCHATTY_HOME` pointed at a temp
directory — so they're safe to run on a machine that is already paired, and they assert on
what was actually sent over the wire. The channel tests need Bun; they skip themselves
without it.

Three things worth knowing if you change this code:

- **`scripts/stop_hook.py` must never be slow and must never raise.** It runs at the end of
  every turn in every session on the machine. The marker-file check comes first for that
  reason, and there's a test that fails if an unreachable backend costs more than one
  timeout.
- **A channel cannot tell whether channels are enabled.** Its server starts whenever the
  plugin is enabled; the launch flag decides only whether its events are honoured, and an
  unhonoured event is dropped with no error. That is why `/audiochatty-connect` reads the
  `claude` process's command line, and why the handshake exists.
- **A session name containing a double quote won't survive** the shell line in
  `commands/audiochatty-connect.md`, since `$ARGUMENTS` is substituted as text. Use plain
  names.

## License

MIT. See [LICENSE](LICENSE).
