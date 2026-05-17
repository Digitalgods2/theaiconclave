# Decision Record 0015 — Tool-loop architecture for the API-based council seats

**Date**: 2026-05-17
**Mode**: Glen-directed (ratified)
**Keeper**: claude-code
**Related**: [DR0011](0011_openrouter_council_seats.md), [DR0012](0012_inline_sandbox_for_api_adapters.md).

## What Was Chosen

The OpenRouter-backed council seats (`deepseek`, `glm`, `qwen`, `kimi`, plus any future config-driven seat) gain an OpenAI-style tool-call loop. Instead of receiving the entire project sandbox inlined into the prompt at task creation (the DR0012 v1 design), they can iteratively call three functions during a turn:

- `read_file(path)` — return the contents of a specific file inside the sandbox
- `list_dir(path)` — return the immediate contents of a sandbox directory
- `glob(pattern)` — return file paths matching a standard glob pattern (`**/*.py`, `app/services/*.py`, etc.), capped at 200 paths per call, honoring the same ignore rules the sandbox already applies (`.git`, `node_modules`, `.venv`, etc.)

The adapter intercepts each tool call, reads from the per-task sandbox, returns content, and loops back to the model until it emits its structured turn JSON.

Concretely:

- **Adapter change** — `app/agents/openrouter_adapter.py` learns a tool-loop mode. Per call: send the prompt with the two tool definitions, parse `response.message.tool_calls` if present, execute each against the sandbox, append a `role: "tool"` message with the result for each call, send the updated message list back. Repeat until the model emits content (the structured turn JSON) instead of further tool calls, or the iteration cap fires.
- **Bounds** — Two hard ceilings per turn: `max_tool_iterations` (default **8**) and `max_tool_bytes` (default **256 KiB** cumulative). When either cap fires, the adapter sends a final message instructing the model to emit its structured turn now with whatever it has. The cap is logged so it's visible in the audit trail.
- **Schema** — Two new `MessageType` values: `tool_call` (direction `from_agent`, captures the function name + arguments) and `tool_result` (direction `to_agent`, captures the bytes returned). Each tool round is two rows in `agent_messages`. The transcript renderer surfaces them in the Detail view so the user can see exactly which files an agent read.
- **Sandbox inline behavior** — When tool-loop is on for a seat and a task has a sandbox, the inlined-file-contents block from DR0012 is dropped; only the file-tree manifest is kept (the agent needs *some* hint about what's available to start asking for). When tool-loop is off, DR0012 behavior is unchanged.
- **Config** — Per-seat `tool_loop: bool` flag in `openrouter.models[]`. **Default `false`** for v1 (opt-in per seat, since not every OpenRouter model implements tool-calls equally well). Glen can flip individual seats on as he verifies them. After all four configured seats are verified, the default flips to `true` and the docs are updated.
- **Degradation** — If a configured `tool_loop: true` seat returns content without ever emitting tool calls (some models ignore the function definitions), the adapter accepts that as a normal turn. If a tool-loop seat's model doesn't support function-calling at all (errors out of the gate), the adapter logs a warning and falls back to DR0012 inlined behavior for that task.
- **Timeout** — Per-call timeout is raised from `180s` to `300s` for tool-loop turns (multi-round inference legitimately takes longer than a single shot).

## Why It Was Chosen

The DR0012 inlined-sandbox design was the right v1 — simple, single round-trip, no protocol changes. Real-world experience after a few months of code-heavy conclaves surfaced its ceiling: on a code-review task with **~95K-token inlined prompts**, the open-weight seats fell back to template best-practices instead of citing specific files. The bottleneck wasn't parameter count — it was the model's *effective attention budget* (somewhere in the 50–80K range for most current open-weight models). Once the prompt exceeded that, the actual codebase became background noise and the deliberation degraded.

The CLI seats don't have this problem because they natively pull files on demand (Codex's `-C`, Claude's `Read` tool, Gemini's `--include-directories`). DR0012's Open Questions section already flagged this asymmetry as the natural follow-up.

A tool-loop closes the gap. The agent reads what it actually needs (specific files, specific lines) instead of being given everything and asked to filter. Real-world precedent: this is how Claude Code, Cursor, and Aider already work — and how the CLI seats already work inside our own system. We're just bringing the open-weight seats up to parity.

