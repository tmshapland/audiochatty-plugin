#!/usr/bin/env bun
/**
 * The audiochatty channel — the return path, in one file.
 *
 * `channel_return_path_plan.md` Phase 3 · R2, R4, R5, R6, R10, R11.
 *
 * Everything else in this plugin pushes *out*: a Stop hook posts each finished turn to
 * audiochatty, where it is rewritten into something you can listen to. This process is
 * the other direction. What you say back — spoken into audiochatty from wherever you are
 * — arrives here and is injected into this Claude Code session as a prompt.
 *
 * **Claude Code spawns this as a subprocess and talks to it over stdio.** So stdout is
 * the MCP transport and nothing may ever be printed to it. Debug output goes to stderr,
 * behind `AUDIOCHATTY_DEBUG`, and that is the only channel this file writes to by hand.
 *
 * ## The four problems this solves, in the order they occur
 *
 * **1. Which session am I?** (R4) This process starts when `claude` launches, *before*
 * `/audiochatty-connect` runs, and a machine can have fifteen of them. Nothing may inject
 * into the wrong terminal. So at startup it binds an ephemeral loopback port and writes
 * `~/.audiochatty/channels/<pid>.json` describing itself — port, pid, process ancestry,
 * and whatever `CLAUDE_*` variables reached its environment. `/audiochatty-connect`, which
 * runs *inside* one session and knows both its own ancestry and its `CLAUDE_SESSION_ID`,
 * picks the channel that shares its `claude` process and POSTs `/bind` to that port. Until
 * that happens this process does nothing at all: no polling, no network, no cost.
 *
 * **2. How does a laptop behind NAT hear anything?** (R5) It doesn't — it asks. Once
 * bound, `GET /agent/inbound` every 5s while something could plausibly be in flight,
 * backing off to 30s when it has been quiet, and skipping the network for a minute after
 * a failure (the same circuit-breaker discipline `stop_hook.py` uses, for the same reason:
 * a sleeping Render service must not become a hot loop).
 *
 * **3. What if a message is delivered twice?** (R6) Channel notifications are not
 * acknowledged — `mcp.notification()` resolves when the bytes hit the transport, and if
 * the session never loaded this server as a channel the event is dropped silently. So the
 * backend cannot learn from the transport whether anything landed. Delivery is
 * at-least-once with an explicit ack, and the ids that have been injected are written to
 * disk *before* the ack, so a crash in between replays into a dedupe rather than into a
 * duplicated instruction.
 *
 * **4. Is anyone actually listening?** (R11) A channel server cannot tell whether it is
 * registered. This server starts whenever the plugin is enabled;
 * `--channels` / `--dangerously-load-development-channels` decides only whether its
 * notifications are *honoured*, and unhonoured ones vanish with no error. So on binding it
 * injects one handshake event asking Claude to call `audiochatty_ack` with a nonce. If the
 * ack comes back, the return path is proven and the backend is told
 * (`POST /agent/session/verified`). If it never comes back the session stays unverified,
 * and the place the user finds that out is the audiochatty inbox — not this terminal,
 * which they have walked away from.
 *
 * ## What it is not
 *
 * There is **no reply tool** (R10). The Stop hook is already the reply path: Claude gets
 * the instruction, does the work, finishes the turn, and the hook posts the summary that
 * lands in the inbox. A reply tool would be a second, racing copy of that. The one tool
 * here, `audiochatty_ack`, exists only for the handshake in (4) and must never grow into
 * a general-purpose way to send text back.
 *
 * ## The backend contract this is written against
 *
 * Frozen in `channel_return_path_plan.md` Phase 2 and repeated in `README.md`:
 *
 *     GET  /agent/inbound?session_id=<uuid>   → {"messages": [{id, text, sender_name,
 *                                                              created_at}, …]}
 *     POST /agent/inbound/ack                 ← {"message_ids": [...]}
 *     POST /agent/session/verified            ← {"claude_session_id": "..."}
 *
 * All three carry the device token as `Authorization: Bearer`, and an ended or unknown
 * session answers the poll with an empty list rather than a 404 — this process polls
 * forever and has no way to tell a real error from an expected one.
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { spawnSync } from 'node:child_process'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'

const VERSION = '0.1.0'

// -- the numbers ------------------------------------------------------------------------

/** While something could plausibly be in flight. */
const POLL_ACTIVE_MS = 5_000
/** Once it has been quiet. A message is only written when the caller hangs up, so half a
 *  minute of latency on a session nobody is talking to costs nothing anyone can feel. */
