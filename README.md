# audiochatty for Claude Code

Turn a Claude Code session into a contact in [audiochatty](https://github.com/tmshapland).
When a session you've opted in finishes a turn, a short spoken summary of what it did shows
up in your audiochatty inbox. You listen to it and you can ask follow-up questions out loud.

The flow is **one-directional: your laptop → audiochatty.** Nothing is ever pushed back into
your session, and nothing this plugin installs can type into your terminal.

---

## What this actually does to your machine

This is short on purpose. Read it before you install, and read it again after — everything
below is in this repo, in the files it names.

| | |
| --- | --- |
| **Runtime** | Python 3, standard library only. No pip, no venv, no node, no build step. |
| **Background processes** | None. The hooks are short-lived processes Claude Code spawns, waits on, and discards. |
| **On disk** | `~/.audiochatty/` (mode 0700): a credentials file (0600) with your device token, and one small marker file per registered session. Nothing else. |
| **Network** | POSTs to your audiochatty backend, from `scripts/audiochat.py`. Nowhere else. |
| **When it's off** | Until you run `/audiochatty-login`, the plugin is inert. Until you run `/audiochatty-connect` in a session, that session sends nothing. |

---

## Install

```bash
claude plugin marketplace add tmshapland/audiochatty-plugin
claude plugin install audiochatty@audiochatty
```

Then, once per machine:

```
> /audiochatty-login

  Open https://audiochatty.app/link and enter this code:

        WXYZ-1234

  It expires in 10 minutes.
  Once you've entered it, run /audiochatty-login again to finish.
```

Open audiochatty in a browser — you're already signed in — go to **Settings → Link a coding
agent**, and type the code. Then run `/audiochatty-login` again:

```
> /audiochatty-login
  Linked to Mike's Workspace as Mike.
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

## Use

```
> /audiochatty-connect billing-refactor
  This session is now "billing-refactor" in audiochatty.
```

That's it. Work normally — the terminal looks no different — and each completed turn shows
up in your inbox under that name.

| Command | What it does |
| --- | --- |
| `/audiochatty-login` | Pair this machine. Once per machine. |
| `/audiochatty-connect [name]` | Register *this* session. The name defaults to the folder name. |
| `/audiochatty-status` | Is this machine paired, is this session registered, under what name. Entirely local — no network call. |
| `/audiochatty-disconnect` | Stop sending this session. The machine stays paired. |

Every other terminal you have open does nothing at all: the `Stop` hook is global, so it
runs everywhere, looks for a marker file for that session, finds none, and exits.

To turn everything off: `claude plugin disable audiochatty`. To remove it:
`claude plugin uninstall audiochatty` and `rm -rf ~/.audiochatty`. To kill a machine you no
longer have — a stolen laptop, an old work machine — revoke its token from **Settings →
Linked devices** in audiochatty; the next thing it sends gets a 401.

---

## Exactly what the hook sends

Once per completed turn, in a registered session, `scripts/stop_hook.py` POSTs this and
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
that isn't acceptable for your work, don't register those sessions.

`SessionEnd` sends only `{"claude_session_id": …}`, and only when a session has genuinely
ended — never on `/clear` or `/resume`, which keep the same session id and the same open
terminal.

---

## When something isn't working

Start here:

```
> /audiochatty-status
```

Then, in order of likelihood:

- **The session was never registered.** `/audiochatty-connect` is per session, not per
  machine, and a new terminal is a new session.
- **The backend is asleep or down.** Turns are dropped silently — that is deliberate, since
  a hook that waits on the network is a hook you feel on every turn. After one failure the
  plugin skips the network entirely for 60 seconds rather than paying the timeout again.
  Nothing is retried; a turn that couldn't be delivered is gone.
- **The device was revoked.** Everything gets a 401 and stays silent. Run `/audiochatty-login`
  to pair again.

To see what the hook is actually deciding:

```bash
echo '{"session_id":"<your-session-id>","last_assistant_message":"test"}' \
  | AUDIOCHATTY_DEBUG=1 python3 ~/.claude/plugins/.../scripts/stop_hook.py
```

`AUDIOCHATTY_DEBUG=1` puts one line on stderr per hook run and changes nothing else.

### Pointing at a different backend

```bash
AUDIOCHATTY_BACKEND_URL=http://localhost:8000 python3 scripts/audiochat.py login
```

`--backend-url` does the same for one command. Once paired, the URL you paired against is
remembered in `~/.audiochatty/credentials.json`, because a device token is only valid at the
backend that minted it.

---

## Development

```bash
python3 -m unittest discover -s tests     # 53 tests, no network, no Claude Code needed
claude plugin validate .                  # manifests
```

The tests run every script as a subprocess against a stub HTTP server, with
`AUDIOCHATTY_HOME` pointed at a temp directory — so they're safe to run on a machine that is
already paired, and they assert on what was actually sent over the wire.

Two things worth knowing if you change this code:

- **`scripts/stop_hook.py` must never be slow and must never raise.** It runs at the end of
  every turn in every session on the machine. The marker-file check comes first for that
  reason, and there's a test that fails if an unreachable backend costs more than one
  timeout.
- **A session name containing a double quote won't survive** the shell line in
  `commands/audiochatty-connect.md`, since `$ARGUMENTS` is substituted as text. Use plain
  names.

## License

MIT. See [LICENSE](LICENSE).
