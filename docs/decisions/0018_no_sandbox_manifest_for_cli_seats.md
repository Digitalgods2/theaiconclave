# Decision Record 0018 — CLI seats receive sandbox path but not the file manifest

**Date**: 2026-05-18
**Mode**: Glen-directed (ratified at implementation greenlight)
**Keeper**: claude-code
**Related**: [DR0012](0012_inline_sandbox_for_api_adapters.md), [DR0015](0015_tool_loop_api_seats.md), [DR0016](0016_user_data_root_and_lazy_config.md), [DR0017](0017_per_seat_readiness.md).
**Source**: implementation session 2026-05-18; companion follow-on to the packaging-architecture consult thread ending at `tsk_01KRVN7VT7NEQ8VNR6NV8CFYFZ`.

## What Was Chosen

The `# Project Sandbox` section in every prompt now has two render modes, selected per-adapter via a new `include_sandbox_manifest: bool` parameter that threads through all five public builders in `app/services/prompt_builder.py`:

- **`include_sandbox_manifest=True`** (default, used by OpenRouter seats) — emits the sandbox path, the read-only tool guidance, **and** a `## File Manifest` block produced by `app/services/sandbox.build_manifest()`. This is the DR0012 behavior preserved verbatim — API-backed seats have no native file tools and need the manifest to know what's available.
- **`include_sandbox_manifest=False`** (passed by CLI adapters: codex / claude-code / gemini) — emits the sandbox path and the read-only tool guidance **without** the file manifest. CLI seats can enumerate the sandbox themselves through their vendor-native tools: Codex's `-C <sandbox>` shell, Claude's `Read`/`Glob` with `--add-dir`, Gemini's `--include-directories`. Re-inlining the manifest just bloats their prompt.

Concretely:

- `app/services/prompt_builder.py`: `_format_project_sandbox(task, include_manifest=True)` gains the parameter; the manifest section is appended only when `include_manifest=True`. All five public builders (`build_primary_prompt`, `build_consultant_prompt`, `build_final_prompt`, `build_peer_prompt`, `build_conclave_prompt`) gain `include_sandbox_manifest: bool = True` and forward it.
- `app/agents/codex_adapter.py`, `claude_adapter.py`, `gemini_adapter.py`: every `build_*_prompt(...)` call passes `include_sandbox_manifest=False`.
- `app/agents/openrouter_adapter.py`: untouched. Falls through the default `True`. Both `tool_loop=True` and `tool_loop=False` OpenRouter paths benefit — the tool-loop path uses the manifest as the model's starting point for `read_file` calls; the inline path uses it as a navigable index alongside `build_sandbox_section` inlined contents.
- 5 new tests in `tests/test_prompt_budget.py` covering: default behavior includes manifest, CLI seats omit it, path + tool guidance survive in both, no-sandbox tasks are unaffected.

## Why It Was Chosen

The principle is "CLI seats have their own tools — don't duplicate them in the prompt." The original architecture-review consult thread surfaced this as a deferred follow-on after observing gemini CLI's prompt size growing across deep continue-threads. Batch C (centralized prompt-budget enforcement, landed earlier this session) already capped the cumulative growth of `prior_messages` — the proximate cause of the timeout. This record handles the second-order contribution: the sandbox manifest itself, which for typical projects is 5–30 KB per prompt.

For non-tool-loop OpenRouter seats, the manifest remains structurally necessary: they cannot navigate the sandbox without it (DR0012 reason still holds). For tool-loop OpenRouter seats, the manifest is the model's catalog for which `read_file` calls to make (DR0015 reason still holds). Both keep the default `True`.

For CLI seats, the manifest is redundant. Codex CLI's `-C <sandbox>` makes the sandbox the shell's CWD; Claude's `--add-dir` + `Read`/`Glob` enumerate file trees on demand; Gemini's `--include-directories` exposes the sandbox to its tool repertoire. Each CLI's vendor-built tools produce a richer, more searchable view of the sandbox than a static manifest dump. Removing the manifest:

- Reduces CLI prompt size by 5–30 KB depending on project size.
- Pairs cleanly with Batch C's budget centralizer: the saved budget is available for `prior_messages` in deep threads instead of being eaten by static file listings the model already has tools for.
- Doesn't displace any operational signal — the path is still in the prompt; the read-only tool guidance is still in the prompt; the model still knows the sandbox exists and what tools to use.

## What Was Rejected