const POLL_IDLE_MS = 30_000
/** Empty polls before backing off — 6 × 5s, so a minute of silence. */
const IDLE_AFTER_EMPTY_POLLS = 6
/** After a failed request, skip the network entirely for this long. Same value and same
 *  reasoning as `BREAKER_COOLDOWN` in `audiochat.py`: one attempt pays the timeout, the
 *  next twelve pay nothing, and a Render service asleep for hours costs us twelve
 *  requests an hour instead of seven hundred. */
const BREAKER_COOLDOWN_MS = 60_000
/** Nothing here is interactive, but nothing here may hang either. */
const REQUEST_TIMEOUT_MS = 10_000
/** How long the handshake waits before its one retry. Long enough that a session busy
 *  with a real turn has finished it — events queue and are delivered together, so a probe
 *  sent mid-turn is read at the end of it. */
const PROBE_RETRY_MS = 90_000
/** Ids kept in the delivered ledger. Far more than a session will ever see; the cap is
 *  here so a long-lived session cannot grow the file without bound. */
const MAX_DELIVERED_IDS = 500
/** A single instruction, capped. Spoken text is short; this is a guard against a backend
 *  that has gone wrong, not a policy about how much someone can say. */
const MAX_CONTENT_CHARS = 20_000

// -- where state lives ------------------------------------------------------------------
//
// The same `~/.audiochatty` the CLI and the hooks use, with the same `AUDIOCHATTY_HOME`
// override, so the test suite can point all of it at a temp directory and a real
// developer's credentials are never touched.

function homeDir(): string {
  const override = process.env.AUDIOCHATTY_HOME
  const base = override && override.trim() ? override : path.join(os.homedir(), '.audiochatty')
  fs.mkdirSync(base, { recursive: true, mode: 0o700 })
  return base
}

function channelsDir(): string {
  const dir = path.join(homeDir(), 'channels')
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 })
  try {
    fs.chmodSync(dir, 0o700)
  } catch {
    /* an existing directory we cannot tighten is not worth failing over */
  }
  return dir
}

function rendezvousPath(): string {
  return path.join(channelsDir(), `${process.pid}.json`)
}

/**
 * The delivered-ids ledger, keyed by **Claude Code session, not pid**.
 *
 * A deliberate deviation from the plan's `<pid>.delivered.json`, and the reason is the
 * requirement it was attached to: "never inject an id twice even across a restart" (R6).
 * A restart is a new pid, so a pid-keyed ledger is empty exactly when it is needed. What
 * survives a restart is the session — the user relaunches `claude`, runs
 * `/audiochatty-connect`, and the same `claude_session_id` binds again — so that is the
 * key. The rendezvous file stays pid-keyed, because that one really is about a process.
 */
function deliveredPath(claudeSessionId: string): string {
  return path.join(channelsDir(), `${safeFilename(claudeSessionId)}.delivered.json`)
}

function credentialsPath(): string {
  return path.join(homeDir(), 'credentials.json')
}

/** A session id arrives over a loopback socket and is about to become a path. Keep it to
 *  characters that cannot escape the directory — the same rule, and the same 128, as
 *  `_safe_filename` in `audiochat.py`. */
function safeFilename(value: string): string {
  return [...String(value)]
    .filter(c => /[A-Za-z0-9\-_]/.test(c))
    .join('')
    .slice(0, 128)
}

function readJson(file: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'))
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
  } catch {
    return {}
  }
}

/**
 * Write 0600, atomically. `fs.writeFileSync` with `mode` creates the file with those
 * permissions rather than chmod-ing afterwards — on a shared machine that window is the
 * whole vulnerability — and the rename is atomic because the temp file is a sibling.
 */
function writePrivateJson(file: string, data: unknown): void {
  const tmp = `${file}.tmp`
  fs.writeFileSync(tmp, `${JSON.stringify(data, null, 2)}\n`, { mode: 0o600 })
  fs.renameSync(tmp, file)
}

// -- who spawned me ---------------------------------------------------------------------

type Ancestor = { pid: number; ppid: number; comm: string }

/**
 * This process's ancestry, innermost first.
 *
 * This is the whole of R4's correlation mechanism as far as *this* side is concerned:
 * `/audiochatty-connect` runs as a Bash subprocess of the same `claude`, computes its own
 * ancestry the same way, and binds to the channel whose chain shares that pid. This side
 * only has to record it honestly.
 *
 * One `ps` for the whole table rather than one per generation — this runs at startup, in
 * front of everything else, and a dozen process spawns is a dozen more than it needs.
 */
