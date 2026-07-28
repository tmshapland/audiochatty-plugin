---
description: Disconnect this Claude Code session from audiochatty, both directions
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" *)
---

# Disconnecting this session from audiochatty

```!
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" disconnect --session-id "${CLAUDE_SESSION_ID}"
```

Relay the one line above and stop. This session no longer sends anything to audiochatty and
no longer receives anything from it; the machine is still paired, so `/audiochatty-connect`
will start both again.
