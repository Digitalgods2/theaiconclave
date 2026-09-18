# Roadmap

This document tracks what's shipped, what's next, and what's been *intentionally not built* and why. The third category is as important as the first two — recording that a feature was considered and consciously declined prevents future cycles of "should we add X?" without context.

## Shipped (MVP)

| Capability | Notes |
|---|---|
| Charter v1.0 + v1.1 (Multimodal Disagreement) + v1.2 (Operability before capability) + v1.3 (Evidence Norms) | `skills/generic/conclave_charter.md`. v1.2 also added the *Operability Impact* field to Decision Records; v1.3 requires participants to identify the basis for load-bearing factual claims. |
| Protocol v1.0 + v1.1 (Multimodal Disagreement) | `docs/SWITCHBOARD_PROTOCOL.md`, `skills/generic/conclave_charter.md` |
| Three deliberation modes | `resolve`, `consult`, `conclave` — see `docs/SWITCHBOARD_PROTOCOL.md` and `docs/CODING_WORKFLOW.md` |
| Three real-AI adapters | Codex, Gemini, Claude Code — all read-only, JSON-output-disciplined |
| Conclave with full-mesh deliberation | N participants, convergence-based termination, synthesis round on weak convergence |
| Decision panel | Per-task recorded decision, dashboard + CLI access (`/decide`, `/decision`) |
| Threading | `parent_task_id`, ancestry walks, thread breadcrumb, `/continue` |
| Multimodal attachments | Text / Markdown / PDF inlined; images visible to all agents via per-adapter image piping |
| Permission toggles on the New Task form | Per-task permissions, three presets (read-only / read + .env / read everything), client-side enforcement of the install-implies-others rule |
| Retention policy | Tier-based selection, operational triggers (DB size 2 GB / task count 1000), 6-hour worker |
| Copy buttons | Consistent upper-right anchor on every panel; inline next to task IDs |
| Dashboard UI | Single-page app, vanilla JS, served from FastAPI at `/` |
| Project sandbox | Per-task read-only copy of `project_path` so agents can browse source. Tier-aware permission gates, ignore patterns, 200 MiB cap, automatic cleanup, orphan sweep on startup. |
| Paused-task answer flow | `switchboard.py answer <task_id> -` reads from stdin; `/answer` slash command in Claude Code supports direct text OR run-a-command-and-send-output. |
| Live deliberation visibility | Detail view shows currently-active agent + elapsed time + recent runs while a task is in flight. |
| Cost/usage tracking | Per-`agent_run` token counts and (where reported) USD-equivalent cost. Per-message inline details + aggregate on terminal tasks. |
| Adapter context limits | Each adapter declares `max_context_chars`; informational today, basis for future hard limits. |
| Drag-a-folder upload | Dropzone walks folder via `webkitGetAsEntry`, applies the same skip patterns as the sandbox, uploads each file in sequence. |
| Git-diff attachment | `POST /api/git/diff` runs `git diff (+ --cached)` server-side; dashboard button appends to question textarea. |
| Convergence judge | After conclave weak-convergence (synthesis-round already done), one participant arbitrates semantic equivalence and the orchestrator upgrades `agreement_level` accordingly. |
| Inbox filters | Status / mode / search / export-status + "Show last N" quantity + sticky-header scrolling. |
| Tier 2 export tracking | `exported_at` + `export_path` columns on tasks; `POST /api/tasks/{id}/export` marks the task; `POST /api/tasks/export-batch` for bulk export of unexported terminal tasks; dashboard surfaces export indicator + "Re-export" label after first export. |
| DB concurrency hardening | `busy_timeout=30s` on every connection + `with_retry()` decorator on the worker's claim and retention's VACUUM. |
| Codex + Gemini slash-command parity | `clients/` source-of-truth dir; 8 slash commands in Codex (via `~/.codex/skills/switchboard-conclave/SKILL.md`) and Gemini (via `gemini extensions link` of the `switchboard-conclave` extension). Provenance: every call passes `--invoked-by <tool>`. See decision 0007. |
| Detail export (PDF / DOCX / MD / TXT) | `GET /api/tasks/{id}/download?format=...` streams the full task detail; dashboard "Export detail as…" control uses the browser Save dialog (`showSaveFilePicker` where available). `app/services/doc_export.py`. See decision 0008. |
| Open-weight council seats | `deepseek` / `glm` / `qwen` / `kimi` — pluggable, config-driven seats in the dashboard checkbox list. Backing is **OpenRouter** (`OpenRouterAdapter`, pay-per-token, no subscription; `openrouter:` config section). See decisions 0009 (initial Ollama Cloud proof-of-concept, superseded), 0011, and 0014 (Ollama Cloud removal). |
| Sandbox read-access for API seats | When a task has a project sandbox, the OpenRouter seats get a read-only file tree + file contents inlined into their prompt (they have no file-browsing tool, unlike the CLI seats). Sized to each model's context budget; priority-ordered (entry points first); notes omitted files. `app/utils/sandbox_inline.py`. See decision 0012. |
| Settings panel + DB-stored API keys | Dashboard left rail → gear → Settings → API Keys: store/reveal the **OpenRouter** key (eyeball toggle), kept in the `settings` table. Rule: the env var (`OPENROUTER_API_KEY`) wins, else the DB value. `app/api/settings.py`, `app/services/settings_store.py`. See decisions 0010 + 0011. |
| Orphan task reaper | Startup sweep marks tasks stuck in `running` >1h as `failed` with preserved transcript + `task_orphaned` audit log entry. `app/services/orphan_reaper.py`, called from the lifespan in `app/main.py`. Phase 1 of the post-DR plan on `tsk_01KRSW6AS3M66B4RRJE3JFAPRV` — deliberately minimal (no UI, no recovery actions). The full Recovery Console (Phase 3) ships only if stuck tasks are observed in practice. |
| Confidence-weighted synthesis | Per-task `confidence_aggregate` (min/max/mean/count) computed at finalization and persisted in `final_results.confidence_aggregate_json`. API endpoint also returns a `confidence_trajectory` computed on-the-fly from existing `agent_messages`. Dashboard renders both: color-banded stat cards (green/amber/red), a wide-spread caveat ("consensus may be conformist drift"), and a round-by-round dot trail per participant. Synthesis directive now includes each participant's confidence so the synthesizer can weight positions. Phase 2 of post-DR plan. |
| Decision Memory retrieval | TF-IDF cosine similarity over `docs/decisions/*.md`, zero external dependencies. At task creation the top-3 matches (≥0.05 cosine) are frozen in `tasks.prior_art_json` and injected as a "Prior Art" section in every agent prompt. Dashboard renders the matches as a panel between user-request and transcript, with relevance scores. `app/services/decision_memory.py`. Phase 2.5 of post-DR plan. |
| Opt-in Tier 2 trim after export | New `retention.trim_tier2_after_export: bool = false` config flag. When on, the retention worker additionally drops `final_results` rows for tasks already exported to disk (the markdown export is the long-term archive), but only after Tier 3 trimming was insufficient to come back under budget. Still respects `min_task_age_days` and skips parents of live threads. `app/services/retention.py:find_trimmable_tier2_tasks`. |
| Tool-loop for API-based council seats | OpenRouter seats can opt into `read_file` / `list_dir` / `glob` tools instead of getting the whole sandbox inlined. Per-seat `tool_loop: false` default in `openrouter.models[]`. New `tool_call` / `tool_result` message types persist each round of the loop as `agent_messages` rows for full audit-trail visibility; the dashboard renders them as compact ribbons under each turn. Caps: 8 iterations / 256 KiB cumulative read / 3 consecutive malformed calls per turn — any cap fires a forced final-turn POST. DR0015. |
| Decision Projects + provider-neutral evidence layer + neutral synthesis | Persistent `decision_projects` group tasks with reusable evidence URLs; each URL is fetched once, server-side, before dispatch and every participant sees identical extracted text (SHA-256-hashed, SSRF/DNS/redirect-validated). Optional judge/synthesis seats must stay outside the participant set; the synthesizer preserves material dissent and cites only captured evidence IDs. No bundled search vendor; no per-participant mid-round web access. **Supersedes DR0013** (pre-fetched URL attachments v1) outright rather than revising it — DR0013's conclave pressure-test (`tsk_01KRR4B0MWTCN95TEAPYQ2RS4M`) converged on a narrow single-fetch MVP with SSRF controls, immutable snapshot metadata, and an explicit "per-agent access stays deferred" call, all of which DR0027 implements. DR0027, `app/services/evidence.py` + `public_http.py` + `evidence_views.py`. |
| Test suite | Covers protocol, modes, threading, retention (incl. Tier 2 trim), attachments, sandbox, sandbox-inline, judge, db concurrency, export tracking, exporter, provenance, doc export, openrouter adapter, settings API, orphan reaper, confidence aggregate, decision memory, tool-loop |

