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
no longer receives anything from it; the machine is still paired and the session is still
running inside its wrapper, so `/audiochatty-connect` will start both again without needing
a restart.

**It stays disconnected until the user asks for it back.** Connecting is automatic now, and
`/clear` re-runs the thing that does it — so this command leaves a record of the decision,
and nothing automatic will undo it. Only running `/audiochatty-connect` reconnects.
