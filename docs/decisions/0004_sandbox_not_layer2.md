# Decision Record 0004 — Project Sandbox (shipped) and Layer 2 (deferred)

**Date**: 2026-05-11
**Mode**: Glen + Claude-as-keeper deliberation (no conclave run; this was a back-and-forth between Glen and Claude)
**Keeper**: claude-code

## What Was Chosen

**Ship**: a read-only **project sandbox** mechanism. Per-task copy of `project_path` to `data/sandboxes/<task_id>/`, with skip patterns and permission gates. Each agent's CLI gets pointed at the sandbox via its native read-only mechanism:

- Codex: `-C <sandbox> -s read-only`
- Gemini: `--include-directories <sandbox> --approval-mode plan --skip-trust`
- Claude: `--tools "Read" --add-dir <sandbox> --dangerously-skip-permissions`

Cleaned up on task completion. Orphan sweep on service startup. Opt-in per task via `context.extra.include_sandbox: true` on the API and a checkbox on the dashboard.

**Defer indefinitely**: "Layer 2" — extending per-task permission flags to grant agents *write* and *execute* authority during deliberation.

## Why It Was Chosen

The motivating failure: a conclave task asking *"recommend improvements for this application"* couldn't actually examine the codebase because read access to a directory's tree (vs. individually attached files) was not exposed. The agents (correctly) noted they couldn't proceed without enumeration.

Initial proposal was to extend permissions to allow shell execution (which would let agents `ls`/`cat` their way through the project). That's Layer 2. After deliberation:

### Why the sandbox is right

- **Solves the actual gap**: read-only directory access. Agents can now enumerate and read project files via their native read-only tools — no shell execution authority needed.
- **No race conditions**: sandbox is a copy; agents read but never modify. Multiple readers don't conflict.
- **No category confusion**: the deliberation surface (the sandbox) is fixed at task creation. All three agents see the same starting state; reads don't change it.
- **No audit trail divergence**: deliberation produces text recommendations; nothing on the filesystem changes during a conclave round.
- **Native CLI mechanisms**: each agent's existing read-only sandbox flag is what we use. No new agent authority granted.
- **Bounded cost**: per-task disk usage capped at 200 MiB; cleaned up on completion; orphan-swept on startup. Token cost scales with what each agent actually reads (selective), not with full project inlining (always-on).

### Why Layer 2 was rejected

Five concrete concerns, all stemming from a deeper category confusion:

1. **Category confusion**: the conclave's deliberative value depends on three agents reasoning about *the same stable situation*. Once any participant can modify the filesystem during deliberation, the situation changes mid-loop and the others become reactive to fait accompli rather than independent analysts.

2. **Race conditions**: conclave participants run in parallel (`asyncio.gather`). Two agents editing the same file = last-write-wins corruption with no audit trail of intent.

3. **Output-discipline regression**: granting elevated tool access historically led to JSON-output drift (observed once with Claude in `--permission-mode plan`). Granting `Bash`, `Edit`, `Write` would widen this surface.

4. **Redundancy with the interactive CLI**: the Claude Code session driving Switchboard is itself an execution-capable agent. Adding two more parallel writers (headless Codex + Gemini) creates a coordination problem, not 3× capability.

5. **Audit trail divergence**: today decisions are traceable (deliberate → decide → act). With in-conclave execution, agents act *during* deliberation in arbitrary order; reconstructing "what did the conclave do to my filesystem" is hard.

## What Was Rejected

- **In-prompt context manager** (proposed earlier): server-side bundling of project files into the prompt up to a token budget. Rejected in favor of the sandbox because the manifest-in-prompt approach is lossy at scale (the 256 KiB cap truncates real projects) and forces all agents to receive the same fixed slice regardless of relevance. The sandbox lets each agent pull selectively.

- **Layer 2 with additive permission checkboxes**: rejected per the five reasons above.

- **Layer 2 with explicit-gate design** (separate "elevated mode" flag): still rejected for the same reasons, but reconsider if the L3 workflow (deliberate → decide → execute in Claude Code → continue) genuinely fails for a concrete use case.

## Known Risks

- **Output-discipline regression on Claude with the Read tool**: we already enable `--tools "Read"` for image-attached tasks; extending to sandbox-attached tasks widens the surface. Mitigation: prompt engineering instructing Claude to use Read sparingly and produce structured JSON; defensive parsing already handles fence-wrapped output.

- **Information leakage in sandbox copies**: a project may contain secrets the user didn't mean to share. Mitigation: default ignore patterns include `.env*`, `*.key`, `*.pem`, `credentials*`; per-task permissions gate `.env` and `*.key` files even when present in the project; sandbox is read-only so agents can't exfiltrate beyond what they put in their responses.

- **Stale snapshot when task is paused**: if a task pauses with `awaiting_user_input` and the user modifies their project meanwhile, the sandbox is fixed at copy-time. Probably correct — deliberation anchored to a point in time — but a behavior to be aware of.

- **Cost surface for token usage**: each agent reading many files burns tokens. Tasks with overly broad questions on large codebases may cost meaningfully more than focused questions. Mitigation: prompt-side guidance encourages selective reading; user can scope by setting `project_path` to a subdirectory.

- **Sandbox cap of 200 MiB**: projects above this get truncated arbitrarily during copy. Mitigation: log shows when cap hit; user can pre-scope `project_path` to a smaller subtree.

## Open Questions

- **Per-adapter file-read limits**: should we cap how many tool-uses (Read calls in Claude, shell commands in Codex) an agent can make per round? Today there's no cap. A runaway agent could blow the timeout.

- **Manifest size for very large projects**: 400-entry cap on the manifest may hide files in big projects. Adjust if it becomes a real friction.

- **When to actually do Layer 2**: not now, but if a clear use case emerges (e.g., "the conclave must run pytest and reason over its output for X-style task"), revisit with the explicit-gate design rather than additive permissions.

## Who Is Keeping Continuity

**`claude-code`** as keeper. Implementation lives at `app/services/sandbox.py`, adapter wiring across `app/agents/{codex,gemini,claude}_adapter.py`, prompt-builder integration at `app/services/prompt_builder.py::_format_project_sandbox`, orchestrator wiring at `app/services/orchestrator.py::run_task`. Tests at `tests/test_sandbox.py`.
