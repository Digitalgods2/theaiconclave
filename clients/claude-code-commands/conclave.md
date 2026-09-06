---
description: Submit a question to the AI conclave (codex + antigravity + claude-code) for multi-AI deliberation
---

The user wants the AI conclave to deliberate on the following question.

**Question**: $ARGUMENTS

Use the `switchboard-conclave` skill to run this in `conclave` mode with the default three real agents (`codex`, `antigravity`, `claude-code`). Run:

```bash
python -m switchboard_conclave --invoked-by claude-code run conclave codex,antigravity,claude-code "$ARGUMENTS"
```

Then render the result for the user. Lead with the final answer; surface disagreements verbatim if any. Don't flatten dissent.

If the question is trivial enough that a single AI could answer it, mention that briefly before running — the conclave costs subscription quota across all three providers.
