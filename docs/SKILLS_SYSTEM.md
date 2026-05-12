# Skills System

A **skill** is a Markdown instruction file that tells an AI agent how to behave inside Switchboard. Skills are not code. They are *embedded* into prompts by adapters and read by agents at runtime to shape their responses.

This document explains what skills are, where they live, how they get loaded, and how to add new ones. The protocol (`SWITCHBOARD_PROTOCOL.md`) defines what gets *transmitted*; this document defines what gets *expected* from the agent on the other end.

## 1. Why Skills Exist

The protocol can require an agent to return JSON in a specific shape. It cannot make the agent *understand* its role on the task. Skills bridge that gap. When the orchestrator sends a task to an agent, the prompt includes:

- **Role frame** — the relevant skill content
- **Task framing** — the user's request and context
- **Prior messages** — proposals, critiques, prior rounds
- **Output requirement** — JSON schema, no fences, no prose

The skill is the agent's user manual for the round it is about to run.

## 2. The Two Layers

### Generic skills — `skills/generic/`

Apply to every agent regardless of which CLI it wraps. The MVP ships six:

- `switchboard_connector.md` — how an outside agent calls Switchboard
- `primary_agent_behavior.md` — how to behave as primary
- `consultant_behavior.md` — how to behave as consultant
- `safety_behavior.md` — what every agent must not do, regardless of task
- `user_invocation_triggers.md` — when a user phrase means "consult"
- `role_disambiguation.md` — how to choose mode/role when invocation is ambiguous

### Per-agent skills — `skills/<agent>/`

Override or supplement generic skills with tool-specific guidance:

- `skills/codex/codex_switchboard_skill.md` — Codex-specific connector behavior
- `skills/claude-code/claude_switchboard_skill.md` — Claude Code-specific connector behavior
- `skills/<agent>/<role>_rules.md` — role overrides where the agent's defaults conflict with Switchboard's expectations

When both layers exist, **per-agent skills take precedence on overlapping topics.**

## 3. How Skills Get Loaded

### Outbound — agent calls Switchboard

A user installs the per-agent skill into their agent's own config (e.g., a Claude Code skill file in `~/.claude/skills/`). When the user types a triggering phrase or a slash command, the agent reads its skill, builds a Switchboard task request, and POSTs it to `127.0.0.1:8787/api/tasks`. The skill is the *user manual the agent reads to know how to call Switchboard*.

### Inbound — Switchboard runs an agent

When Switchboard runs an agent, the adapter constructs a prompt that **embeds the relevant generic skills verbatim**:

| Adapter call | Skills embedded |
|---|---|
| `run_primary` | `primary_agent_behavior.md` + `safety_behavior.md` |
| `run_consultant` | `consultant_behavior.md` + `safety_behavior.md` |
| `run_final` | `primary_agent_behavior.md` + `safety_behavior.md` + the prior consultant critique |
| `run_peer` | `primary_agent_behavior.md` + `safety_behavior.md` (peer mode is "primary without critique loop") |

If a per-agent skill file exists for the agent and role, the adapter substitutes it for the generic version.

## 4. Skill Format

Every skill file is plain Markdown. No frontmatter, no YAML, no executable code. Conventions:

- Lead with a one-line purpose.
- Use **imperative voice** (*"Return…"* not *"You should return…"*).
- Reference the JSON schema from the protocol — do not redefine it.
- Keep total length under ~1500 words. Skills are embedded into every relevant prompt; tokens are not free.

## 5. Adding or Modifying Skills

1. Edit the markdown file in `skills/`.
2. Restart the Switchboard service so adapters re-read the content.
3. If the change introduces a new generic skill, update the relevant adapter to include it in prompt construction.
4. Skill changes are not breaking protocol changes — no `protocol_version` bump. They do require a new test if behavior changes meaningfully.

## 6. What Skills Do Not Replace

- **Permissions.** A skill cannot grant capabilities the task did not authorize. A skill telling an agent "you may write files" is overridden by `can_write_files: false` on the task.
- **The protocol.** A skill cannot tell an agent to return a different JSON shape. The validator will reject it.
- **The orchestrator's debate rules.** A skill cannot tell an agent to ignore consultant critique or skip rounds. The orchestrator controls flow; skills shape behavior *within* that flow.
