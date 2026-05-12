---
description: Record your authoritative decision on a completed conclave task
---

The user wants to record their final decision on a task that the conclave deliberated on. Per the Conclave Charter, significant work closes with a decision record — this is the user's authoritative call after seeing the conclave's recommendation.

Parse `$ARGUMENTS`:
- First token: task_id (typically `tsk_...`) — or `latest` to target the most recent task
- Rest of the line: the user's decision text (free-form)

If `$ARGUMENTS` is empty or only contains the task_id, ask the user briefly what their decision is, then proceed.

Run:

```bash
python "C:/Users/gosmo/.claude/skills/switchboard-conclave/switchboard.py" --invoked-by claude-code decide <task_id> "<decision text>"
```

The decision must be free-form text (the user's own words). The Conclave Charter §Decision Records suggests a structure (what was chosen, why, what was rejected, known risks, open questions, who keeps continuity) — if the user gave a one-liner, that's fine; if they gave a structured record, that's better.

After recording, confirm to the user that the decision is now part of the task's permanent record. Note that:

- The decision is now visible in the dashboard's Detail view above the post-task action bar
- The decision will auto-include as context in any follow-up task they submit on this thread
- `/decision <task_id>` fetches it back if they want to act on it later
