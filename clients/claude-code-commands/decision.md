---
description: Fetch a task's decision and conclave context, ready for follow-up action
---

The user wants to see (and typically act on) a recorded decision. This is the bridge between *deciding* in the dashboard or via `/decide`, and *executing* the decision in code, prose, or another task.

Parse `$ARGUMENTS`:
- First token: task_id (typically `tsk_...`) — or `latest` to target the most recent task
- No other arguments are expected

Run:

```bash
python "C:/Users/gosmo/.claude/skills/switchboard-conclave/switchboard.py" --invoked-by claude-code decision <task_id>
```

The output shows:
- The original question the user asked the conclave
- The conclave's final answer (with agreement_level)
- The user's recorded decision (or "none recorded yet" with instructions for `/decide`)

## After displaying it

If a decision is present, the user is almost certainly about to ask you to act on it. Be ready:
- If the decision implies a code change, an artifact, or a follow-up plan, surface the next concrete step.
- If the decision is ambiguous in your context (e.g. it references files or paths you don't have access to), ask one minimal clarification before acting.
- Respect permission boundaries — if the decision implies write/run actions and the current session doesn't have those permissions, surface that and ask.

If no decision is recorded yet, point the user to `/decide` or the dashboard's Detail view. Do not assume what they would have decided.
