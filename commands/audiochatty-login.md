---
description: Pair this machine with audiochatty
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" *)
---

# Pairing this machine with audiochatty

```!
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" login
```

Show the user exactly what the command printed — if it printed a pairing code, the code
and the link matter more than anything you might add around them, and if it printed a
command to start Claude Code with (or a line to add to their shell profile), print those
exactly as they are. Say nothing else.
