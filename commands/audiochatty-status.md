---
description: Show whether this machine and this session are connected to audiochatty
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" *)
---

# audiochatty status

```!
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" status --session-id "${CLAUDE_SESSION_ID}"
```

Relay the above to the user verbatim, including any command it prints. It is entirely local
— no network call was made and nothing was changed. This is the command someone runs when
audiochatty says a session can't be talked to, so the diagnosis it prints is the point of
it; don't summarise it away.