**Why not a single pre-flight "what files do you need?" call followed by an inlined second call?** Considered briefly. Rejected: the value of the loop is *iterative exploration* — reading file A surfaces something that prompts reading file B. A single-shot file selector forecloses that.

**Why default off?** Tool-call support varies across OpenRouter models. `deepseek-v4-pro`, `qwen3.6-plus`, and `glm-4.6` claim function-calling support, but each implements it slightly differently and the strictness of structured-output adherence varies. Defaulting off lets Glen verify per seat (one task each is enough) before flipping it on, and avoids a regression on day one for any seat that doesn't actually behave under tool-call mode.

## What Was Rejected

- **Always-on (no opt-in).** Considered, rejected. See "Why default off" above — tool-call support is uneven across providers and a regression would be invisible until someone notices weak outputs.
- **A `grep` tool.** Rejected for v1. Unlike `glob` (which returns just file paths and is naturally bounded by a path count cap), `grep` opens a real token-cost amplification path — one bad search over a large codebase can pull megabytes of matching content. The `max_tool_bytes` cap would clip it, but a clipped result is often worse than no result (the agent reasons over a misleading partial view). Better to wait until real usage shows the agent floundering without it, then design `grep` with explicit context-bytes and match-count limits.
- **Per-task override of `tool_loop`.** Considered, deferred. The task request would gain a per-seat-override field, which sprawls the protocol. Glen's stated usage is "verify a seat once, flip it on permanently" — that's a config edit, not a per-task knob. Add only if a real need emerges.
- **Streaming the tool responses back to the model.** Rejected for v1. OpenRouter's chat-completions endpoint is request/response; streaming tool results would mean reworking the transport layer. The bounds (`max_tool_bytes=256KiB`) keep per-iteration payloads manageable without streaming.
- **Treating tool calls as a permission-bearing operation.** Considered (per the charter's safety model), rejected. The tool surface is intentionally narrow: read-only access to the same sandbox the agent already had inlined. Adding a permission flag wouldn't gate anything new — it would just be ceremony. If a future tool adds write or execute capability, that *would* deserve a permission flag.

## Operability Impact

(Sixth+ decision under Charter v1.2 §Decision Records.)

- **Observability**: **positive**. Each tool call becomes a visible row in `agent_messages` with the function name, arguments, and (truncated) response bytes. The user sees the agent's actual information-gathering pattern in the transcript — strictly more visibility than today's "everything was inlined, we hope they read the right parts" posture.
- **Durability**: **neutral**. Two new `message_type` values are added to the enum; no schema migration beyond that (the `agent_messages` table already accommodates the shape).
- **Recoverability**: **slight negative**. A tool-loop turn that crashes mid-loop is harder to mentally model than a single-shot turn. Mitigation: the `max_tool_iterations` cap puts a hard ceiling on loop length; the orphan reaper (Phase 1 of post-DR plan `tsk_01KRSW6AS3M66B4RRJE3JFAPRV`) catches any task that ends up stuck regardless. Net: comparable to today's behavior on the failure path.
- **Audit trail**: **positive**. Transcripts get richer. Exports already render every `agent_message` row; the new types fall in automatically. The user can post-hoc reconstruct exactly which files a given conclave participant read — useful when a decision turns out to be load-bearing later and you want to audit what evidence the agent actually saw.
- **Retention/export**: **neutral**. Tier 3 trimming already operates on `agent_messages` as a whole; tool-call rows are subject to the same rules as conclave_turn rows.
- **Complexity**: **moderate negative**. The OpenRouter adapter gains a loop construct, two new message types, four new bounds (iterations, bytes, timeout, plus the fallback path), and a flag-driven mode switch. ~150–250 lines of net new adapter code, plus a comparable amount of test coverage. Manageable; not a refactor.
- **Cost**: **mixed**. Per-turn cost goes up (multiple inference calls instead of one), but signal quality goes up faster (the seats actually use the codebase). The existing Usage panel surfaces per-task spend at runtime, so Glen will see the tradeoff per-task and can disable a seat that's expensive without value.
- **Accepted risks**:
  - **Runaway tool-call loops** (a model that keeps requesting files without ever emitting structured turn JSON). Mitigated by `max_tool_iterations=8` and `max_tool_bytes=256KiB` with a forced-final-turn fallback when either fires. The orphan reaper is the last line of defense.
  - **Model doesn't follow tool-call protocol cleanly** (returns malformed `tool_calls`, claims a tool the adapter didn't offer, etc.). Adapter validates each call before executing; invalid calls return an error message back to the model in the next iteration. If three consecutive iterations produce invalid calls, the adapter forces the final turn.
  - **Tool-call mode regresses output quality on a specific model.** Mitigated by the per-seat opt-in default. If Glen verifies a seat and tool-loop turns out worse on his actual tasks, he flips it off — no code change.
  - **Increased cost per task on tool-loop seats.** Visible in the Usage panel. Each iteration is a billable inference call. If real usage shows cost climbing faster than value, the bounds tighten or specific seats get flipped off.