function ancestry(startPid: number): Ancestor[] {
  const table = processTable()
  const chain: Ancestor[] = []
  const seen = new Set<number>()
  let pid = startPid
  for (let depth = 0; depth < 32; depth++) {
    if (pid <= 1 || seen.has(pid)) break
    const row = table.get(pid)
    if (!row) break
    seen.add(pid)
    chain.push({ pid, ppid: row.ppid, comm: row.comm })
    pid = row.ppid
  }
  return chain
}

function processTable(): Map<number, { ppid: number; comm: string }> {
  const table = new Map<number, { ppid: number; comm: string }>()
  // `-Ao` is accepted by both BSD `ps` (macOS) and procps (Linux). `comm` is the full
  // executable path on macOS and the bare name on Linux; either is enough to recognise
  // `claude`, and the pid is what the match actually turns on.
  const out = spawnSync('ps', ['-Ao', 'pid=,ppid=,comm='], { encoding: 'utf8', timeout: 5_000 })
  if (out.status !== 0 || !out.stdout) return table
  for (const line of out.stdout.split('\n')) {
    const match = /^\s*(\d+)\s+(\d+)\s+(.*)$/.exec(line)
    if (!match) continue
    table.set(Number(match[1]), { ppid: Number(match[2]), comm: match[3].trim().slice(0, 256) })
  }
  return table
}

/**
 * The `CLAUDE_*` variables that reached this process, by allowlist.
 *
 * An allowlist rather than everything starting with `CLAUDE`, because this file is read by
 * another program and a blanket dump of the environment is how a secret ends up somewhere
 * nobody expected it. If `CLAUDE_SESSION_ID` turns out to be here, `connect` can match on
 * it directly and the ancestry walk becomes the fallback rather than the mechanism — which
 * is exactly what Phase 0's spike set out to learn, so record it either way.
 */
function claudeEnv(): Record<string, string> {
  const keys = [
    'CLAUDE_SESSION_ID',
    'CLAUDE_PID',
    'CLAUDECODE',
    'CLAUDE_PLUGIN_ROOT',
    'CLAUDE_PROJECT_DIR',
  ]
  const found: Record<string, string> = {}
  for (const key of keys) {
    const value = process.env[key]
    if (value) found[key] = String(value).slice(0, 256)
  }
  return found
}

// -- the binding ------------------------------------------------------------------------

type Binding = {
  agentSessionId: string
  claudeSessionId: string
  backendUrl: string
  token: string
  sessionName: string
}

let binding: Binding | null = null
/** Bumped on every bind and unbind. A poll loop whose generation is stale exits at its
 *  next tick, which is how unbind stops a loop that is asleep inside a 30s wait. */
let generation = 0
let verified = false
let verificationPending = false
let probeNonce: string | null = null
let delivered = new Set<string>()
/** Ids injected but not yet acked by the backend. Retried on the next poll. */
const unacked = new Set<string>()

function debug(message: string): void {
  if (process.env.AUDIOCHATTY_DEBUG) {
    // stderr, always. stdout is the MCP transport and a stray line there corrupts it.
    process.stderr.write(`[audiochatty-channel] ${message}\n`)
  }
}

function nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z')
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

// -- the rendezvous file ----------------------------------------------------------------

type Rendezvous = {
  version: number
  pid: number
  ppid: number
  port: number
  started_at: string
  ancestry: Ancestor[]
  claude_env: Record<string, string>
  bound: boolean
  verified: boolean
  claude_session_id: string | null
  agent_session_id: string | null
  session_name: string | null
  backend_url: string | null
  bound_at: string | null
  verified_at: string | null
}

let rendezvous: Rendezvous | null = null

/**
 * Announce this process. **The device token is deliberately not in here** — it lives in
 * `credentials.json` and has no business being copied into a second file. Everything here
 * is either public (a port, a pid) or already known to whoever can read the directory.
 */
function writeRendezvous(patch: Partial<Rendezvous> = {}): void {
  if (!rendezvous) return
  rendezvous = { ...rendezvous, ...patch }
  try {
    writePrivateJson(rendezvousPath(), rendezvous)
  } catch (err) {
    debug(`could not write rendezvous: ${err}`)
  }
}

/**
 * Delete rendezvous files belonging to processes that no longer exist.
 *
 * The normal exit path removes its own file, so this is for the abnormal one: a
 * `kill -9`, a laptop that slept through a crash. A stale file is how `connect` binds to a
 * corpse — it POSTs to a port nothing is listening on, or worse, to a port something else
 * has since been given.
 */
