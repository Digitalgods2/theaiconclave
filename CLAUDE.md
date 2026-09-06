# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

> The AI Conclave helps a human make better decisions by turning multiple AI models into a governed deliberation council — with preserved dissent, human authority, and auditable decision memory. A personal AI decision board for builders, writers, researchers, and technical creators who need more than an answer; they need the reasoning trail.

**The AI Conclave** — a local FastAPI service at `127.0.0.1:8787` (the AI Conclave Switchboard) that orchestrates structured deliberation between AI coding agents (Codex, Gemini, Claude Code, plus pluggable OpenRouter seats). Single-user, local-only, SQLite-backed.

It is in part **used to design itself** — most architectural decisions came out of conclave deliberations recorded in `docs/decisions/`. Read those before proposing structural changes.

`AGENTS.md` carries the style / commit / PR conventions for this repo; this file carries the architecture.

## Commands

```powershell
# Install
pip install -r requirements.txt

# Run the service (foreground)
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787

# Run the full test suite (559 tests)
python -m pytest

# Run a single test file / test
python -m pytest tests/test_conclave_flow.py
python -m pytest tests/test_conclave_flow.py::test_unanimous_convergence -v

# Smoke-test the orchestrator without burning CLI quota (fake adapter)
curl.exe -X POST http://127.0.0.1:8787/api/tasks -H "Content-Type: application/json" --data "@examples/task_request_fake.json"

# Verify each real CLI is reachable
curl.exe -X POST http://127.0.0.1:8787/api/agents/codex/test
curl.exe -X POST http://127.0.0.1:8787/api/agents/gemini/test
curl.exe -X POST http://127.0.0.1:8787/api/agents/claude-code/test

# Install slash commands into ~/.claude, ~/.codex, ~/.gemini
python clients/install.py
python clients/install.py --check

# The CI gates, in the order .github/workflows/ci.yml runs them (Ubuntu + Windows)
python -m compileall -q app clients tools
python -m ruff check app clients tools tests   # correctness rules only: E9, F63, F7, F82
python -m pyright                              # only the modules listed in pyproject.toml [tool.pyright].include
python tools/check_javascript.py               # dashboard JS syntax
python tools/build_landing.py --check          # landing/ is generated; --check fails if it drifts
python tools/audit_dead_code.py                # Python reachability (also asserted by tests/test_dead_code_audit.py)
python -m pytest -q
python tests/browser_smoke.py                  # Playwright; needs `python -m playwright install chromium`
```

`pytest.ini` sets `asyncio_mode = auto` — async tests don't need the `@pytest.mark.asyncio` decorator. `tests/conftest.py` has an **autouse** fixture that pins `SWITCHBOARD_DATA_DIR` to a per-test `tmp_path` and resets the `paths.py` / `config.py` caches, so every test gets an isolated data root for free — never write a test that reaches for the developer's real `data/`.

No formatter is configured and Ruff is deliberately narrow (correctness rules only) — match surrounding style rather than reformatting. Pyright runs in `basic` mode over an explicit allowlist in `pyproject.toml`; new fully-typed modules are meant to be added to that list, not the whole tree at once.

`requirements.txt` / `requirements-dev.txt` are compiled lockfiles with hashes (`requirements.in` / `requirements-dev.in` are the sources). CI installs with `--require-hashes`, so add a dependency to the `.in` file and recompile — never hand-edit the lock.

## Big-picture architecture

### Request lifecycle
1. **Submit** — `POST /api/tasks` (HTTP, dashboard, or slash command) writes a row to `tasks` with `status=pending`.
2. **Claim** — `app/workers/task_worker.py` polls every `worker_poll_interval_seconds` (default 2s), claims one pending task using a `with_retry()`-wrapped UPDATE, sets `status=running`.
3. **Dispatch** — `app/services/orchestrator.py` selects a flow by `mode`: `run_conclave` / `run_resolve` / `run_consult`. Each flow loops adapter calls, recording every prompt + response into `agent_messages` and per-call meta into `agent_runs`.
4. **Pause/resume** — if an agent emits `needs_user_input`, status flips to `awaiting_user_input`. `POST /api/tasks/{id}/answer` flips back to `pending` and the worker reclaims it. `run_resolve` reseeds `prior_messages` from `agent_messages` on resume.
5. **Terminate** — orchestrator writes a `final_results` row, status becomes `completed` / `failed` / `cannot_resolve` / `cancelled` (cancellation comes through `services/task_control.py`). `_post_finalize_hooks` then runs the non-blocking extras (failure-cause tags, trajectory export, artifact capture). Sandboxes are cleaned up; orphan sandboxes are swept on service startup (`app/main.py` lifespan).

