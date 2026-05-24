# Decision Record 0025 — Backend tool-plugin surface: deferred

**Date**: 2026-05-24
**Status**: Ratified by Glen (rejection / deferral)
**Mode**: conclave 3-AI + Glen ratified (rejection)
**Source task**: `tsk_01KSDCJ6GT107MB9KH71ERTVA6`

## What was chosen

**Do not build** a backend tool-plugin surface for The AI Conclave Switchboard's OpenRouter seats at this time. The proposed surface (`app/services/plugin_tools/` with `register(ctx)` modules, opt-in allowlist in `config.yaml`, per-tool declared permissions, budget participation, and two reference tools `query_decision_memory` / `git_history`) is deferred indefinitely pending a concrete user need.

Glen's authoritative decision, verbatim:

> Rejected. Lack of ROI; return is hypothetical at single-user scale. The conclave's verdict (yes-with-modifications, five operability gates) was well-reasoned and would be the right shape if the feature were built — but the cost is real today and the audience for user-written tool plugins is hypothetical. Revisit when there is a specific tool a user wants to add and an explicit use case to weigh against the gates.

## Why

The proposal was inspired by NousResearch's `hermes-agent` plugin system, which lets users register new tools the LLM can call mid-deliberation. The conclave (codex + gemini + claude-code, judge codex) converged on `yes-with-modifications` with five required operability gates — see "What was rejected" below for the gate list, which is preserved here as the implementation shape *if and when* the feature is reopened.

The rejection rests on a single-user-scale ROI argument that the conclave's abstract reasoning did not weigh:

- The feature only benefits OpenRouter seats with `tool_loop: true` (per DR0015). Day-to-day deliberation on this project leans on the CLI seats (Codex / Gemini / Claude Code), which own their own toolboxes; the OpenRouter tool-loop is a feature that exists but is not on the hot path.
- The two reference tools (`query_decision_memory`, `git_history`) solve problems that are not pressing today. Decision Memory already injects relevant past records as Prior Art at task creation (per Phase 2.5 of the post-DR plan and DR0019/0020/0021's lineage); mid-deliberation lookup is a nice-to-have without an observed friction point.
- The "extend what agents can do without us shipping every domain tool" pitch is a multi-user-platform argument. There is no audience for it yet.
- The full cost — loader + contract + the five operability gates + tests + reference tools + plugin-author documentation — is real, ~one day of focused work, against a hypothetical return.

## What was rejected (preserved as the implementation shape if reopened)

The conclave's converged recommendation is preserved verbatim for future reference. If this proposal is reopened, these five gates are the agreed-upon minimum scope for v1:

1. **Per-call tool telemetry** into `agent_messages` / `agent_runs`: `task_id`, seat, tool name, declared + granted permissions, args (redacted), result/error, status, duration, bytes, budget consumption.
2. **Trajectory export integration** — every plugin tool call lands in the DR0023 JSONL with full telemetry (Charter v1.2 audit-trail requirement).
3. **Orchestrator error handling** — plugin exceptions fail the *tool call only*, not the turn; timeouts and load failures are non-fatal.
4. **Plugin-author documentation** — permission semantics, budget participation, exception contracts; required for v1, not deferred.
5. **Audit-tool enhancement** (`tools/audit_plugin_tools.py`) — surface per-plugin name, declared permissions, grant status, source metadata.

Trust posture (also from the conclave): "soft allowlist + per-tool declared permissions + budget participation" is adequate **only when documented as trusted local Python**, not as sandboxed third-party plugins. Matches the DR0024 precedent.

Sequencing: concurrent with or after DR0015's tool-loop observability work; before any marketplace, installer, CLI-seat plugins, backend-route registration, web/search/network tools, write tools, or execute tools.

## Preserved dissent (per Charter §Dissent Norms)

The conclave converged on the v1 scope but preserved unresolved dissent on **what should happen if a future user proposes a network-capable plugin** (e.g., `pubmed_search`). The split was not flattened by the v1 recommendation and is preserved here for the future deliberation that would resolve it:

- **codex**: "Any network capability during deliberation implicates DR0013 regardless of opt-in. A future network-capable plugin would require explicit revision of, supersession of, or a carve-out exception to DR0013."
- **claude-code**: "Governed user opt-in is fundamentally different in kind from DR0013's rejected unbounded per-agent web autonomy. A future plugin like `pubmed_search` could potentially live in a different policy domain rather than reopening DR0013."
- **conclave resolution**: a v1 plugin surface (if shipped) must not serve as a backdoor for network tools. The first user-proposed network-capable plugin is the trigger for a fresh decision-record deliberation that resolves this split.

Because v1 is not being built, this split remains unresolved and unowned. It re-attaches to whichever future proposal first crosses the DR0013 boundary.

## Known risks

None. Nothing is being built. The conclave's analysis and the dissent are preserved in this record so a future re-proposal does not start from zero.

## Open questions

- **Triggering condition for revisit.** Glen named one explicitly: "when there is a specific tool a user wants to add and an explicit use case to weigh against the gates." That is the bar. A general "should we have plugins?" re-deliberation without a named tool would face the same ROI argument and likely reach the same conclusion.
- **DR0013 dissent resolution.** Deferred to whichever future proposal first introduces a network-capable plugin candidate.
- **Whether the failure mode "users wrote tools we'd rather they hadn't" ever materializes.** If a user does write a domain tool against the existing tool-loop interface by patching `app/services/sandbox_tools.py` directly (treating it as a fork), that is itself a signal that the plugin surface is wanted.

## Who is keeping continuity

Claude Code, as charter keeper. The `tsk_01KSDCJ6GT107MB9KH71ERTVA6` transcript holds the full deliberation; this record holds the decision.

## Operability Impact

Zero. Nothing was built. No DB schema changes, no new API surface, no new code path in the orchestrator, no new dependencies, no new failure modes. The existing tool-loop (DR0015) continues to operate against its fixed `read_file` / `list_dir` / `glob` toolbox.

The bookkeeping cost of this rejection — one decision record, one INDEX entry — is the entire operability footprint.
