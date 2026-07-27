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
and the link matter more than anything you might add around them. Say nothing else.