### The three modes (see `docs/SWITCHBOARD_PROTOCOL.md` for the wire format)

| Mode | Shape | Termination |
|---|---|---|
| `conclave` | N equal participants, full-mesh visibility | ≥ `convergence_threshold` participants signal `i_am_done`; weak convergence triggers a synthesis round + judge pass, then optionally an independent synthesizer seat |
| `resolve` | Open-ended, primary-driven | Primary signals `resolved` / `cannot_resolve`, or backstop fires (`max_seconds` / `max_rounds` / repetition guard) |
| `consult` | Fixed 3-step: primary → consultant(s) → primary final | After primary's final message |

### The adapter contract (`app/agents/base.py`, see `docs/AGENT_ADAPTERS.md`)
Every agent (CLI or API) is a `BaseAdapter` subclass. The orchestrator only ever calls adapters through this interface — per-tool quirks (`codex exec --json`, `gemini -p -o json`, `claude -p --output-format json`, OpenRouter HTTP) stay encapsulated. Adapters never retry; they raise `AdapterError(code, message)` which the orchestrator converts to a `ProtocolError` on the task.

The four CLI seats subclass **`CliAdapterBase`** (`agents/cli_adapter_base.py`), which owns the four `run_*` methods; a subclass supplies only `name`, `max_context_chars`, and `_invoke()`. `openrouter_adapter.py` and `fake_adapter.py` deliberately do not inherit it (see `docs/AGENT_ADAPTERS.md`), though OpenRouter shares `_response_coercion.parse_and_coerce`.

Adapter files: `codex_adapter.py`, `claude_adapter.py`, `antigravity_adapter.py`, `gemini_adapter.py`, `openrouter_adapter.py`, `fake_adapter.py` (tests + smoke tests; hidden from the dashboard). Every CLI adapter spawns subprocesses through the shared flags in `agents/_spawn.py` — see *Platform notes*.

`gemini_adapter.py` wraps a CLI Google **retired on 2026-06-18** for AI Pro/Ultra/free tiers; it stays in-tree, disabled, for Code Assist Standard/Enterprise licence holders. `antigravity_adapter.py` (the `agy` binary) is its successor and carries the quirks that CLI forces — `-p=` rather than `-p`, prompt over stdin as stream-json because conclave prompts exceed the Windows command-line limit, `status` rather than the exit code as the success signal, and `--mode plan` for read-only (never with `--disable-slash-commands`, which silently voids it). See `docs/AGENT_ADAPTERS.md`.

OpenRouter seats are **registered at startup from config** (`agent_registry.register_openrouter_models`) — adding a new open-weight seat is a config edit in `openrouter.models[]`, not new code. API seats can also run a bounded tool loop (DR0015): the model calls `read_file` / `list_dir` / `glob`, implemented in `services/sandbox_tools.py` against the per-task sandbox root with traversal refusal and byte budgets.

