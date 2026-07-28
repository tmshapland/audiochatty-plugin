# The audiochatty channel

This directory is the **return path**. The rest of the plugin pushes out — a `Stop` hook
posts each finished turn to audiochatty, where it is rewritten into something you can
listen to from anywhere. This is the other direction: what you say back, spoken into
audiochatty on your phone, arrives in your running Claude Code session as a prompt.

It is a [Claude Code channel](https://code.claude.com/docs/en/channels-reference): an MCP
server that Claude Code spawns as a subprocess and that can push events into the session
it belongs to.

```
you, away from your desk                  this laptop
─────────────────────────                 ────────────────────────────────────────
  speak into audiochatty  ──▶ Supabase ──▶ channel/server.ts  ──▶ your Claude Code session
                              (polled)     (this directory)       (as a prompt)
  hear what it did       ◀──  Supabase ◀── scripts/stop_hook.py ◀─ the turn it just finished
```

## What it sends into your session

One thing, and this is exactly what it looks like when it arrives:

```
<channel source="plugin:audiochatty:audiochatty"
         message_id="8f3c…" sender_name="Mike" sent_at="2026-07-27T18:04:11Z">
change that back, and use the other helper instead
</channel>
```

Your terminal renders that as a one-line `← audiochatty` summary. The body is what you
dictated, verbatim — audiochatty transcribed it, nobody rewrote it. Claude is told (in the
server's `instructions`, which go into its system prompt) to treat it exactly as if you had
typed it into the terminal yourself.

It also sends **one handshake event** when the session first connects, asking Claude to
call the `audiochatty_ack` tool with a nonce. That is the only way this process can learn
that its events are actually being honoured — see "Why the handshake" below.

And that is the whole list. It sends nothing else, ever.

## What it does *not* do

- **It has no reply tool.** A two-way channel usually gives Claude a `reply` tool. This one
  deliberately does not: the `Stop` hook is already the reply path. Claude gets your
  instruction, does the work, finishes the turn, and the hook posts the summary that lands
  in your audiochatty inbox. A reply tool would be a second, racing copy of that.
- **It reads nothing.** No transcript, no files, no environment beyond the five `CLAUDE_*`
  variables listed below. The outbound half of this plugin is what reads your session, and
  the plugin's top-level README documents exactly what it sends.
- **It does nothing at all until it is bound.** A channel server starts in *every* session
  where the plugin is enabled. This one binds a loopback port, writes one file, and then
  sits still until `/audiochatty-connect` tells it which session it belongs to. Measured on
  an idle unbound process: **0.05s of CPU over 35 seconds** (i.e. nothing but the event
  loop) and no network traffic whatsoever. The one cost that is not zero is memory — a Bun
  process is ~55–80 MB resident, and there is one per Claude Code session.
- **It asks for nothing until the handshake is answered.** Binding starts the handshake and
  nothing else; the poll begins when the ack comes back. See "Why the handshake".

## How it knows which session it belongs to

This is the part that is easy to get wrong, so it is worth reading before changing
anything here. The server is spawned when `claude` launches — *before* `/audiochatty-connect`
runs — and one machine can have fifteen of them. Nothing may inject into the wrong terminal.

At startup it:

1. binds an **ephemeral port on 127.0.0.1** (no fixed number to collide over), and
2. writes `~/.audiochatty/channels/<pid>.json`, mode 0600, describing itself:

```jsonc
{
  "version": 1,
  "pid": 41213, "ppid": 41200, "port": 62115,
  "started_at": "2026-07-27T18:02:55Z",
  // innermost first, up to the top: how `connect` recognises its own `claude`
  "ancestry": [{ "pid": 41213, "ppid": 41200, "comm": "bun" },
               { "pid": 41200, "ppid": 41199, "comm": ".../claude" }, …],
  // by allowlist, never a blanket dump of the environment
  "claude_env": { "CLAUDECODE": "1" },
  "bound": false, "verified": false,
  "claude_session_id": null, "agent_session_id": null,
  "session_name": null, "backend_url": null,
  "bound_at": null, "verified_at": null
}
```

`/audiochatty-connect` runs *inside* one session, computes its own process ancestry the
same way, finds the channel whose chain shares its `claude` process, and POSTs to that
port. Ambiguity is an error, never a guess. **The device token is deliberately not in this
file** — it lives in `credentials.json` and has no business being copied.

The file is deleted on exit, and a channel starting up prunes any file left behind by a
process that no longer exists. A stale rendezvous file is how `connect` binds to a corpse.

### The local endpoints (what `/audiochatty-connect` talks to)

| Route | Body | Answer |
| --- | --- | --- |
| `POST /bind` | `{agent_session_id, claude_session_id, backend_url, token, session_name?}` | `200 {status:"bound", …}` · `400 missing_fields` · `403 token_mismatch` · `409 already_bound` (with `claude_session_id`, `same_session`, `verified`) |
| `POST /unbind` | `{token}` | `200 {status:"unbound", claude_session_id}` |
| `GET /status` | — | `{pid, bound, verified, claude_session_id, agent_session_id}` |

Two gates on `/bind`, and both matter: the **token must match this machine's
`credentials.json`**, so a stray local process cannot point the channel at a session it
does not own, and a **second bind is refused**, because a channel that can be rebound is a
channel that can be redirected mid-session.

## The backend contract

Frozen with `channel_return_path_plan.md` Phase 2. All three carry the device token as
`Authorization: Bearer`:

```
GET  /agent/inbound?session_id=<uuid>  → {"messages": [{"id", "text",
                                                        "sender_name", "created_at"}, …]}
POST /agent/inbound/ack                ← {"message_ids": ["…"]}
POST /agent/session/verified           ← {"claude_session_id": "…"}
```

An **ended or unknown session answers the poll with an empty list, not a 404**: this
process polls forever, and it has no way to tell a real error from an expected one. (The
parser also accepts a bare array, so a backend that returns one is not a bug here.)

The poll runs every **5s** while something could plausibly be in flight, backs off to
**30s** after a minute of quiet, and after any failure skips the network entirely for
**60s** — the same circuit breaker `scripts/stop_hook.py` uses, because the failure it
guards against is the same one: a sleeping Render service must never become a hot loop.

## Delivery is at-least-once, deduped here

Channel notifications are **not acknowledged**. `mcp.notification()` resolves when the
bytes hit the transport, and if the session never loaded this server as a channel the
event is dropped with no error. So the backend cannot learn from the transport whether
anything arrived, and this process is where exactly-once actually happens:

1. inject the event,
2. write the id to `~/.audiochatty/channels/<claude_session_id>.delivered.json`,
3. *then* `POST /agent/inbound/ack`.

A crash in the gap between 2 and 3 replays into a dedupe. A crash between 1 and 2 replays
into a duplicated instruction, which is why the ledger is written first — the whole point
of this feature is that an instruction edits files on your machine.

The ledger is keyed by Claude Code session rather than by pid, so it survives the restart
it exists for. An ack that fails is retried on the next poll; ids the backend serves again
are re-acked rather than re-injected.

## Why the handshake

A channel server **cannot tell whether it is registered**. It starts whenever the plugin is
enabled; `--channels` / `--dangerously-load-development-channels` decides only whether its
notifications are *honoured*, and unhonoured ones vanish silently.

So on binding it injects one handshake event asking Claude to call `audiochatty_ack` with a
nonce. An ack proves the path works, and the backend is told
(`POST /agent/session/verified` → `agent_sessions.channel_verified_at`). No ack, one retry
90 seconds later, then it stays unverified — and **the place that failure surfaces is the
audiochatty inbox**, which says the session cannot be talked to. A warning in a terminal
you have walked away from is worth nothing.

`audiochatty_ack` is the only tool this server exposes and must never become a
general-purpose way to send text back. See R10 in the plan.

**The ack is also the starting gun for the poll**, and that ordering was bought with a real
session rather than reasoned into existence. The first version polled from the moment it
was bound, so on a session with an instruction already waiting — the ordinary case, since
an instruction spoken to a session that has since restarted is delivered when it
reconnects — the handshake event and the instruction were handed to the model *in the same
batch*. Claude Code queues events and delivers them together; the model answered the
handshake and never acted on the instruction. It read as one notification with two
paragraphs, because that is what it was.

Waiting for the ack fixes that structurally instead of asking the prompt to be cleverer,
and it buys something worth more: **a message is never injected into a session that has not
proven it can receive one.** An unverified channel asks the backend for nothing, so an
instruction waits for a session that can act on it rather than vanishing into one whose
notifications are being dropped.

## Prompt injection

The docs are blunt that an ungated channel is a prompt-injection vector: anyone who can
reach your endpoint can put text in front of Claude. Three things gate this one.

- The loopback port accepts **only** `/bind` and `/unbind`, both requiring this machine's
  device token, and neither can inject anything. There is no local route that pushes text
  into the session.
- Everything that *is* injected comes from `GET /agent/inbound` — an authenticated call
  with this machine's device token, answered only with messages addressed to *this session*
  inside your own audiochatty workspace. The sender check the docs ask for happens there,
  in the backend, where the workspace boundary already lives.
- The text is capped (20,000 characters) and a row with no id or no text is dropped rather
  than injected.

What this does **not** protect against is someone in your own audiochatty workspace
speaking an instruction at your session. That is the feature.

## Running and testing it

Requires [Bun](https://bun.sh). `node_modules/` is **committed** (Phase 0's decision), so
there is no build step and no install step — clone the plugin and it runs. The trade is
that this directory carries ~24 MB of vendored dependencies; `bun add` is the only thing
that should ever change them.

```bash
# what Claude Code effectively does, minus the session
AUDIOCHATTY_HOME=$(mktemp -d) bun server.ts

# the tests: no Claude Code, no Flask, no network — 25 of them
cd .. && python3 -m unittest tests.test_channel

# what it is doing, on stderr (stdout is the MCP transport and must stay clean)
AUDIOCHATTY_DEBUG=1 bun server.ts
```

`AUDIOCHATTY_HOME` moves `~/.audiochatty` somewhere else, exactly as it does for the CLI
and the hooks, which is what lets the suite run on a machine that is already paired.

The tests play the client half of MCP over stdio and `tests/stub_backend.py` plays
audiochatty, so everything above is asserted without a browser, a phone, or a paid API
call — including the ordering of the ledger and the ack, dedupe across a restart, and the
handshake round trip.

## Launching it for real

During the research preview a channel has to be on an Anthropic-curated allowlist, and this
one is not, so it needs the development flag and its warning dialog:

```bash
claude --dangerously-load-development-channels plugin:audiochatty@audiochatty
```

There is no way around that dialog short of an Anthropic partner listing or a Team/
Enterprise admin setting `allowedChannelPlugins`. What it buys you is the loop: you can
tell this session what to do next from audiochatty.