function pruneStaleRendezvous(): void {
  let entries: string[] = []
  try {
    entries = fs.readdirSync(channelsDir())
  } catch {
    return
  }
  for (const entry of entries) {
    if (!/^\d+\.json$/.test(entry)) continue
    const pid = Number(entry.slice(0, -5))
    if (pid === process.pid || isAlive(pid)) continue
    try {
      fs.unlinkSync(path.join(channelsDir(), entry))
      debug(`pruned stale rendezvous for dead pid ${pid}`)
    } catch {
      /* someone else got there first */
    }
  }
}

function isAlive(pid: number): boolean {
  try {
    // Signal 0 checks for existence without delivering anything.
    process.kill(pid, 0)
    return true
  } catch (err) {
    // EPERM means the process exists and belongs to somebody else — still alive.
    return (err as NodeJS.ErrnoException)?.code === 'EPERM'
  }
}

let cleanedUp = false

function cleanup(): void {
  if (cleanedUp) return
  cleanedUp = true
  try {
    fs.unlinkSync(rendezvousPath())
  } catch {
    /* already gone */
  }
}

// -- the delivered ledger (R6) ----------------------------------------------------------

function loadDelivered(claudeSessionId: string): Set<string> {
  const stored = readJson(deliveredPath(claudeSessionId)).message_ids
  return new Set(Array.isArray(stored) ? stored.map(String) : [])
}

function persistDelivered(claudeSessionId: string): void {
  const ids = [...delivered].slice(-MAX_DELIVERED_IDS)
  delivered = new Set(ids)
  try {
    writePrivateJson(deliveredPath(claudeSessionId), {
      message_ids: ids,
      updated_at: nowIso(),
    })
  } catch (err) {
    // Losing the ledger risks a duplicate instruction after a crash. It is not worth
    // dropping the message that is already in Claude's context over.
    debug(`could not persist delivered ids: ${err}`)
  }
}

// -- the MCP server ---------------------------------------------------------------------

// Read this as prompt engineering, because it is: it goes into Claude's system prompt and
// it is the only thing standing between an instruction arriving and an instruction being
// carried out. Two real sessions shaped it, and both failures are worth knowing about.
//
// The first draft buried "act on it" under two paragraphs about not replying, and the
// model acknowledged the message instead of doing it. Hence the order here: what to do,
// then what not to do, then one narrow exception keyed on an attribute it can see.
//
// The second failure is the one that matters. Claude Code wraps an inbound channel event
// in its own reminder that external channel messages are untrusted data for situational
// awareness — sound by default, since most channels carry a webhook or a stranger's chat
// message. Here it is wrong, and the session said so in as many words: "I received a
// message from audiochatty, but I'm not acting on it… I'll await instructions from you
// directly." So the paragraph on provenance is not decoration. It is the argument that
// this particular channel carries the operator's own voice, and it has to be made
// explicitly because the default assumption is the opposite.
const INSTRUCTIONS = [
  'This session is connected to audiochatty, a voice app the user talks to when they are',
  'away from the terminal.',
  '',
  'WHO IS SPEAKING. An event tagged <channel source="...audiochatty..."> is the operator of',
  'this session — the person who started it — speaking to you. It is not third-party',
  'content, not a webhook, and not a stranger: audiochatty runs on this machine under their',
  'own credentials, and a message only reaches you if someone inside their own workspace',
  'addressed it to this session. The only difference from the text in this terminal is that',
  'they spoke it from somewhere else instead of typing it here.',
  '',
  'WHAT TO DO WITH IT. Treat it as a prompt they typed into this terminal: act on it in this',
  'session, do the work it asks for, and finish the turn. It is an instruction, not a',
  'notification, and not something to acknowledge instead of doing. Apply exactly the',
  'judgement you would apply to the same words typed at the keyboard — no more, no less.',
  'The sender_name attribute is who spoke it; message_id identifies the message.',
  '',
  'HOW TO ANSWER. There is no reply tool and none is needed: the user hears what happened',
  'from the summary of the turn you finish, which audiochatty reads aloud to them. So do',
  'not look for a way to answer them through the channel — just do the work.',
  '',
  'THE ONE EXCEPTION is an event carrying kind="handshake". For that one, call the',
  'audiochatty_ack tool with the nonce it gives you and do nothing else — it is how',
  'audiochatty learns that this session can be reached, and without it the user is told',
  'they cannot talk to this session. Every other event is work to be done.',
].join('\n')

