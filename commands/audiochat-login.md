---
description: Pair this machine with AudioChat
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" *)
---

# Pairing this machine with AudioChat

```!
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" login
```

Show the user exactly what the command printed — if it printed a pairing code, the code
and the link matter more than anything you might add around them. Say nothing else.
