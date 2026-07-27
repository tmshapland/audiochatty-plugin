---
description: Stop sending this Claude Code session to AudioChat
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" *)
---

# Disconnecting this session from AudioChat

```!
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" disconnect --session-id "${CLAUDE_SESSION_ID}"
```

Relay the one line above and stop. This session no longer sends anything to AudioChat;
the machine is still paired, so `/audiochat-connect` will start it again.
