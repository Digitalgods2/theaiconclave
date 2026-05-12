# Decision Record 0012 — Inline the project sandbox for the API-based council seats

**Date**: 2026-05-12
**Mode**: Glen-directed (no separate conclave)
**Keeper**: claude-code

## What Was Chosen

When a task has a project sandbox attached, the **HTTP-based council seats** (OpenRouter — `deepseek` / `glm` / `qwen`; and the Ollama Cloud seats if enabled) now receive a **read-only file tree + the contents of as many files as fit** inlined into their prompt. The CLI seats (codex / claude-code / gemini) already get native read access to the sandbox directory; the API seats can't browse, so the only way to give them "read access" is to paste the files in.

Glen's words: *"I want the best brains in the world to at least have read access."* The open-weight models on OpenRouter were text-only — when asked to "examine the program in this folder" they'd keep asking the user to describe files (task `tsk_01KRF5T1KB...`). This closes that gap.

Concretely:

- **New helper** `app/utils/sandbox_inline.py` — `build_sandbox_section(sandbox_path, char_budget) -> str`. Walks the (already-filtered) sandbox dir, skips binaries (NUL-byte heuristic), generated/junk extensions (`.png`, `.pyc`, `.min.js`, `.zip`, …), empty files, and files over a 200 KB per-file cap. Always emits a file tree (cheap); then inlines file contents in priority order — known entry-point names (`main.py`, `README.md`, `package.json`, `config.*`, …) first, then shallow source/config files, then the rest — until the budget runs out, then notes how many were omitted ("ask for any specific file by path if you need it").
- **Adapter wiring** — `OpenRouterAdapter` and `OllamaCloudAdapter` each gain `_append_sandbox(prompt, ctx)`: if `ctx.task.context.extra["sandbox_path"]` is set, it appends `build_sandbox_section(path, budget)` to the prompt where `budget = max_context_chars - len(prompt) - 16000` (headroom for the response + tokenizer slop). Every `run_*` method now calls `self._invoke(self._append_sandbox(prompt, ctx), …)`.
- **No new config, no UI change.** It's automatic: tick "include project" on the New Task form (as you already would for a code review) → the orchestrator builds the sandbox → every seat, CLI or API, can read it.
- **Tests** — `tests/test_sandbox_inline.py` (6 tests: tree + contents, skips binary/image/empty, tiny-budget → tree-only, priority ordering under a tight budget, per-file cap, nonexistent/empty path → ""); plus a test in `tests/test_openrouter_adapter.py` that `run_conclave_turn` appends the section when the task carries a sandbox (and doesn't when it doesn't). 177 tests total, all pass.

## Why It Was Chosen

