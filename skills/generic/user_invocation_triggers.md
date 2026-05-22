# User Invocation Triggers

## Purpose

This skill teaches an AI agent how to recognize when the user wants to invoke The AI Conclave Switchboard for cross-agent consultation. The agent (Claude Code, Codex, Gemini CLI, OpenClaw, etc.) is the "primary" by default — this file tells it when to *stop* answering directly and *start* an AI Conclave Switchboard task instead.

## Endpoint

Default local AI Conclave Switchboard endpoint:

```
POST http://127.0.0.1:8787/api/tasks
```

## Two Types of Triggers

### 1. Explicit triggers (slash commands)

The user types a defined verb. These are unambiguous — invoke immediately, do not ask for confirmation.

| Command | Meaning |
|---|---|
| `/consult <agent> [on <topic>]` | I stay primary; named agent critiques my answer |
| `/secondopinion [on <topic>]` | I stay primary; default consultant from config critiques |
| `/handoff <agent> [on <topic>]` | Named agent becomes primary; I become consultant |
| `/poll <agent1> <agent2> [...]` | Parallel mode — each agent answers independently, no critique loop |

If `<topic>` is omitted, use the last user message or current task as the topic.

Per-agent invocation syntax (how Claude Code vs. Codex vs. Gemini actually register these slash commands locally) lives in each agent's own skill folder, e.g. `skills/claude-code/slash_commands.md`.

### 2. Soft triggers (natural language)

When the user's phrasing implies they want another perspective, treat it as a consultation request. Examples that **should** trigger:

- "are you sure?" (after I've already given an answer)
- "double-check this"
- "get a second opinion"
- "what would Codex say?" / "ask Gemini"
- "is there a better way to do this?"
- "I'm not convinced"
- "have someone else look at this"
- "compare your answer with another model"

Phrases that should **not** trigger (these are doubt or clarification, not consultation):

- "wait, really?" — user wants me to re-explain or verify, not consult
- "are you sure that's the right syntax?" — factual check, just verify
- "doesn't that conflict with X?" — user is reasoning with me, not asking for an outside party

When ambiguous, ask one short question: *"Do you want me to consult another AI, or just re-check my own answer?"*

## Role Disambiguation

When the trigger names a specific agent, decide the role from the user's verb:

| User phrasing | Mode | Primary | Consultant |
|---|---|---|---|
| "ask Codex about this" | consult | me | Codex |
| "what would Codex think" | consult | me | Codex |
| "let Codex handle this" | handoff | Codex | me |
| "have Codex do it" | handoff | Codex | me |
| "what do Codex and Gemini think" | poll | none | both |
| "compare your answer with Codex" | poll | none | both |

When the trigger does not name an agent, default to **consult** mode using the configured default consultant.

## When NOT to Auto-Invoke

Do **not** start an AI Conclave Switchboard task for:

- Trivial tasks (fix a typo, rename a variable, format a file). The latency tax outweighs the value.
- Tasks the user has already approved an answer for. Re-consulting is noise.
- Tasks where I have high confidence and no ambiguity. Save consultation for genuine uncertainty.
- Loops — if the user just received an AI Conclave Switchboard result and pushes back, do **not** immediately re-consult. Re-think first; consult only on a second push or an explicit request.

If uncertain whether a trigger applies, ask in one sentence: *"Want me to run this through the AI Conclave Switchboard, or handle it directly?"*

## Task Submission

When invoking, send a structured task request (see `docs/SWITCHBOARD_PROTOCOL.md`) with:

- `source_agent`: my own name
- `task_type`: best fit (debug, code_review, architecture_review, general_consultation)
- `user_request`: the user's actual question, verbatim where possible
- `primary_agent`, `consultants`: per the role disambiguation table above
- `permissions`: inherit from the current session — do not escalate
- `limits`: respect user-configured max rounds and timeout
- `context`: minimal and relevant — see `safety_behavior.md` for what must never be sent

Surface the returned task ID to the user so they can follow along in the dashboard.

## Returning Results

When the AI Conclave Switchboard returns a final result:

- Lead with the final answer, not the process.
- Surface disagreements verbatim. Do not flatten "consultant disagreed about X" into "we agreed."
- If commands or patches require approval, show them clearly and wait.
- Do not pretend the consultation produced consensus when it did not.
- Do not silently substitute the consultant's answer for my own — name who said what.
