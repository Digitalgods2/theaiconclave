# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

> AI Switchboard helps a human make better decisions by turning multiple AI models into a governed deliberation council — with preserved dissent, human authority, and auditable decision memory. A personal AI decision board for builders, writers, researchers, and technical creators who need more than an answer; they need the reasoning trail.

**AI Switchboard** (a.k.a. the AI Conclave) — a local FastAPI service at `127.0.0.1:8787` that orchestrates structured deliberation between AI coding agents (Codex, Gemini, Claude Code, plus pluggable OpenRouter seats). Single-user, local-only, SQLite-backed.

It is in part **used to design itself** — most architectural decisions came out of conclave deliberations recorded in `docs/decisions/`. Read those before proposing structural changes.

## Commands

```powershell
# Install
pip install -r requirements.txt

# Run the service (foreground)
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787

# Run the full test suite (~187 tests)
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
```

`pytest.ini` sets `asyncio_mode = auto` — async tests don't need the `@pytest.mark.asyncio` decorator.

## Big-picture architecture

### Request lifecycle
1. **Submit** — `POST /api/tasks` (HTTP, dashboard, or slash command) writes a row to `tasks` with `status=pending`.
2. **Claim** — `app/workers/task_worker.py` polls every `worker_poll_interval_seconds` (default 2s), claims one pending task using a `with_retry()`-wrapped UPDATE, sets `status=running`.
3. **Dispatch** — `app/services/orchestrator.py` selects a flow by `mode`: `run_conclave` / `run_resolve` / `run_consult`. Each flow loops adapter calls, recording every prompt + response into `agent_messages` and per-call meta into `agent_runs`.
4. **Pause/resume** — if an agent emits `needs_user_input`, status flips to `awaiting_user_input`. `POST /api/tasks/{id}/answer` flips back to `pending` and the worker reclaims it. `run_resolve` reseeds `prior_messages` from `agent_messages` on resume.
5. **Terminate** — orchestrator writes a `final_results` row, status becomes `completed` / `failed` / `cannot_resolve`. Sandboxes are cleaned up; orphan sandboxes are swept on service startup (`app/main.py` lifespan).

### The three modes (see `docs/SWITCHBOARD_PROTOCOL.md` for the wire format)

| Mode | Shape | Termination |
|---|---|---|
| `conclave` | N equal participants, full-mesh visibility | ≥ `convergence_threshold` participants signal `i_am_done`; weak convergence triggers a synthesis round + judge pass |
| `resolve` | Open-ended, primary-driven | Primary signals `resolved` / `cannot_resolve`, or backstop fires (`max_seconds` / `max_rounds` / repetition guard) |
| `consult` | Fixed 3-step: primary → consultant(s) → primary final | After primary's final message |

### The adapter contract (`app/agents/base.py`, see `docs/AGENT_ADAPTERS.md`)
Every agent (CLI or API) is a `BaseAdapter` subclass. The orchestrator only ever calls adapters through this interface — per-tool quirks (`codex exec --json`, `gemini -p -o json`, `claude -p --output-format json`, OpenRouter HTTP) stay encapsulated. Adapters never retry; they raise `AdapterError(code, message)` which the orchestrator converts to a `ProtocolError` on the task.

Adapter files: `codex_adapter.py`, `gemini_adapter.py`, `claude_adapter.py`, `openrouter_adapter.py`, `fake_adapter.py` (tests + smoke tests; hidden from the dashboard).

OpenRouter seats are **registered at startup from config** (`agent_registry.register_openrouter_models`) — adding a new open-weight seat is a config edit in `openrouter.models[]`, not new code.

### Layers (`app/`)
- `protocol/validators.py` — Pydantic models for the wire format. Schema changes ripple through every adapter.
- `agents/` — adapters (above).
- `services/orchestrator.py` — mode flows + persistence helpers (`_record_message`, `_record_run_*`). This is the longest and most load-bearing file.
- `services/agent_registry.py` — adapter discovery + dynamic registration from config.
- `services/prompt_builder.py` — assembles the prompt sent to each agent (charter + role behavior + role disambiguation + sandbox inline + prior messages).
- `services/sandbox.py` + `utils/sandbox_inline.py` — per-task read-only copy of `project_path`; inline file-tree for API seats that have no file-browsing tool (decision 0012).
- `services/judge.py` — convergence judge: after weak conclave convergence + synthesis, one participant arbitrates semantic equivalence and the orchestrator upgrades `agreement_level`.
- `services/retention.py` — tier-based retention worker (6h cadence). Tier 1 (decisions, charter amendments, unresolved dissent) is never auto-trimmed.
- `services/exporter.py` + `services/doc_export.py` — decision-record markdown export + per-task detail export (PDF/DOCX/MD/TXT).
- `services/settings_store.py` — DB-stored API keys for OpenRouter. **Rule: env var wins over DB value** (`OPENROUTER_API_KEY`).
- `workers/task_worker.py` — the claim loop.
- `api/` — FastAPI routers (`tasks`, `agents`, `git`, `uploads`, `settings`, `health`).
- `dashboard/` — single-page vanilla-JS app served at `/`. `dashboard.js` is ~2000 lines; modularization is a known "Next" item but not yet acted on.