The whole point of the open-weight seats is "another pair of eyes" on the code (decision 0011) — but eyes that can't see the code are nearly useless for the conclave's main use case, code review. The CLI seats lean on their native read tools; the API seats had nothing equivalent. Inlining is the only mechanism available (you can't hand an HTTP model a filesystem), and these models have large context windows (the OpenRouter defaults declare 400K–800K char budgets), so most small-to-medium codebases fit, and what doesn't gets a tree + an "ask for it" affordance. It reuses the existing sandbox machinery (already filtered for `.git`, `node_modules`, secrets, etc.) — no new infrastructure.

## What Was Rejected

- **A fetch-this-file tool / function-calling loop** (model asks for a file, we fetch it, continue). Rejected for v1 — much more complex (multi-turn tool loop per adapter, per provider), and unnecessary when the context windows are big enough to just include everything. Could revisit if codebases routinely overflow the budget.
- **Task-keyword-based file prioritisation** (rank files by relevance to the question text). Rejected for v1 — heuristic-heavy and brittle. Entry-point-name + shallow-path ordering is a decent, deterministic default; the model can ask for specific files if the budget cut something it needs.
- **Changing the prompt builder instead of the adapters.** The prompt builder already adds a "# Project Sandbox" section (path + file manifest) for all agents, telling CLI agents to use their read tools. Adding the *contents* there would bloat the prompt for the CLI agents (which don't need it — they can read the real files) and for the `fake` adapter. Doing it per-adapter keeps the contents going only to the seats that actually need them.
- **A config toggle to disable inlining.** Not worth it — if a task has a sandbox, the user wants the agents to see it; if they don't want that, they don't attach a sandbox. Sizing is automatic per the model's context budget.

## Operability Impact

(Seventh decision under Charter v1.2 §Decision Records.)

- **Observability**: neutral.
- **Durability**: neutral. No new state, no schema change. The helper reads the existing per-task sandbox dir.
- **Recoverability**: neutral. If the sandbox path is missing/unreadable, `_append_sandbox` returns the prompt unchanged (graceful) — the seat just operates text-only as before.
- **Audit trail**: neutral. (The inlined files aren't separately persisted; they're part of the prompt, same as any other prompt content.)
- **Retention/export**: neutral.
- **Complexity**: low. One new ~180-line helper module, a ~15-line method on each of the two HTTP adapters, the `run_*` call-site change. No new dependencies, no new processes.
- **Accepted risks**:
  - **Bigger prompts → slightly more cost** on the metered OpenRouter seats. A code-review prompt with an inlined small codebase is maybe 100K–400K input chars ≈ 25K–100K tokens ≈ a few cents per turn at OpenRouter prices (vs. <$0.01 without). Still pennies; bounded by `max_context_chars` and the round/time backstops. Glen's "costs out of scope for on-demand use" holds.
  - **Truncation when a codebase exceeds the budget.** Mitigated by priority ordering (entry points first) + the explicit "N files omitted — ask by path" note, so the model knows it's partial and can request more.
  - **Redundancy with the prompt builder's existing manifest section.** The API agent now sees the file list twice (once in "# Project Sandbox", once in "## PROJECT FILES") and a "use your read tools" line it can't act on. Cosmetic; the more-specific "you have no browsing tool, here are the contents" section wins. Not worth de-duplicating.
  - **A pathological huge file** could still eat budget up to the 200 KB per-file cap; beyond that it's excluded entirely (and dropped from the tree). Acceptable.
- **Exceptions to "Operability before capability"**: **none.** Capability addition; the only downside is a bounded cost bump on a metered dependency.
- **Follow-up review point**: if real codebases routinely overflow the budget (lots of "N files omitted" in transcripts), consider the fetch-a-file tool loop or smarter prioritisation. Until then, the simple inline approach is enough.

## Known Risks

(Operability Impact covers the categories.)

- **The user still has to attach the project.** This only fires when `context.extra["sandbox_path"]` is set, i.e. the user ticked "include project" on the New Task form. A code-review task submitted without a sandbox still leaves the API seats text-only — by design.
- **Not visually verified in a browser** (no browser in the build environment) — but there's no UI change here; the change is server-side prompt construction, covered by tests.

## Open Questions

- **Should the inlined section be placed earlier in the prompt** (before the prior-turns transcript) rather than appended at the end? Currently appended; recency arguably helps, and it works. Revisit if models seem to ignore it.
- **A "files the model asked for" follow-up mechanism** — if the budget omitted a file and the model says "I need `app/services/orchestrator.py`", there's no automatic way to feed it on the next round. The model could be told to set `need_user_input` and ask, and the user pastes it — clunky but works. The fetch-a-file tool loop is the real fix; deferred.
- **Per-adapter `max_context_chars` accuracy.** The OpenRouter defaults (400K/800K chars) are conservative guesses; if a model's real window is smaller, a too-big inlined sandbox could get rejected by the provider. Mitigated by the 16K headroom and the 0.85-ish conservatism baked into "verify slug" thinking; tune in config if a real call complains.

## Who Is Keeping Continuity

**`claude-code`** as keeper. Implementation lives at:

- `app/utils/sandbox_inline.py` — `build_sandbox_section`, `_walk`, `_priority_rank`, skip lists
- `app/agents/openrouter_adapter.py` — `_append_sandbox`, `_SANDBOX_HEADROOM`, `run_*` call-site changes
- `app/agents/ollama_adapter.py` — same
- `tests/test_sandbox_inline.py` — 6 tests
- `tests/test_openrouter_adapter.py` — added: sandbox-inlined and not-inlined cases
