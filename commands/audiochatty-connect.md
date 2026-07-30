---
description: Connect this Claude Code session to audiochatty, both directions
argument-hint: [name]
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" *)
---

# Connecting this session to audiochatty

```!
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" connect --session-id "${CLAUDE_SESSION_ID}" "$ARGUMENTS"
```

Relay the output above to the user. Everything it describes already happened — this is a
report, not a task. If it printed a command to start Claude Code with, print that command
exactly as it is rather than paraphrasing it.

There is nothing else for you to do here. Connecting also opens the return path, and that
needs no acknowledgement from you: the process that types in what the user says is the one
that started this session, so reaching this terminal is not something it has to prove.
