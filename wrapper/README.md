# `audiochatty run` — the wrapper

Start Claude Code with this instead of `claude`, and instructions you speak into audiochatty
from your phone get typed into that session for you.

```
you, away from your desk                  this laptop
─────────────────────────                 ────────────────────────────────────────
  hear what it did       ◀── audiochatty ◀── scripts/stop_hook.py ◀─ the turn it just finished
  say what to do next    ──▶ audiochatty ──▶ audiochatty run     ──▶ typed into your session
```

Claude Code has no idea this exists. There is no plugin to load, no flag to pass, and no
warning dialog — from the session's point of view, somebody typed something.

---

## Read this part before you use it

**This process can type anything into your Claude Code session, and Claude Code will act on
it.** That is not a side effect of the design; it *is* the design. Concretely, while a
session is connected:

| | |
| --- | --- |
| **What it can do** | Type any text into your session, as if you had typed it. Claude Code treats it exactly like your own keyboard input — which means it can edit files, run commands, and install things, subject to whatever permissions you have already granted that session. |
| **What that means for prompts** | It types into a live terminal, so text it sends can land on a permission prompt or any other keypress-driven UI. Answering those by voice is a deliberate, separate, still-unbuilt feature (`wrapper_return_path_plan.md` Phase 9) — but the *capability* is inherent to typing into a terminal and exists the moment you use this. |
| **Who can make it type** | A local process that (a) can reach a loopback port on this machine and (b) can read your `~/.audiochatty/credentials.json`. On a single-user laptop that is you and anything you run. There is no remote path in: the wrapper never accepts an inbound connection from off the machine. |
| **What reaches it from outside** | Only messages your own audiochatty workspace addressed to *this* session, fetched by the wrapper asking the backend — never pushed. |
| **When it's off** | On a machine you have not paired, the wrapper is inert: it proxies your terminal and does nothing else. No polling, no network, and `/inject` refuses. On a paired machine it connects itself at launch, so it is *on* from the moment you run it — `/audiochatty-disconnect` is how you turn one session off again. |
| **On disk** | `~/.audiochatty/wrappers/<pid>.json`, mode 0600, deleted when the wrapper exits — plus, once a session has been delivered to, `<claude-session-id>.delivered.json` beside it, which is the list of instruction ids already typed in and outlives the process on purpose. Your device token is in neither; the rendezvous file does carry the reason a launch connect failed, so `/audiochatty-status` can explain a session that never appeared. |
| **Platform** | macOS and Linux. The pseudo-terminal this is built on does not exist on Windows. |