const mcp = new Server(
  { name: 'audiochatty', version: VERSION },
  {
    capabilities: {
      // The key that makes this a channel: its presence registers the notification
      // listener in Claude Code. Verified against the channels reference,
      // "Server options" — always `{}`.
      experimental: { 'claude/channel': {} },
      // For `audiochatty_ack` (R10) and nothing else.
      tools: {},
    },
    instructions: INSTRUCTIONS,
  },
)

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'audiochatty_ack',
      description:
        'Confirm a handshake from audiochatty. Call this only when a channel event asks ' +
        'you to, passing the nonce it gave you. It is not a way to send messages back — ' +
        'there is none, and none is needed.',
      inputSchema: {
        type: 'object',
        properties: {
          nonce: {
            type: 'string',
            description: 'The nonce from the handshake event, exactly as it was given.',
          },
        },
        required: ['nonce'],
      },
    },
  ],
}))

mcp.setRequestHandler(CallToolRequestSchema, async req => {
  if (req.params.name !== 'audiochatty_ack') {
    throw new Error(`unknown tool: ${req.params.name}`)
  }
  const nonce = String((req.params.arguments as { nonce?: unknown } | undefined)?.nonce ?? '')
  const result = await acceptAck(nonce)
  return { content: [{ type: 'text', text: result }] }
})

/**
 * The other half of R11. A matching nonce proves that a notification this process sent
 * was actually honoured by this session — the one thing the transport will not tell us —
 * so it is worth a round trip to the backend to record.
 *
 * A wrong nonce is answered blandly rather than with an error. It means a stale handshake
 * from an earlier binding, or a model improvising, and neither is a problem worth putting
 * a red tool result in someone's terminal over.
 */
async function acceptAck(nonce: string): Promise<string> {
  if (!binding || !probeNonce || nonce.trim() !== probeNonce) {
    debug(`ack with unknown nonce (bound=${Boolean(binding)})`)
    return 'ignored'
  }
  verified = true
  probeNonce = null
  writeRendezvous({ verified: true, verified_at: nowIso() })
  verificationPending = true
  // The handshake is also the starting gun for the poll: see `pollLoop`.
  void pollLoop(generation)
  const reported = await reportVerified(binding)
  return reported ? 'verified' : 'verified (audiochatty not reachable yet; will retry)'
}

/** `POST /agent/session/verified`, retried from the poll loop until it lands: the inbox
 *  reads this column, so a verification the backend never heard about is a session the
 *  user is told they cannot talk to. */
async function reportVerified(active: Binding): Promise<boolean> {
  try {
    await backendPost(active, '/agent/session/verified', {
      claude_session_id: active.claudeSessionId,
    })
    verificationPending = false
    debug('verification recorded with the backend')
    return true
  } catch (err) {
    debug(`could not record verification: ${err}`)
    return false
  }
}

// -- talking to the backend -------------------------------------------------------------

