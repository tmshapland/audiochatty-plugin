---
description: Connect this Claude Code session to audiochatty, both directions
argument-hint: [name]
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" *), mcp__audiochatty__audiochatty_ack
---

# Connecting this session to audiochatty

```!
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" connect --session-id "${CLAUDE_SESSION_ID}" "$ARGUMENTS"
```

Relay the output above to the user. Everything it describes already happened — this is a
report, not a task. If it printed a command to relaunch with, print that command exactly
as it is rather than paraphrasing it.

One thing here may need an action from you. Connecting also opens the return path, so a
`<channel source="...audiochatty...">` event carrying `kind="handshake"` usually arrives in
this same turn. Answer that one — call `audiochatty_ack` with the nonce it gives you — and
then stop. The ack is how audiochatty learns this session can be reached; without it the
user is told they can't talk to it.
