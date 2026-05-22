# Role Disambiguation

This skill teaches an AI agent how to decide *who is primary, who is consultant, and which mode applies* when the user invokes the AI Conclave Switchboard. It is the companion to `user_invocation_triggers.md` — that file recognizes the trigger; this file decides the routing.

## The Three Modes

| Mode | Shape | Use when |
|---|---|---|
| `consult` | One primary, one or more consultants critique | Default — user wants their current agent's answer reviewed |
| `handoff` | Named agent becomes primary; current agent steps back | User wants someone else to *do* the task, not review it |
| `poll` | Every named agent answers independently — no critique loop | User wants to compare independent perspectives |

The mode is decided by the user's verb, not by which agents are named. See the table in `user_invocation_triggers.md`.

## Default Routing

When the user's trigger does not name an agent:

- **Mode** → `consult`
- **Primary** → me (the current agent)
- **Consultant** → the configured default consultant from `config.yaml`

When the user names exactly one agent:

- **"ask X"** / **"what would X say"** / **"check with X"** → `consult` mode, I am primary, X is consultant
- **"let X handle"** / **"have X do"** / **"X should answer"** → `handoff` mode, X is primary, I am consultant
- **"compare with X"** / **"X's take vs. mine"** → `poll` mode, X and I are peers

When the user names two or more agents:

- **"ask X and Y"** → `consult` mode, I am primary, both X and Y are consultants. They critique my answer in parallel.
- **"compare X and Y"** / **"what do X and Y think"** → `poll` mode, both are peers, no critique
- **"have X review, then Y review"** → sequenced consult — confirm with the user before running. Default is to run the second consultant only after the primary's response to the first critique.

## Edge Cases

### Named agent is unavailable

Do not silently substitute. Tell the user, list available agents, and ask which to use:

> *"Codex isn't currently enabled. Available consultants: claude-code, gemini, deepseek. Which should I use, or should I proceed without consultation?"*

### Named agent is the same as the current agent

If I am Claude Code and the user says "ask Claude" — clarify in one sentence:

> *"I'm already Claude. Did you mean a fresh-session self-review (re-ask in a separate context), or did you mean a different agent?"*

Do not silently treat it as a no-op.

### Poll mode with only one available agent

Poll mode requires at least two peers. If only one is up, ask:

> *"Only Codex is available right now — poll mode needs at least two. Want me to run a consult instead, or wait?"*

### Permissions don't match the task

If the named primary needs permissions the task does not grant (e.g., user says "let Codex fix the file" but `can_write_files` is false), do not escalate permissions automatically. Surface the conflict and ask:

> *"Handoff to Codex for a file fix needs write permission. The task is currently read-only. Approve write access for this task, or keep it read-only and just get a written recommendation?"*

### User wants to flip roles mid-task

If an AI Conclave Switchboard task is already in flight and the user says "actually let Gemini take this" — do not interrupt the running task. Either:

1. Wait for the current task to complete, then start a new task with the prior result as context, or
2. Cancel the current task explicitly (POST /api/tasks/{id}/cancel) and start fresh.

Confirm which the user wants. The current primary's reasoning state is not transferable to a new primary without re-establishing context.

### User wants to escalate after a consultation completes

"Now ask Gemini what *they* think of this" → start a new task. The prior AI Conclave Switchboard `final_result` becomes part of the new task's `context`. Do not extend the original task — its rounds budget and timeline are spent.

### User names an agent for a task type that agent does not support

If the agent's registry entry does not include the requested `task_type`, surface it:

> *"Codex's registered modes are debug and code_review; the task type is architecture_review. Run it anyway, or pick a different primary?"*

Default: ask, do not assume.

### Ambiguous verb ("see what Codex thinks about doing X")

If I cannot tell whether the user wants `consult` (Codex critiques my plan) or `handoff` (Codex makes the plan), ask one question:

> *"Should I draft an answer first and have Codex critique it, or hand the task to Codex from the start?"*

Default if forced to choose: `consult`. The user can always re-issue as handoff.

## Hard Rules

1. **Never silently substitute agents.** If the named agent isn't available, ask.
2. **Never escalate permissions to fit a routing choice.** Permissions are set by the task; routing adapts to permissions, not the other way around.
3. **Never reuse a `task_id` for a follow-up.** Each AI Conclave Switchboard invocation is its own task with its own rounds budget.
4. **Never claim a mode the user didn't ask for.** If I'm uncertain between `consult` and `handoff`, ask. Do not split the difference.
5. **Final authority follows the mode, not the agent identity.** In `handoff`, the new primary decides — I (as consultant) do not override its final answer in my response back to the user. I surface disagreement; I do not overwrite.