- **Exceptions to "Operability before capability"**: **none**. This is a capability addition that *improves* observability and audit trail (positive operability dividends) and doesn't displace any named operability gap. The bounded-priority-window test is satisfied — the orphan reaper closed the recoverability gap in May 2026 (Phase 1 of the post-DR plan); no other operability foundation is currently flagged as missing.

## Known Risks

(Covered above. Two additional notes.)

- **Charter §Tool surface evolution.** v1 ships with exactly three tools (`read_file`, `list_dir`, `glob`). Adding a fourth tool later (e.g., `grep`, `git_log`, `git_blame`) is a config + adapter change, but it's also a *capability surface expansion* and should get a follow-up decision record at that point — not just a quiet adapter PR. Recording this here so the precedent is explicit.
- **DR0012 inlined-sandbox path is preserved, not deleted.** When `tool_loop: false` (the v1 default), DR0012 behavior is unchanged. This is intentional — it lets Glen verify each seat in isolation and roll back per-seat without code changes. Once all configured seats are verified and the default flips to `true`, the inlined-contents path is still kept as a fallback for seats whose models don't support function-calling. We don't anticipate removing it.

## Open Questions

- **Bounds tuning.** `max_tool_iterations=8` and `max_tool_bytes=256KiB` are first-pass guesses based on rough reasoning about how many files a useful round of exploration touches. Real-world usage may show these need tightening (cost) or loosening (capability). Both are config-driven, so tuning is a config edit. Glen should expect to revisit them after the first 5–10 tool-loop conclaves.
- **Per-iteration prompt format on subsequent rounds.** OpenAI's spec calls for appending the original assistant message containing the tool calls plus one `role: "tool"` message per result. We follow that. If a model insists on a different shape (some OpenRouter providers handle this differently), the adapter will need per-provider adjustments — handle that case if and when it surfaces.
- **`glob` cap of 200 paths.** First-pass guess. If real usage shows the agent doing follow-up `glob` calls with progressively narrower patterns to fit under the cap (`**/*.py` returns 250, so it asks for `app/**/*.py`, then `app/services/**/*.py`), the cap is too tight and should be loosened. Conversely, if a careless `**/*` over a giant repo bloats the audit trail without value, tighten. Tunable in the same way as the other bounds.
- **Behavior when the sandbox is absent.** If a task is submitted with no `project_path`, the adapter should still surface the tools but every call returns "sandbox not present." Alternative: don't surface the tools at all on no-sandbox tasks. Decision deferred until implementation — both are cheap.

## Who Is Keeping Continuity

**`claude-code`** as keeper. Implementation will land at:

- `app/agents/openrouter_adapter.py` — new tool-loop mode, bounds, fallback path
- `app/protocol/validators.py` — `MessageType.TOOL_CALL`, `MessageType.TOOL_RESULT`
- `app/config.py` — `OpenRouterModel.tool_loop: bool = False`
- `config.example.yaml` — flag added to each seat entry with comments on verification status
- `app/dashboard/dashboard.js` + `.css` — transcript renderer surfaces the new message types
- `app/services/exporter.py`, `app/services/doc_export.py` — export formatters handle the new types
- `tests/test_openrouter_tool_loop.py` — loop behavior, bounds, fallback, malformed tool calls
- `docs/AGENT_ADAPTERS.md` — updated to document the tool-loop mode

The implementation lands in two commits: (1) protocol + adapter + tests, (2) dashboard + export + docs. Ratification of this record gates both.
