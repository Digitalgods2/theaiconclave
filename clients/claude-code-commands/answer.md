---
description: Answer a paused conclave task (status awaiting_user_input). Supports direct text OR run-and-send a shell command.
---

A conclave task is paused with `resolution_status: needs_user_input` (or a conclave participant signaled `need_user_input`). The user wants to resolve the pause.

Parse `$ARGUMENTS`:
- **First token**: task ID (typically `tsk_...`) or `latest` for the most recent task
- **Rest**: either (a) the answer text to submit as-is, OR (b) a shell command to run, capture, and send

## Decide which mode

Look at what follows the task ID. Is it:

- **Direct text answer** — short, prose-like, e.g. *"Yes, use Postgres 15 for v1"* or *"The error was caused by missing .env entry"*. Submit as-is.
- **Shell command** — starts with a binary name (`pytest`, `npm`, `git`, `cargo`, `python`, `node`, `make`, `ls`, `find`, etc.), contains pipes, contains options like `-v` or `--verbose`. The user wants you to run it and submit the output.

If ambiguous, ask the user in one short sentence which they meant.

## Mode A: Submit text directly

```bash
python "C:/Users/gosmo/.claude/skills/switchboard-conclave/switchboard.py" --invoked-by claude-code answer <task_id> "<text>"
```

Use this when the answer is a sentence or two the user typed.

## Mode B: Run a command and submit its output

Use your **Bash tool** to run the user's command. Capture combined stdout + stderr. Pipe to the helper's `-` stdin mode:

```bash
<their-command> 2>&1 | python "C:/Users/gosmo/.claude/skills/switchboard-conclave/switchboard.py" --invoked-by claude-code answer <task_id> -
```

For example, if `$ARGUMENTS` is `latest pytest -v tests/`:
```bash
pytest -v tests/ 2>&1 | python "C:/Users/gosmo/.claude/skills/switchboard-conclave/switchboard.py" --invoked-by claude-code answer latest -
```

If the command fails (non-zero exit, command not found, etc.), still submit whatever output you captured — the conclave needs to see the failure too.

## After submitting

The task transitions from `awaiting_user_input` back to `pending` and the worker re-claims it. The conclave continues from where it paused.

To watch the resumed deliberation finish:
```bash
python "C:/Users/gosmo/.claude/skills/switchboard-conclave/switchboard.py" --invoked-by claude-code wait <task_id>
```

Or open the dashboard at http://127.0.0.1:8787/ and find the task in the Inbox.

## Hard rules

- **Always show the user what you ran** before/after running it. They should see the command and (a summary of) the output you submitted.
- **Never modify state** unless explicitly authorized — `git push`, `npm install`, file edits, etc. are NOT what this slash command is for. This is for running read-only inspections (tests, linters, type-checkers, file listings) to answer the conclave's question.
- If the command would clearly write/modify/install, refuse and ask the user to confirm or to use a different invocation.
