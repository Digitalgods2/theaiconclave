---
description: Get a quick second opinion on a question from one named AI agent
---

The user wants a quick second opinion. Parse `$ARGUMENTS` to extract:
- The agent name (one of: `codex`, `gemini`, `claude-code`) — usually the first word
- The actual question (everything after the agent name)

If `$ARGUMENTS` doesn't name an agent, use `codex` by default.

Use the `switchboard-conclave` skill in `consult` mode. Run:

```bash
python "C:/Users/gosmo/.claude/skills/switchboard-conclave/switchboard.py" --invoked-by claude-code run consult claude-code,<agent> "<question>"
```

Where `claude-code` is the primary (drafts the answer) and `<agent>` is the consultant (critiques it). Render the result. The final answer reflects the primary's response after considering the consultant's critique — so lead with that, then surface where the consultant disagreed.
