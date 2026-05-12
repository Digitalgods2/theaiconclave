---
description: Get a second opinion on the current conversation/answer from another AI
---

The user wants a second opinion on whatever has just been discussed.

If `$ARGUMENTS` is empty, use the last substantive question or proposal in this conversation as the topic. Otherwise use `$ARGUMENTS` as the topic.

Default to `consult` mode with `claude-code` as primary (since you have full context) and `codex` as the consultant. If the user mentions another agent in their phrasing ("get Gemini's take"), use that instead.

Run:

```bash
python "C:/Users/gosmo/.claude/skills/switchboard-conclave/switchboard.py" --invoked-by claude-code run consult claude-code,codex "<topic>"
```

Render the result. Lead with the consultant's actual critique — that's what the user asked for. If the consultant agreed, say so plainly without padding.
