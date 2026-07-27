---
description: Register this Claude Code session as a contact in AudioChat
argument-hint: [name]
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" *)
---

# Registering this session with AudioChat

```!
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" connect --session-id "${CLAUDE_SESSION_ID}" "$ARGUMENTS"
```

Relay the one line above to the user and stop. Registration already happened — this is a
report, not a task.