### Persistence
SQLite at `data/switchboard.db`. **WAL mode + `busy_timeout=30s` + `with_retry()` on heavy write paths** are deliberate hardening for the worker/retention/API-call concurrency triangle — don't strip them. Schema lives in `app/database.py`; see `docs/DATABASE_SCHEMA.md` for the table reference.

## Invariants to preserve

These come from ratified decision records and the Conclave Charter (`docs/CONCLAVE_CHARTER.md`, embedded into every agent prompt via `skills/generic/conclave_charter.md`). Read those before changing related behavior.

- **Read-only by default.** Permissions in `config.yaml` and per-task `permissions` are a default-deny model. Adapters do not write to disk during deliberation. A task may NOT escalate beyond what the user submitted.
- **No in-conclave code execution ("Layer 2") — deferred indefinitely.** Conclave participants reason about *the same stable situation*. See `docs/ROADMAP.md` § "Considered and Intentionally Not Built" before re-proposing.
- **Operability before capability** (Charter v1.2). New capability proposals require an *Operability Impact* field in their decision record.
- **Charter v1.2 is binding and is embedded in every prompt.** Amendments go through a `conclave`-mode deliberation, get ratified by the user, and land as a numbered decision record.
- **Env var > DB value** for `OPENROUTER_API_KEY`. Don't invert this.
- **JSON output discipline.** Every adapter parses structured output. The "Codex/Gemini/Claude calls fail with `agent_error: could not extract JSON`" failure mode in `INSTALL.md` is usually a CLI update changing output shape — re-check the adapter's parsing, don't loosen the parser.
- **`--invoked-by <tool>` provenance.** Every CLI slash command passes it; `source_agent` on the task row records it. Preserve this when touching `clients/`.

## Where to make changes

| To change... | Edit... |
|---|---|
| Wire format / message schema | `app/protocol/validators.py` — then every adapter + the prompt builder + the relevant test in `tests/test_protocol.py` |
| Termination rules for a mode | `app/services/orchestrator.py` — `run_conclave` / `run_resolve` / `run_consult` |
| What gets sent to an agent | `app/services/prompt_builder.py` (general) or the adapter's `_build_*` helpers (per-tool framing) |
| Add an open-weight council seat | `config.yaml` → `openrouter.models[]` — no code change |
| Slash command surface | `clients/claude-code-commands/*.md`, `clients/codex-skill/`, `clients/gemini-extension/` — these are the source of truth; `clients/install.py` deploys them |
| Agent role behavior (the "behave like a participant" text) | `skills/generic/*.md` — embedded into prompts at runtime |
| Charter | `skills/generic/conclave_charter.md` (binding) + `docs/CONCLAVE_CHARTER.md` (human-readable mirror) — bump version, write a decision record |

## Documents worth reading before non-trivial work

1. `docs/CODING_WORKFLOW.md` — the canonical deliberate → decide → execute → record loop
2. `docs/SWITCHBOARD_PROTOCOL.md` — wire format, mode definitions
3. `docs/CONCLAVE_CHARTER.md` + `skills/generic/conclave_charter.md` — the binding charter
4. `docs/ROADMAP.md` — shipped, next, and *intentionally not built* (read before proposing features)
5. `docs/SAFETY_MODEL.md` — permission model
6. `docs/AGENT_ADAPTERS.md` — adapter interface contract
7. `docs/TASK_LIFECYCLE.md` — the state machine (incl. resolve-mode pause)
8. `docs/decisions/INDEX.md` — every ratified decision with one-line summary

## Runtime layout (gitignored)

`data/switchboard.db` (+ WAL/SHM), `data/sandboxes/<task_id>/`, `data/uploads/`, `data/exports/`, local `config.yaml`. The DB contains the full text of every deliberation and any source copied into per-task sandboxes — treat it as sensitive.

## Platform notes

Development is on Windows 11 / Python 3.13. The codebase is platform-agnostic but examples in `INSTALL.md` use PowerShell `curl.exe` and backtick line continuations. `pathlib.Path` is used throughout; no hard-coded path separators.
