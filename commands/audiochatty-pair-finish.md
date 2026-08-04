---
description: Finish pairing this machine once the code has been entered
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" *)
---

# Pairing this machine with audiochatty — step 2 of 2

```!
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/audiochat.py" pair-finish
```

Show the user exactly what the command printed — if it printed a command to start Claude
Code with, or a line to add to their shell profile, print those exactly as they are. Say
nothing else.

This is the half that redeems the code from `/audiochatty:audiochatty-pair-start`. It polls
for up to 45 seconds, so if the user has not entered the code in the browser yet it will
say so and hand the terminal back; running it again resumes the same pairing.