async function backendGet(active: Binding, pathAndQuery: string): Promise<unknown> {
  const response = await fetch(`${active.backendUrl}${pathAndQuery}`, {
    method: 'GET',
    headers: {
      Authorization: `Bearer ${active.token}`,
      'User-Agent': `audiochatty-channel/${VERSION}`,
    },
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return await response.json().catch(() => ({}))
}

async function backendPost(active: Binding, route: string, body: unknown): Promise<unknown> {
  const response = await fetch(`${active.backendUrl}${route}`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${active.token}`,
      'Content-Type': 'application/json',
      'User-Agent': `audiochatty-channel/${VERSION}`,
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return await response.json().catch(() => ({}))
}

type InboundMessage = { id: string; text: string; sender_name: string; created_at: string }

/** Normalise whatever came back into the four fields we use. A row without an id or
 *  without text is dropped rather than injected: it cannot be deduped and it has nothing
 *  to say. */
function parseInbound(payload: unknown): InboundMessage[] {
  const rows = Array.isArray(payload)
    ? payload
    : Array.isArray((payload as { messages?: unknown })?.messages)
      ? (payload as { messages: unknown[] }).messages
      : []
  const messages: InboundMessage[] = []
  for (const row of rows) {
    if (!row || typeof row !== 'object') continue
    const record = row as Record<string, unknown>
    const id = String(record.id ?? '')
    const text = String(record.text ?? '').slice(0, MAX_CONTENT_CHARS)
    if (!id || !text.trim()) continue
    messages.push({
      id,
      text,
      sender_name: String(record.sender_name ?? '').slice(0, 128),
      created_at: String(record.created_at ?? '').slice(0, 64),
    })
  }
  return messages
}

// -- the poll loop (R5) -----------------------------------------------------------------

/**
 * Ask for messages, forever, until the binding ends.
 *
 * **Started by the handshake, not by the bind**, and that ordering was bought with a real
 * session. The first draft polled from the moment it was bound, so on a session with an
 * instruction already waiting — the ordinary case, since an instruction spoken to a
 * session that has since restarted is delivered when it reconnects — the handshake event
 * and the instruction arrived in the same batch. Claude Code queues events and hands them
 * to the model together, the model answered the handshake, and the instruction went
 * unread. It read as one notification with two paragraphs, because that is what it was.
 *
 * Waiting for the ack fixes it structurally rather than by asking the prompt to be
 * cleverer, and it buys a second thing worth more: **a message is never injected into a
 * session that has not proven it can receive one.** An unverified channel asks the backend
 * for nothing, so an instruction stays undelivered and waits for a session that can act on
 * it, instead of vanishing into a session whose notifications are being dropped.
 */
async function pollLoop(mine: number): Promise<void> {
  let emptyPolls = 0
  debug('poll loop started')

  while (binding && mine === generation) {
    const active = binding
    let delay = emptyPolls >= IDLE_AFTER_EMPTY_POLLS ? POLL_IDLE_MS : POLL_ACTIVE_MS

    try {
      const payload = await backendGet(
        active,
        `/agent/inbound?session_id=${encodeURIComponent(active.agentSessionId)}`,
      )
      const messages = parseInbound(payload)
      if (messages.length) {
        emptyPolls = 0
        await deliver(active, messages)
      } else {
        emptyPolls += 1
        if (unacked.size) await ack(active, [...unacked])
      }
      if (verificationPending) await reportVerified(active)
    } catch (err) {
      // Every failure is the same failure from here: the backend did not answer, and
      // nothing about that is the terminal's problem. Sit out a minute.
      debug(`poll failed (${err}); backing off ${BREAKER_COOLDOWN_MS}ms`)
      delay = BREAKER_COOLDOWN_MS
      emptyPolls = 0
    }

    await sleep(delay)
  }

  debug('poll loop stopped')
}

/**
 * Inject, remember, then ack — in that order, and the order is R6.
 *
 * Writing the ledger before the ack means a crash in the gap replays into a dedupe; the
 * reverse would mean a crash in the gap replays into a duplicated instruction, and the
 * whole point of this feature is that an instruction changes files on someone's machine.
 */
async function deliver(active: Binding, messages: InboundMessage[]): Promise<void> {
  const injected: string[] = []

  for (const message of messages) {
    if (delivered.has(message.id)) continue
    try {
      await mcp.notification({
        method: 'notifications/claude/channel',
        params: {
          content: message.text,
          // Every key here is an identifier: letters, digits and underscores. A key with
          // a hyphen in it is dropped silently by Claude Code, which is a bug you only
          // find by noticing the attribute missing from the tag.
          meta: {
            message_id: message.id,
            sender_name: message.sender_name,
            sent_at: message.created_at,
          },
        },
      })
    } catch (err) {
      // The transport is gone, which means the session is gone. Stop; the ids not yet
      // recorded stay undelivered at the backend and arrive on the next binding.
      debug(`injection failed: ${err}`)
      break
    }
    delivered.add(message.id)
    unacked.add(message.id)
    injected.push(message.id)
  }

  if (injected.length) {
    persistDelivered(active.claudeSessionId)
    debug(`injected ${injected.length} message(s)`)
  }

  // Anything the backend served that we have already seen means an ack that never
  // landed — say so again rather than letting it come back forever.
  const toAck = messages.filter(m => delivered.has(m.id)).map(m => m.id)
  for (const id of toAck) unacked.add(id)
  if (unacked.size) await ack(active, [...unacked])
}

async function ack(active: Binding, ids: string[]): Promise<void> {
  try {
    await backendPost(active, '/agent/inbound/ack', { message_ids: ids })
    for (const id of ids) unacked.delete(id)
    debug(`acked ${ids.length} message(s)`)
  } catch (err) {
    // Left in `unacked` and retried next poll. The backend keeps serving them until it
    // hears, and the ledger keeps them from being injected twice in the meantime.
    debug(`ack failed: ${err}`)
  }
}

// -- the handshake (R11) ----------------------------------------------------------------

function newNonce(): string {
  const bytes = new Uint8Array(5)
  crypto.getRandomValues(bytes)
  return [...bytes].map(b => b.toString(16).padStart(2, '0')).join('')
}

/**
 * One handshake, one retry, then give up and stay unverified.
 *
 * The same nonce both times on purpose: a retry with a fresh nonce would invalidate an
 * ack that was already on its way, and turn a slow session into a permanently unverified
 * one. Giving up quietly is the right end state — the failure is reported where the user
 * is standing when it matters, which is the audiochatty inbox, not here.
 */
async function probe(mine: number): Promise<void> {
  probeNonce = newNonce()
  for (let attempt = 0; attempt < 2; attempt++) {
    if (attempt > 0) await sleep(PROBE_RETRY_MS)
    if (!binding || mine !== generation || verified) return
    try {
      await mcp.notification({
        method: 'notifications/claude/channel',
        params: {
          content:
            'audiochatty handshake: call the audiochatty_ack tool with nonce ' +
            `"${probeNonce}" and nothing else. This confirms that instructions spoken ` +
            'into audiochatty can reach this session. No other action is needed and ' +
            'there is nothing to report back to the user.',
          meta: { kind: 'handshake', nonce: probeNonce ?? '' },
        },
      })
      debug(`handshake sent (attempt ${attempt + 1})`)
    } catch (err) {
      debug(`handshake could not be sent: ${err}`)
      return
    }
  }
}

// -- the local rendezvous port (R4) -----------------------------------------------------

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/**
 * `POST /bind` — the only way this process starts doing anything.
 *
 * Two checks, and both matter. **The token must match this machine's credentials file**,
 * so another local process — a stray script, something that wandered in — cannot point
 * this channel at a session it does not own. And **a second bind is refused**, because a
 * channel that could be rebound is a channel that could be redirected mid-session.
 */
async function handleBind(request: Request): Promise<Response> {
  let body: Record<string, unknown>
  try {
    body = (await request.json()) as Record<string, unknown>
  } catch {
    return jsonResponse(400, { error: 'invalid_json' })
  }

  const agentSessionId = String(body.agent_session_id ?? '').trim()
  const claudeSessionId = String(body.claude_session_id ?? '').trim()
  const backendUrl = String(body.backend_url ?? '')
    .trim()
    .replace(/\/+$/, '')
  const token = String(body.token ?? '')

  if (!agentSessionId || !claudeSessionId || !backendUrl || !token) {
    return jsonResponse(400, { error: 'missing_fields' })
  }

  const stored = String(readJson(credentialsPath()).token ?? '')
  if (!stored || stored !== token) {
    debug('bind refused: token does not match this machine')
    return jsonResponse(403, { error: 'token_mismatch' })
  }

  if (binding && binding.claudeSessionId !== claudeSessionId) {
    // A channel that can be redirected mid-session is a channel that can put one
    // person's instruction in another person's terminal. This is the refusal R4 is for.
    return jsonResponse(409, {
      error: 'already_bound',
      claude_session_id: binding.claudeSessionId,
      verified,
    })
  }

  if (binding) {
    // The *same* session binding again is not that: it is `/audiochatty-connect` run
    // twice in the same terminal, or run again after re-pairing, in which case the token
    // it carries is newer than the one in memory. Refresh in place and say so. The
    // delivered ledger is untouched — same session, same conversation.
    const wasVerified = verified
    binding = { ...binding, agentSessionId, backendUrl, token, sessionName: String(body.session_name ?? '').slice(0, 128) }
    writeRendezvous({
      agent_session_id: agentSessionId,
      backend_url: backendUrl,
      session_name: binding.sessionName || null,
      bound_at: nowIso(),
    })
    if (!wasVerified) {
      // The previous handshake went unanswered. Bumping the generation retires it before
      // starting another, so there is only ever one live nonce.
      generation += 1
      void probe(generation)
    }
    debug(`rebound the same session (verified=${wasVerified})`)
    return jsonResponse(200, { status: 'rebound', claude_session_id: claudeSessionId, verified: wasVerified })
  }

  binding = {
    agentSessionId,
    claudeSessionId,
    backendUrl,
    token,
    sessionName: String(body.session_name ?? '').slice(0, 128),
  }
  generation += 1
  verified = false
  verificationPending = false
  delivered = loadDelivered(claudeSessionId)
  unacked.clear()

  writeRendezvous({
    bound: true,
    bound_at: nowIso(),
    verified: false,
    verified_at: null,
    claude_session_id: claudeSessionId,
    agent_session_id: agentSessionId,
    session_name: binding.sessionName || null,
    backend_url: backendUrl,
  })

  const mine = generation
  debug(`bound to session ${claudeSessionId} (${delivered.size} id(s) already delivered)`)
  // The handshake, and only the handshake. Polling starts when it is answered — see
  // `pollLoop`. Not awaited: the bind response is what `/audiochatty-connect` is waiting
  // on, and it should not wait for a round trip through the model.
  void probe(mine)

  return jsonResponse(200, {
    status: 'bound',
    pid: process.pid,
    claude_session_id: claudeSessionId,
    agent_session_id: agentSessionId,
  })
}

/**
 * `POST /unbind` — `/audiochatty-disconnect`'s half of the same handshake.
 *
 * Bumping the generation is what stops a poll loop that is currently asleep inside a 30s
 * wait: it wakes, sees a number that is not its own, and exits. The delivered ledger is
 * left on disk deliberately — the session may reconnect, and re-injecting an instruction
 * it already acted on is the one failure this whole file is arranged to prevent.
 */
async function handleUnbind(request: Request): Promise<Response> {
  let body: Record<string, unknown> = {}
  try {
    body = (await request.json()) as Record<string, unknown>
  } catch {
    /* an empty body is fine here */
  }

  const stored = String(readJson(credentialsPath()).token ?? '')
  const token = String(body.token ?? '')
  if (stored && token !== stored) {
    return jsonResponse(403, { error: 'token_mismatch' })
  }

  const was = binding?.claudeSessionId ?? null
  binding = null
  generation += 1
  verified = false
  verificationPending = false
  probeNonce = null
  unacked.clear()
  writeRendezvous({
    bound: false,
    bound_at: null,
    verified: false,
    verified_at: null,
    claude_session_id: null,
    agent_session_id: null,
    session_name: null,
    backend_url: null,
  })
  debug(`unbound from ${was ?? 'nothing'}`)
  return jsonResponse(200, { status: 'unbound', claude_session_id: was })
}

function startRendezvousServer() {
  return Bun.serve({
    // An ephemeral port: the file is how anyone finds it, so there is no fixed number to
    // collide with when fifteen of these are running.
    port: 0,
    // Loopback only. Nothing outside this machine can reach it, whatever the network
    // thinks it is doing.
    hostname: '127.0.0.1',
    async fetch(request) {
      const url = new URL(request.url)
      if (request.method === 'POST' && url.pathname === '/bind') return handleBind(request)
      if (request.method === 'POST' && url.pathname === '/unbind') return handleUnbind(request)
      if (request.method === 'GET' && url.pathname === '/status') {
        // Cheap enough to be worth having when someone is debugging a channel that is
        // running but silent. It says nothing a reader of the rendezvous file cannot
        // already see, and in particular says nothing about the token.
        return jsonResponse(200, {
          pid: process.pid,
          bound: Boolean(binding),
          verified,
          claude_session_id: binding?.claudeSessionId ?? null,
          agent_session_id: binding?.agentSessionId ?? null,
        })
      }
      return jsonResponse(404, { error: 'not_found' })
    },
  })
}

// -- startup ----------------------------------------------------------------------------

async function main(): Promise<void> {
  pruneStaleRendezvous()

  const http = startRendezvousServer()
  rendezvous = {
    version: 1,
    pid: process.pid,
    ppid: process.ppid,
    port: http.port,
    started_at: nowIso(),
    ancestry: ancestry(process.pid),
    claude_env: claudeEnv(),
    bound: false,
    verified: false,
    claude_session_id: null,
    agent_session_id: null,
    session_name: null,
    backend_url: null,
    bound_at: null,
    verified_at: null,
  }
  writeRendezvous()
  debug(`listening on 127.0.0.1:${http.port}; rendezvous at ${rendezvousPath()}`)

  // A rendezvous file for a process that has exited is worse than no file at all, so
  // every way out of here goes through `cleanup()`.
  for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP'] as const) {
    process.on(signal, () => {
      cleanup()
      process.exit(0)
    })
  }
  process.on('exit', cleanup)
  process.on('uncaughtException', err => {
    debug(`unhandled: ${err}`)
    cleanup()
    process.exit(1)
  })
  process.on('unhandledRejection', err => {
    debug(`unhandled rejection: ${err}`)
  })

  await mcp.connect(new StdioServerTransport())
  // Claude Code closing the transport is Claude Code exiting. Take the file with us.
  mcp.onclose = () => {
    cleanup()
    process.exit(0)
  }
  debug('connected over stdio; idle until bound')
}

await main()
