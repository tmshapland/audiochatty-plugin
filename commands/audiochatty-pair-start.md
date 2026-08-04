---
description: Get a pairing code to link this machine with audiochatty
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" *)
---

# Pairing this machine with audiochatty — step 1 of 2

```!
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" pair-start
```

Show the user exactly what the command printed. The pairing code and the link matter more
than anything you might add around them, so print those verbatim. Say nothing else.

Pairing takes two commands, and this is the first: it prints a code for the user to enter
in the browser. Once they have entered it, they run `/audiochatty:audiochatty-pair-finish`
here to complete the pairing. The command above already tells them that — don't restate it
in your own words, and don't run the second command for them.
