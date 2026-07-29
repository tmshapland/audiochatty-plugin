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
| **When it's off** | Until `/audiochatty-connect` runs in the session, the wrapper is inert: it proxies your terminal and does nothing else. No polling, no network, and `/inject` refuses. |
| **On disk** | One file: `~/.audiochatty/wrappers/<pid>.json`, mode 0600, deleted when the wrapper exits. Your device token is **not** in it. |
| **Platform** | macOS and Linux. The pseudo-terminal this is built on does not exist on Windows. |

The old version of this — a Claude Code plugin — could only *push a notification* into a
session. This can type. That is a real increase in what a bug or a compromised backend could
do, and it is why the disclosure text in the app says so too.

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

Then, in the session it starts:

```
> /audiochatty-connect laptop
```

That is what connects the two halves. Without it the wrapper types nothing.

**Requires Python 3 and nothing else.** No pip, no venv, no build step — the same promise as
the rest of `scripts/`, which the previous Bun-based version had broken.

### Options

| | |
| --- | --- |
| `--quiet-period SECONDS` | How long you have to have stopped typing before an instruction is typed in. Default 1.5. |
| `--claude-bin PATH` | The program to wrap. Exists for the test suite. |
| `--verbose` | Print the port and rendezvous path to stderr at startup. Off by default, because a wrapped session should look exactly like an unwrapped one. |
| `AUDIOCHATTY_DEBUG=1` | Narrate what the wrapper is doing, on stderr. |
| `AUDIOCHATTY_HOME` | Move `~/.audiochatty` elsewhere. Used by the tests. |

---

## How it works, briefly

1. It opens a pseudo-terminal — a terminal device only this process controls.
2. It starts the real `claude` inside it, with `AUDIOCHATTY_WRAPPER_PORT` in its environment.
3. It sits between your keyboard and that terminal, passing bytes both ways. Raw mode, window
   resizes, Ctrl-C, and the child's exit code all pass through, which is why a wrapped session
   feels like an unwrapped one.
4. Once connected, it asks the backend for anything you have spoken, and types it in.

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

## Not yet confirmed against the real Claude Code interface

The test suite (`tests/test_wrapper.py`) drives the real wrapper with a real pseudo-terminal
and a fake `claude`, so it proves the mechanism. Two things it cannot prove, because they are
questions about Claude Code's own prompt rather than about the terminal:

- **that a bracketed paste arrives as one multi-line message** rather than as several. If it
  does not, the fallback is to flatten each instruction onto a single line, which loses its
  paragraph breaks.
- **that an instruction typed while Claude Code is mid-turn is queued** rather than dropped.

Both are `wrapper_return_path_plan.md` Phase 0 spikes (W5 and W7) and need a person at a
keyboard. Until they are signed off, treat the paste format as provisional.