### Layers (`app/`)
- `protocol/validators.py` — Pydantic models for the wire format. Schema changes ripple through every adapter.
- `agents/` — adapters (above).
- `services/orchestrator.py` — mode flows + persistence helpers (`_record_message`, `_record_run_*`). The longest and most load-bearing file (~1650 lines).
- `services/agent_registry.py` — adapter discovery + dynamic registration from config; per-seat readiness (DR0017).
- `services/prompt_builder.py` — assembles the prompt sent to each agent (charter + role behavior + role disambiguation + sandbox inline + prior messages).
- `services/prompt_budget.py` — single source of truth for how much `prior_messages` history may ride along. Every public builder in `prompt_builder.py` goes through it; it trims oldest-first to fit the adapter's `max_context_chars` and reports the drop count so the prompt can say the cut happened. Deep continue-threads used to silently blow CLI context windows before this existed.
- `services/sandbox.py` + `utils/sandbox_inline.py` — per-task read-only copy of `project_path`; inline file-tree for API seats that have no file-browsing tool (decision 0012).
- `services/judge.py` — convergence judge: after weak conclave convergence + synthesis, a seat arbitrates semantic equivalence and the orchestrator upgrades `agreement_level`.
- `services/synthesis.py` — optional **independent** final synthesizer (`orchestration.synthesis_agent`, DR0027). It must not be a participant; it is required to preserve material dissent and to cite only captured evidence IDs. `_run_independent_synthesis` in the orchestrator wires it in; failure is non-fatal (returns `None`, the ordinary assembled final stands).
- `services/evidence.py` + `services/public_http.py` + `services/evidence_views.py` — the provider-neutral evidence layer (DR0027). A URL is fetched **once, before deliberation**, and its exact extracted text + SHA-256 is persisted to `evidence_snapshots`, so every participant sees identical source bytes. `public_http.py` connects only to the public IP it validated (host routing/TLS still use the hostname; private/link-local targets, credentials in URLs, and unvalidated redirects are refused). `evidence_views.py` builds the deterministic, budgeted excerpts that go into prompts, wrapped as untrusted data.
- `services/results.py` — the one persisted-result contract shared by HTTP and every export format. Change a final-result field here, not per-exporter.
- `services/task_control.py` — process-local registry of the running task's asyncio task, so `POST /api/tasks/{id}/cancel` can interrupt a coroutine mid-adapter-call. The task row stays the durable source of truth for status.
- `services/task_metrics.py` — content-free value/compute aggregates over `agent_runs` plus the three decision-feedback signals (`decision_changed`, `material_risk_found`, `extra_review_worth_it`). Saving feedback never schedules work.
- `services/action_plan.py` — compiles a final result's recommended actions into classified, permission-annotated steps. **Advisory only**: never executes, never creates approvals, never pauses a task, never drops blocked steps (DR0019).
- `services/artifacts.py` — draft outputs written to `user_data_root()/artifacts`, *not* into the user's project. Applying an artifact to the project is a separate explicit API/dashboard action (DR0020).
- `services/decision_memory.py` — TF-IDF cosine retrieval over `docs/decisions/*.md` so past decision records surface as context on new tasks. No external deps; index rebuilds on directory mtime change.
- `services/trace_analyzer.py` — rule-based failure-cause tagging, no LLM (DR0022).
- `services/trajectory_exporter.py` — versioned JSON trajectory dumps (DR0023).
- `services/retention.py` — tier-based retention worker (6h cadence). Tier 1 (decisions, charter amendments, unresolved dissent) is never auto-trimmed.
- `services/exporter.py` + `services/doc_export.py` — decision-record markdown export + per-task detail export (PDF/DOCX/MD/TXT).
- `services/settings_store.py` — DB-stored API keys for OpenRouter. **Rule: env var wins over DB value** (`OPENROUTER_API_KEY`).
- `services/migration.py`, `services/pidlock.py`, `services/orphan_reaper.py` — startup machinery; see *Startup sequence* below.
- `workers/task_worker.py` — the claim loop.
- `api/` — FastAPI routers, all mounted in `app/main.py`: `tasks` (+ its `trajectories_router`), `task_artifacts`, `task_feedback`, `agents`, `evidence`, `projects`, `metrics`, `git`, `uploads`, `settings`, `help`, `health`.
- `dashboard/` — single-page vanilla-JS app served at `/`. `dashboard.js` is ~5300 lines; modularization is a known "Next" item but not yet acted on.

### Decision projects and evidence (DR0027)
`decision_projects` group tasks and carry user-authored instructions plus reusable evidence URLs; `evidence_snapshots` hold the frozen text. The deliberate non-goals from DR0027 are as binding as the features: **no bundled search vendor** (operators may configure a generic JSON search endpoint under `evidence.search_endpoint`, or capture URLs directly), **no per-participant web access during a round**, and **no reuse of a participant as its own judge or synthesizer**. Remote content is labelled untrusted and isolated from prompt instructions; prompt-injection-like phrasing is counted as a quality signal, not filtered silently.

### Runtime state root (DR0016)
All writable state routes through `app/utils/paths.py::user_data_root()`, resolved in this order:

1. `SWITCHBOARD_DATA_DIR` env var (tests, CI, packagers).
2. **Dev-mode anchor** — the nearest ancestor directory containing *both* `pyproject.toml` and `config.example.yaml`; its `data/` becomes the root. This is why `pyproject.toml` stays at the repo root even though nothing is packaged from it — removing it flips a checkout into packaged-app behavior.
3. Platform user-data dir: `%LOCALAPPDATA%\The AI Conclave`, `~/Library/Application Support/The AI Conclave`, or `$XDG_DATA_HOME/ai-conclave`.

