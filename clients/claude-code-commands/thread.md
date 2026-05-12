---
description: Show the ancestry chain (thread) for a conclave task
---

The user wants to see the thread of deliberation leading to a particular task — every prior task it was continued from, with each ancestor's question, final answer, and recorded decision.

Parse `$ARGUMENTS`: task_id (typically `tsk_...`) or `latest` for the most recent task.

Run:

```bash
python "C:/Users/gosmo/.claude/skills/switchboard-conclave/switchboard.py" --invoked-by claude-code thread <task_id>
```

The output shows the chain oldest-first, with the current task marked. Use this when:
- The user is trying to recall what was already decided in a long-running thread
- A new question arose that might already be answered upstream
- The user wants to navigate to an earlier ancestor to drill in

After displaying, if the user wants to act on something in the thread, suggest:
- `/decision <task_id>` to fetch a specific ancestor's full context
- `/continue <task_id> <question>` to start a new follow-up from any node in the chain
