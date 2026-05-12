---
description: Continue a conclave thread - start a new task threaded to a parent (same mode + agents, prior thread context auto-injected)
---

The user wants to continue an existing conclave thread. The new task will be linked to the parent via `parent_task_id`, and the orchestrator automatically loads the parent's ancestry (question + final answer + your recorded decision) into every participant's prompt as the "Prior Thread Context" section.

This is the right move when:
- The first conclave's resolution was ambiguous and you want to dig deeper
- A decision was made and you want to drill into implementation details
- A new question arose that depends on the prior task's context

Parse `$ARGUMENTS`:
- First token: parent task ID (typically `tsk_...`) — or `latest` for the most recent task
- Rest of the line: the new question

Run:

```bash
python "C:/Users/gosmo/.claude/skills/switchboard-conclave/switchboard.py" --invoked-by claude-code continue <parent_task_id> "<question>"
```

The new task automatically inherits the parent's mode and agents — if the parent was a 3-AI conclave, the follow-up is too. To override (e.g., follow up a conclave with just a consult), the user should use `/conclave` or `/consult` directly and reference the parent in their question text.

After the task completes, render the result for the user. The conclave's participants will see the prior thread context in their prompts, so their answers should pick up where the prior thread left off.

If the user wants to inspect the thread genealogy first, suggest `/thread <task_id>`.