`config.database.path = None` means "use `default_db_path()`"; an explicit path in `config.yaml` still wins.

### Startup sequence (`app/main.py` lifespan — the order is load-bearing)
Legacy-directory rename → **`migration.maybe_migrate()` as the first awaitable** (no writer may open the destination root before the first-run copy finishes) → config + logging → **pidlock** → `init_database` → registry init → orphan reaper → retention worker → sandbox sweep.

`services/pidlock.py` enforces **single instance** via `<user_data_root>/switchboard.pid` (PID + process creation time, so PID reuse can't fool it). Two live services polling the same SQLite race for tasks and can run one task under different registries. If startup dies with "another AI Conclave Switchboard is running", find and stop the old process — don't delete the lockfile blindly; a genuinely stale lock is taken over automatically.

### Persistence
SQLite at `<user_data_root>/switchboard.db`. **WAL mode + `busy_timeout=30s` + `with_retry()` on heavy write paths** are deliberate hardening for the worker/retention/API-call concurrency triangle — don't strip them. Base schema lives in `app/database.py`; see `docs/DATABASE_SCHEMA.md` for the table reference.

Schema changes go in **`app/schema_migrations.py`** as a numbered, transactional migration appended to `MIGRATIONS` — `init_database()` calls `apply_migrations()` and records each version in the `schema_migrations` table (DR0027 replaced the old unversioned additive-column block). Never renumber or edit a migration that has shipped; append a new one.

## Invariants to preserve

These come from ratified decision records and the Conclave Charter (`docs/CONCLAVE_CHARTER.md`, embedded into every agent prompt via `skills/generic/conclave_charter.md`). Read those before changing related behavior.

- **Read-only by default.** Permissions in `config.yaml` and per-task `permissions` are a default-deny model. Adapters do not write to disk during deliberation. A task may NOT escalate beyond what the user submitted. Draft outputs go to the app-owned artifacts store, never straight into the user's project.
- **No in-conclave code execution ("Layer 2") — deferred indefinitely.** Conclave participants reason about *the same stable situation*. See `docs/ROADMAP.md` § "Considered and Intentionally Not Built" before re-proposing; DR0025 likewise deferred a backend tool-plugin surface.
- **Operability before capability** (Charter v1.2). New capability proposals require an *Operability Impact* field in their decision record.
- **Charter v1.3 is binding and is embedded in every prompt.** v1.3 adds Evidence Norms (DR0021) to the v1.2 base. Amendments go through a `conclave`-mode deliberation, get ratified by the user, and land as a numbered decision record.
- **Env var > DB value** for `OPENROUTER_API_KEY`. Don't invert this.
- **Neutral seats stay neutral** (DR0027). `orchestration.judge_agent` / `synthesis_agent` must not appear in the participant set, and the final synthesizer preserves material dissent rather than smoothing it away.
- **Evidence is captured once, before deliberation, and never re-fetched per agent** (DR0027). Snapshots are immutable and hashed; a model-supplied citation ID that doesn't resolve is surfaced, not silently dropped. Agents get no general browser or mid-deliberation network access.
- **JSON output discipline.** Every adapter parses structured output. The "Codex/Gemini/Claude calls fail with `agent_error: could not extract JSON`" failure mode in `INSTALL.md` is usually a CLI update changing output shape — re-check the adapter's parsing, don't loosen the parser.
- **`--invoked-by <tool>` provenance.** Every CLI slash command passes it; `source_agent` on the task row records it. Preserve this when touching `clients/`.

## Where to make changes

| To change... | Edit... |
|---|---|
| Wire format / message schema | `app/protocol/validators.py` — then `agents/cli_adapter_base.py` (all four CLI seats at once), `openrouter_adapter.py`, `fake_adapter.py`, the prompt builder, and `tests/test_protocol.py` |
| Add a CLI seat | Subclass `CliAdapterBase`, implement `_invoke()` + `readiness()`/`test_connection()`, register in `agent_registry`, add a `config.yaml` block |
| Termination rules for a mode | `app/services/orchestrator.py` — `run_conclave` / `run_resolve` / `run_consult` |
| What gets sent to an agent | `app/services/prompt_builder.py` (general) or the adapter's `_build_*` helpers (per-tool framing); size/trim policy lives in `app/services/prompt_budget.py` |
| Add an open-weight council seat | `config.yaml` → `openrouter.models[]` — no code change |
| Tools an API seat can call mid-turn | `app/services/sandbox_tools.py` (implementations) + the OpenRouter adapter's tool-loop turn handling (DR0015) |
| Slash command surface | `clients/claude-code-commands/*.md`, `clients/codex-skill/`, `clients/gemini-extension/` — these are the source of truth; `clients/install.py` deploys them |
| Agent role behavior (the "behave like a participant" text) | `skills/generic/*.md` — embedded into prompts at runtime |
| Charter | `skills/generic/conclave_charter.md` (binding) + `docs/CONCLAVE_CHARTER.md` (human-readable mirror) — bump version, write a decision record |
| Failure-cause tag rules | `app/services/trace_analyzer.py::classify_failure_causes` — rule-based, no LLM. Add a `FailureCause` enum member in `app/protocol/validators.py` first, then a rule. Tested in `tests/test_trace_analyzer.py` (DR0022) |
| Trajectory export schema | `app/services/trajectory_exporter.py` — bump `_TRAJECTORY_SCHEMA_VERSION` on breaking shape changes; written automatically by the orchestrator's `_post_finalize_hooks` and on demand via `/api/tasks/{id}/trajectory/export` (DR0023) |
| Database schema | Append a numbered migration to `MIGRATIONS` in `app/schema_migrations.py` (transactional, never renumbered) + update `docs/DATABASE_SCHEMA.md` |
| Evidence fetching / URL safety | `app/services/evidence.py` (capture, hashing, limits) + `app/services/public_http.py` (transport, DNS/redirect validation); prompt-side excerpts in `evidence_views.py`. Config under `evidence:` in `config.yaml` (DR0027) |
| Final-result shape as HTTP + every export sees it | `app/services/results.py` — the shared contract; then `exporter.py` / `doc_export.py` |
| Cancelling a running task | `app/services/task_control.py` + the adapter's cancellation handling (child-process teardown is the adapter's job) |
| Dashboard plugin | Drop a JS file under `app/dashboard/plugins/<name>.js` and list it in `app/dashboard/plugins/manifest.json`; copy-paste template at `plugins/example-hello.js`. Four extension points: `sidebarTabs`, `inboxRowActions`, `inboxFilters`, `detailPanels` (DR0024) |

