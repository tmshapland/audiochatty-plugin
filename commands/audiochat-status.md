---
description: Show whether this machine and this session are connected to AudioChat
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" *)
---

# AudioChat status

```!
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" status --session-id "${CLAUDE_SESSION_ID}"
```

Relay the above to the user verbatim. It is entirely local — no network call was made and
nothing was changed.
