---
description: Reconnect or rename this session in audiochatty (sessions started with `audiochatty run` connect themselves)
argument-hint: [name]
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" *)
---

# Reconnecting this session to audiochatty

```!
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" connect --session-id "${CLAUDE_SESSION_ID}" "$ARGUMENTS"
```

Relay the output above to the user. Everything it describes already happened — this is a
report, not a task. If it printed a command to start Claude Code with, print that command
exactly as it is rather than paraphrasing it.

There is nothing else for you to do here. Connecting also opens the return path, and that
needs no acknowledgement from you: the process that types in what the user says is the one
that started this session, so reaching this terminal is not something it has to prove.

**Most sessions never need this.** A session started with `audiochatty run` connects itself
at launch, silently, so the ordinary answer above is "already connected". It is worth running
for one of three reasons: audiochatty was unreachable at launch and the connect failed, the
session should be renamed, or it was disconnected earlier and should come back.
