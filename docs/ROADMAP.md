# Roadmap

This document tracks what's shipped, what's next, and what's been *intentionally not built* and why. The third category is as important as the first two — recording that a feature was considered and consciously declined prevents future cycles of "should we add X?" without context.

## Shipped (MVP)

| Capability | Notes |
|---|---|
| Charter v1.0 + v1.1 (Multimodal Disagreement) + v1.2 (Operability before capability) | `skills/generic/conclave_charter.md`. v1.2 also added the *Operability Impact* field to Decision Records. |
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
| Open-weight council seats | `deepseek` / `glm` / `qwen` — pluggable, config-driven seats in the dashboard checkbox list. Default backing is **OpenRouter** (`OpenRouterAdapter`, pay-per-token, no subscription; `openrouter:` config section). An `OllamaCloudAdapter` also exists (`ollama_cloud:` section, disabled by default — its big models need a paid Ollama Cloud subscription). See decisions 0009 + 0011. |
| Sandbox read-access for API seats | When a task has a project sandbox, the OpenRouter / Ollama seats get a read-only file tree + file contents inlined into their prompt (they have no file-browsing tool, unlike the CLI seats). Sized to each model's context budget; priority-ordered (entry points first); notes omitted files. `app/utils/sandbox_inline.py`. See decision 0012. |
| Settings panel + DB-stored API keys | Dashboard left rail → gear → Settings → API Keys: store/reveal the **OpenRouter** and **Ollama Cloud** keys (eyeball toggle), kept in the `settings` table. Rule: the env var (`OPENROUTER_API_KEY` / `OLLAMA_API_KEY`) wins, else the DB value. `app/api/settings.py`, `app/services/settings_store.py`. See decisions 0010 + 0011. |
| Test suite | 177 tests across protocol, modes, threading, retention, attachments, sandbox, sandbox-inline, judge, db concurrency, export tracking, exporter, provenance, doc export, ollama adapter, openrouter adapter, settings API |

## Next (in priority order)

1. **Re-draft DR0013 (pre-fetched URL attachments) as a narrower v2 spec** — the v1 proposal was pressure-tested in `tsk_01KRR4B0MWTCN95TEAPYQ2RS4M` (conclave mode, 3 AIs, minor_disagreement) and unanimously refused as written. The conclave's load-bearing critiques to fold into v2:
   - Strike the Multimodal Disagreement transfer argument. Image perceptual divergence is incompatible perception of a fixed artifact; search-result divergence is resolvable retrieval divergence. The analogy doesn't hold.
   - Scope v1 down to **1 URL per task, API-only (no dashboard UI), no `readability-lxml`** — many docs / package registries / API pages don't expose evidence in static HTML; treat extraction as best-effort with raw-source metadata preserved for audit.
   - Address the **staleness-in-multi-round-conclaves** failure mode the original proposal missed (a once-at-dispatch fetch can drift from reality during a 30-minute conclave on a fast-moving page).
   - Explicit safeguards: **SSRF policy**, prompt-injection awareness on fetched content, retention/export semantics, per-adapter context-budget visibility (which content was kept/trimmed and why).
   - Acknowledge **per-agent web access as deferred-but-viable**, not rejected on principle. The conclave's Codex / Gemini positions favor "shared-snapshot first, per-agent later as a discovery-layer that pins selected results into shared snapshots." Claude argued the four proposed mitigations are already sufficient for per-agent v1.
   - Critical design question to answer before code: *which artifact is authoritative for deliberation and audit — raw bytes, rendered DOM, extracted text, summarized text, or the token-trimmed content actually sent to each adapter?*
   When picked up: re-write `docs/decisions/0013_prefetched_url_attachments.md` as v2, ratify via `/decide` on the parent task or a follow-up conclave, then implement. (Capability addition; the Operability Impact section needs honest treatment — the v1 "no exception needed" claim was over-confident per the conclave.)

2. **Crash-safe worker / orphan task reaper** — if the worker dies mid-task, the task stays in `running` forever. Implement a startup sweep that resets tasks stuck in `running` for more than a configurable threshold (e.g., 1 hour) back to `pending` or marks them `failed` with an explanatory error_message. (Operability item — favored under charter v1.2.)

2. **Tool-loop architecture for the API-based council seats** — let the OpenRouter / Ollama seats *pull specific files on demand* instead of getting the whole sandbox pre-inlined. The CLI seats already do this natively (Codex's `-C`, Claude's `--add-dir` + `Read` tool, Gemini's `--include-directories`); the API seats currently inline everything into the prompt, which is the right v1 but degrades when the codebase exceeds the model's effective attention budget (~50–80K tokens). Real-world evidence: on a code-review task with ~95K-token inlined prompts, the open-weight seats fell back to template best-practices instead of citing specific files — the attention budget, not the parameter count, was the bottleneck. Shape: a `read_file` / `list_dir` tool offered to the agent as an OpenAI-style function-call; the adapter intercepts the tool call, reads from the sandbox, returns the content, repeats until the agent produces its structured turn JSON. Bounded by per-turn iteration cap and a total-bytes-read budget. Operability impact: positive (better signal per quota dollar on the metered seats); flagged as the natural follow-up in decision 0012's Open Questions. (Capability addition; would get a decision record + Operability Impact when planned.)

3. **Trim Tier 2 after export (opt-in)** — the retention policy says Tier 2 is "retain indefinitely until exported." With export tracking now in place, an optional retention amendment could allow Tier 2 trim after an explicit export, freeing the corresponding `final_results` row. (Operability item.)

4. **Modularize dashboard.js** — file is approaching 2000 lines; still maintainable but ripe for splitting into per-view modules without adopting a framework. Defer until it actively bites.

5. **Inbox tagging** — letting the user attach freeform tags to tasks would scale browsing better than filters alone once the inbox has hundreds of tasks.

## Considered and Intentionally Not Built

### Layer 2: In-conclave code execution

**Decision date**: 2026-05-11 (post-`tsk_01KRBPT9TRT03KZGJES8Z0BK23`)
**Status**: Deferred indefinitely.

The proposal: extend the per-task permission flags through to the adapter CLIs, so a conclave with `can_run_commands: true` would invoke Codex with `-s workspace-write`, Claude with `--tools "Read,Bash,Edit,Write"`, Gemini with `--approval-mode auto_edit`. Effect: the conclave's participants could actually read/write/execute during deliberation.

**Why declined**:

- **Category confusion.** The conclave's deliberative value depends on three agents reasoning about *the same stable situation*. Once any participant can modify the filesystem during deliberation, the situation changes mid-loop and the others' contributions become reactions to fait accompli rather than independent analysis. Concretely: if Codex writes a refactor in round 1, Gemini in round 2 reads files in a state Codex chose, not the state the conclave started in. The deliberation softens.

- **Race conditions in concurrent writes.** Conclave participants run in parallel (`asyncio.gather`). Two agents editing the same file produces last-write-wins corruption with no audit trail of intent.

- **Output-discipline regression.** With elevated tool access, models drift away from strict JSON output toward tool-use exploration. Already observed once with `--permission-mode plan` on Claude.

- **Redundancy with the interactive CLI.** The Claude Code session driving Switchboard is itself an execution-capable agent. Adding two more parallel writers (headless Codex + Gemini) gives a coordination problem, not 3× capability.

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