## Documents worth reading before non-trivial work

1. `docs/CODING_WORKFLOW.md` — the canonical deliberate → decide → execute → record loop
2. `docs/SWITCHBOARD_PROTOCOL.md` — wire format, mode definitions
3. `docs/CONCLAVE_CHARTER.md` + `skills/generic/conclave_charter.md` — the binding charter
4. `docs/ROADMAP.md` — shipped, next, and *intentionally not built* (read before proposing features)
5. `docs/SAFETY_MODEL.md` — permission model
6. `docs/AGENT_ADAPTERS.md` — adapter interface contract
7. `docs/TASK_LIFECYCLE.md` — the state machine (incl. resolve-mode pause)
8. `docs/decisions/INDEX.md` — every ratified decision with one-line summary (currently through DR0027)

## Runtime layout (gitignored)

Under `user_data_root()` — in a checkout, that is `<repo>/data/`: `switchboard.db` (+ WAL/SHM), `switchboard.pid`, `sandboxes/<task_id>/`, `uploads/`, `exports/`, `artifacts/`, and (outside dev mode) `config.yaml`. The DB contains the full text of every deliberation and any source copied into per-task sandboxes — treat it as sensitive.

## Platform notes

Development is on Windows 11 / Python 3.13. The codebase is platform-agnostic but examples in `INSTALL.md` use PowerShell `curl.exe` and backtick line continuations. `pathlib.Path` is used throughout; no hard-coded path separators.

**Windows subprocess spawning:** CLI adapters must spawn children with the flags in `app/agents/_spawn.py` (`CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`; an empty dict elsewhere). Without them, a console control event delivered to the server's console kills every freshly-spawned CLI child with `STATUS_CONTROL_C_EXIT` (`3221225786`) while uvicorn itself survives — the symptom is every CLI seat dying ~1s after spawn. Timeouts use `proc.kill()`, so children never need Ctrl-C handling.