- **Stripping the manifest from OpenRouter seats too.** Rejected — they have no native file tools. The manifest is their only catalog of what's available. DR0012's rationale holds.
- **Stripping the path and tool guidance along with the manifest for CLI seats.** Rejected — the model needs to know there *is* a sandbox and what to do with it. The path mention is small (one line) and load-bearing for the model's planning.
- **A per-mode override** (e.g., keep the manifest for consult-mode primary proposal but drop it from conclave rounds). Rejected — the rule should be uniform across builders. CLI seat = no manifest, every mode, every builder. Easier to reason about and easier to test.
- **Auto-detecting CLI vs API adapter by class.** Considered, rejected — the prompt builder shouldn't know about adapter types. An explicit boolean passed by the caller keeps the dependency direction clean (adapters import the builder, not the other way around). Each adapter declares its own intent.
- **Adding a per-task knob to override the rule.** Considered, deferred. No real use case for "make a CLI seat ingest a manifest." If one appears, the parameter is already there to wire up.
- **Reverting the 240s → 360s timeout bandaid in this commit.** Considered, deferred. With both the budget centralizer (Batch C) and this manifest drop in place, the bandaid is likely no longer needed — but verifying that requires a real deep-thread consult on production CLIs. Reverting speculatively would risk a regression if some other workload still benefits from the headroom. Recommendation: leave 360s in place and watch real-world usage; revert in a separate commit once empirically validated as safe.

## Operability Impact

(Tenth decision under Charter v1.2 §Decision Records.)

- **Observability**: **neutral**. Prompts can still be inspected in the transcript; this just makes CLI-seat prompts smaller. No change to logging or message persistence.
- **Durability**: **neutral**. No new state, no schema change.
- **Recoverability**: **neutral**. If a CLI's native file tool somehow malfunctions on a sandbox, the path mention + tool guidance still tells the model what to do; failing that, the model's reasoning is recorded in the transcript like any other error.
- **Audit trail**: **neutral**. Each agent's prompt is unchanged in *kind* — only in *bytes*. The audit-relevant content (charter, role, task framing, prior messages) is untouched.
- **Retention/export**: **neutral**.
- **Complexity**: **low-positive**. ~30 LOC of net new code (one parameter on `_format_project_sandbox`, five builder signatures extended, three adapter call sites updated) plus ~120 LOC of test coverage. Reduces the load-bearing surface of the prompt for the most common case (CLI seats) without removing any optionality.
- **Cost**: **slightly positive**. Prompt size reduction translates directly to inference cost for CLI seats charged on tokens-in (Codex is subscription, but the per-token-billed paths through any future-CLI gateway benefit). The bigger win is wall-clock: parsing a smaller prompt is faster, which is the direct cause of the gemini-timeout symptom that started this thread.
- **Accepted risks**:
  - **Codex CLI's exploration may need a stronger nudge** if it doesn't auto-list files. The existing tool-guidance text ("you may use your read tools to enumerate the file tree") should suffice but is worth re-reading the first time a real consult runs after this change. If Codex stops citing files it should be reading, a more explicit "start with `ls` or `find` to see what's available" preamble can be added in a follow-up — adapter preamble territory, not prompt-builder.
  - **Gemini CLI's `--include-directories` is the path it uses internally.** Without our manifest, Gemini's first instinct may be to traverse the directory tree. That's the intent.
- **Exceptions to "Operability before capability"**: **none**. This is a tightening of an existing capability, removing redundancy.

## Known Risks

- **No production validation yet.** The change is well-tested in unit tests but the proof is in real CLI behavior on real consults. If a real conclave shows CLI seats failing to cite files they should be reading, this record is the first thing to revisit.
- **DR0012's "the manifest helps the model plan" rationale still applies to API seats** and is preserved. If someone wonders "wait, didn't we want every seat to see the manifest?" — yes, but only those without native tools.

## Open Questions

- **Should the 240s → 360s timeout bandaid be reverted now?** Deferred to empirical observation. Recommendation: revert in a separate small commit after the next real-world deep-thread consult demonstrates gemini stays under 240s with both Batch C and this DR in place. Until then, leave the headroom.
- **Does Codex need a stronger exploration nudge?** Open until first real consult. Easy to add adapter-side later without touching this record.

## Who Is Keeping Continuity

**`claude-code`** as keeper. Implementation landed in one commit alongside this record:

- `app/services/prompt_builder.py` — `_format_project_sandbox` gains `include_manifest`; five public builders gain `include_sandbox_manifest`
- `app/agents/codex_adapter.py`, `claude_adapter.py`, `gemini_adapter.py` — call sites pass `False`
- `app/agents/openrouter_adapter.py` — untouched (default `True`)
- `tests/test_prompt_budget.py` — five new tests for the toggle
- `docs/decisions/INDEX.md` — DR0018 row appended

332 + 5 = 337 tests passing.