The old version of this — a Claude Code plugin — could only *push a notification* into a
session. This can type. That is a real increase in what a bug or a compromised backend could
do. (The app used to carry a disclosure paragraph saying so on `/link`; 👤 removed it on
2026-08-03, so this README is now the only place it's written down.)

---

## Using it

```bash
# instead of: claude
python3 /path/to/audiochat-plugin/wrapper/__main__.py run
```

Everything after `run` is passed to `claude` unchanged, so `... run --model opus` and
`... run -- --resume` work the way you would expect.

To make `audiochatty run` an actual command, either put `wrapper/audiochatty` on your `PATH`
or alias it:

```bash
# ~/.zshrc
alias audiochatty="/path/to/audiochat-plugin/wrapper/audiochatty"
```

That is the whole of it. On a paired machine the wrapper registers the session it starts and
binds itself, so both halves are live from launch with nothing typed into the session
(`wrapper_return_path_plan.md` W13). `--name laptop` chooses how it appears in your inbox;
the default is the current folder's name.

It says nothing about any of that, deliberately — stdout belongs to Claude Code's interface,
and a line written there is a corrupted screen. `/audiochatty-status`, inside the session, is
where you find out whether it worked and why not.

**Requires Python 3 and nothing else.** No pip, no venv, no build step — the same promise as
the rest of `scripts/`, which the previous Bun-based version had broken.

### Options

| | |
| --- | --- |
| `--quiet-period SECONDS` | How long you have to have stopped typing before an instruction is typed in. Default 1.5. |
| `--claude-bin PATH` | The program to wrap. Exists for the test suite. |
| `--name NAME` | What to call this session in audiochatty. Defaults to the current folder's name. |
| `--verbose` | Print the port and rendezvous path to stderr at startup, then narrate what the wrapper is doing for the rest of the run — same output as `AUDIOCHATTY_DEBUG=1` below, folded into one flag. Off by default, because a wrapped session should look exactly like an unwrapped one. |
| `AUDIOCHATTY_DEBUG=1` | The env-var form of the narration half of `--verbose` (no startup line). Kept for cases a CLI flag can't reach — a test harness, a subprocess not launched via `audiochatty run` directly. |
| `AUDIOCHATTY_HOME` | Move `~/.audiochatty` elsewhere. Used by the tests. |
| `AUDIOCHATTY_POLL_*` | `ACTIVE`, `IDLE`, `COOLDOWN`, `TIMEOUT` — the poll cadences in seconds. Undocumented as options for the same reason `--claude-bin` is: the defaults were reasoned about, and the tests are the only caller that needs a compressed version of them. |

---

## How it works, briefly

1. It opens a pseudo-terminal — a terminal device only this process controls.
2. It starts the real `claude` inside it, with `AUDIOCHATTY_WRAPPER_PORT` in its environment.
3. It sits between your keyboard and that terminal, passing bytes both ways. Raw mode, window
   resizes, Ctrl-C, and the child's exit code all pass through, which is why a wrapped session
   feels like an unwrapped one.
4. It connects that session — registers it, binds itself, tells the backend it is reachable —
   on a background thread, so a slow backend never delays the terminal.
5. From then on it asks the backend for anything you have spoken, and types it in.

Step 5 is a poll, not a push: `GET /agent/inbound` every 5 seconds while something could
plausibly be in flight, every 30 once it has been quiet, and not at all for a minute after a
failed request. A laptop behind NAT cannot be pushed to, and a backend asleep on a free tier
must not turn into a hot loop.

Delivery is **at least once**, and the thing that makes it effectively exactly once is
`~/.audiochatty/wrappers/<claude-session-id>.delivered.json` — a list of the instructions
already typed into this session. It is keyed by session rather than by process precisely so
that it survives a restart, which is the one moment it matters: relaunch, reconnect, and an
instruction the backend never heard the acknowledgement for is skipped rather than typed a
second time. The order is always *type it in → write that file → tell the backend*, because
a crash in the first gap would duplicate an instruction and a crash in the second only
duplicates a dedupe.

Two behaviours are worth knowing because they look like bugs and are not:

- **An instruction can take a moment to appear.** The wrapper will not type over you: if you
  are mid-sentence when something arrives, it waits until you have paused (`--quiet-period`).
  This is the main defence against a spoken instruction getting spliced into a half-written
  line, and it is the one thing `tmux send-keys` fundamentally cannot do, since it cannot see
  your keystrokes.
- **It arrives as a paste, then one Enter.** A spoken instruction routinely contains several
  paragraphs. Typed raw, the prompt would submit at the first newline and treat the rest as a
  second instruction, so it goes in wrapped in the terminal's bracketed-paste markers
  instead. Escape sequences are stripped from the text before it is sent, so nothing inside a
  message can end the paste early and start issuing keystrokes of its own.

The local API — `/bind`, `/unbind`, `/status`, `/inject`, the rendezvous file's fields, and
the environment variables — is frozen and documented in `__main__.py`'s docstring.
`scripts/audiochat.py` is written against that and nothing else.

---

## Confirmed against the real Claude Code interface

The test suite (`tests/test_wrapper.py`) drives the real wrapper with a real pseudo-terminal
and a fake `claude`, so it proves the mechanism. Two things it structurally cannot prove,
because they are questions about Claude Code's own prompt rather than about the terminal —
both since answered by hand, 2026-07-29 (`wrapper_return_path_plan.md` Phase 0, W5 and W7):

- **a bracketed paste arrives as one multi-line message**, line breaks intact. So the paste
  format here is the real one, and the single-line-flattening fallback was never needed.
- **an instruction typed while Claude Code is mid-turn is queued**, and acted on when the
  turn finishes. Typed while idle, it is acted on immediately. So the wrapper needs no
  turn-boundary logic of its own.

A third question turned out to be hiding behind the first, and the hand-check missed it —
**the Enter has to be its own write, `inject.SUBMIT_DELAY` after the paste.** Sent in the same
write as the closing marker, Claude Code's TUI swallows it: the instruction appears in the
prompt box, complete and correct, and sits there until a human presses Return. It is a race,
not a rule, which is why a spike passed — measured against 2.1.221 on 2026-08-04, 4 of 6
single-line instructions submitted and 2 hung, while the multi-line ones a person would
naturally test with submitted every time. A separate `os.write` is not enough on its own (the
bytes coalesce into one pty read); the gap is what does the work. 30ms sufficed in every
trial, 150ms ships, and `tests/test_wrapper.py::TestSubmitIsASeparateWrite` pins it.