## Next (in priority order)

> **Active planning:** Measure whether added deliberation earns its cost before introducing automatic escalation. See [`ADAPTIVE_DELIBERATION_PLAN.md`](ADAPTIVE_DELIBERATION_PLAN.md). Milestone 1 adds feedback and compute telemetry only; it must produce zero additional model calls.

1. **Modularize dashboard.js** — file is approaching 2000 lines; still maintainable but ripe for splitting into per-view modules without adopting a framework. Defer until it actively bites.

2. **Inbox tagging** — letting the user attach freeform tags to tasks would scale browsing better than filters alone once the inbox has hundreds of tasks.

3. **Docs polish: tool-loop mode coverage + sandbox requirement** *(small, batch into the next doc pass)* — README's "pluggable open-weight council seats" bullet and `help.html` §11.8 currently describe the tool-loop in general terms. Two clarifications worth adding when the next doc edit comes around: (a) explicitly state that the tool-loop works in **all three modes** (`conclave`, `consult`, `resolve`) — every adapter method (`run_primary` / `run_consultant` / `run_final` / `run_conclave_turn`) routes through `_invoke_dispatch` and gets the same flag treatment, so it's not conclave-only; (b) call out the **sandbox-required silent fallback** — if a seat has `tool_loop: true` but the task has no sandbox attached (`include_sandbox` unchecked or no `project_path`), the adapter silently uses the prompt-only path. Intentional (so a misconfigured task doesn't fail) but easy to overlook when the value isn't materializing on a task.

4. **Decision Memory: partial-supersession / amendment tracking** *(only if observed in practice)* — the current supersession detector handles a binary state: a record is either superseded outright (a `**Status: SUPERSEDED**` banner at the head) or live. Real-world experience after shipping Phase 2.5 already surfaced a subtler case: Decision 11 (OpenRouter seats) is *operationally live for OpenRouter*, but its incidental references to "Ollama Cloud stays in the codebase, disabled by default" are now factually wrong (Decision 14 deleted the adapter entirely). The append-only Charter rule means we don't rewrite ratified records — so the natural fix is a third state. Possible shapes: a `**Status: PARTIALLY AMENDED**` banner convention with an `**Amended by**:` line pointing to the superseding records, plus a "partial" rendering tier on the dashboard (e.g. amber badge, summary excerpt prefixed with "⚠ partially amended"), plus a `partial=true` flag in the retrieval output so prompts can include the record with an explicit "X is no longer accurate; consult Decision N" caveat. Out of scope until Glen actually hits agent confusion on a real task; tracked here so it's not lost.

5. **User-literature positioning pass** *(README, dashboard help, and landing pages)* — lead with governed, cross-vendor decision review rather than "multiple agents," which is increasingly a baseline orchestration capability. Translate the distinction into concrete feature highlights:
   - Independent opinions from different providers, not multiple instances controlled by one provider.
   - Structured challenge and disagreement, not a pile of parallel answers.
   - Preserved minority positions, with the human retaining final authority.
   - The same frozen evidence shown to every participant, with attributable citations.
   - An auditable decision history and cautious, explicitly approved artifact application.
   - Local-first handling of private source code, evidence, and decisions.

   Suggested source paragraph to adapt per surface: *"The AI Conclave is a local, vendor-neutral review board for consequential AI-assisted decisions. Independent models examine the same frozen evidence, challenge one another through structured deliberation, preserve material dissent, and leave an auditable decision record. You remain the final authority, and proposed file changes remain reviewable until you explicitly apply them."* Avoid naming competitors in primary user copy or claiming that multi-agent orchestration itself is unique; competitor context belongs in internal positioning material.

## Considered and Intentionally Not Built

### Layer 2: In-conclave code execution

**Decision date**: 2026-05-11 (post-`tsk_01KRBPT9TRT03KZGJES8Z0BK23`)
**Status**: Deferred indefinitely.

The proposal: extend the per-task permission flags through to the adapter CLIs, so a conclave with `can_run_commands: true` would invoke Codex with `-s workspace-write`, Claude with `--tools "Read,Bash,Edit,Write"`, Gemini with `--approval-mode auto_edit`. Effect: the conclave's participants could actually read/write/execute during deliberation.

**Why declined**:

- **Category confusion.** The conclave's deliberative value depends on three agents reasoning about *the same stable situation*. Once any participant can modify the filesystem during deliberation, the situation changes mid-loop and the others' contributions become reactions to fait accompli rather than independent analysis. Concretely: if Codex writes a refactor in round 1, Gemini in round 2 reads files in a state Codex chose, not the state the conclave started in. The deliberation softens.

- **Race conditions in concurrent writes.** Conclave participants run in parallel (`asyncio.gather`). Two agents editing the same file produces last-write-wins corruption with no audit trail of intent.

- **Output-discipline regression.** With elevated tool access, models drift away from strict JSON output toward tool-use exploration. Already observed once with `--permission-mode plan` on Claude.

- **Redundancy with the interactive CLI.** The Claude Code session driving the AI Conclave Switchboard is itself an execution-capable agent. Adding two more parallel writers (headless Codex + Gemini) gives a coordination problem, not 3× capability.

- **Audit trail divergence.** Decisions today are traceable: deliberate → decide → act. With in-conclave execution, agents act during deliberation in arbitrary order; reconstructing "what did the conclave do to the filesystem and when" is hard.

**The workflow that replaces it**: see `docs/CODING_WORKFLOW.md`. Use the conclave to deliberate, record a decision, then execute in the interactive Claude Code session. The CLIs themselves are the execution layer.

**Reconsider this decision if**: a real use case emerges that the `docs/CODING_WORKFLOW.md` four-step loop genuinely cannot handle with one extra step. Prediction at time of decision: this won't happen. If it does, the right shape is the *explicit-gate design* — a separate "elevated mode" flag that's clearly distinct from deliberation modes, not additive permission checkboxes.

### Auto-applying recommended actions

Recommended actions from agents currently carry `requires_approval: true` flags. The system surfaces them; it does not execute them. Auto-application has been deferred for the same reasons as Layer 2 plus the additional concern that an automated apply path would erode the safety model's "default-deny" stance.

### Real-time dashboard updates (SSE / WebSocket)

Detail view auto-polls every 3 seconds while non-terminal. Real-time push would be slightly more responsive but adds connection-management complexity. Deferred until the polling latency becomes a real complaint.

### Voice interface

Mentioned in the original product plan. No demand observed. Deferred.

### Cloud / multi-user mode

The current service is single-user, local-only. Multi-user would require auth, RBAC, per-user isolation, and rethought permissions. Not in current scope.

## Process

When you consider a feature and decide *not* to build it, add it to "Considered and Intentionally Not Built" with:

- Decision date
- A short summary of the proposal
- Why declined (concrete reasons, not vibes)
- The workflow or alternative that replaces it
- A "reconsider if" condition

This prevents the same proposal from cycling back without new information.


## September 2026 implementation

The repository improvement milestones in `IMPLEMENTATION_PLAN.md` add final-result
and export fidelity, authenticated clients, restart recovery/retry, pinned evidence
transport, exact evidence exposure records, feedback/value metrics, project inspection
and evidence selection, cross-platform CI/browser verification, hashed dependency locks,
and an editable `landing/src` build. Automatic routing and escalation remain deferred
until outcome feedback justifies changing the user's chosen deliberation mode.
