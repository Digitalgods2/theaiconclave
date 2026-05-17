# The AI Conclave (AI Switchboard)

> **AI Switchboard helps a human make better decisions by turning multiple AI models into a governed deliberation council — with preserved dissent, human authority, and auditable decision memory.**
>
> A personal AI decision board for builders, writers, researchers, and technical creators who need more than an answer. They need the reasoning trail.

A local background service that lets AI coding agents — **Codex, Gemini, and Claude Code** — consult one another through structured deliberation instead of free-form chat. You ask a question; the conclave deliberates; you get a verdict with the disagreements surfaced verbatim, not flattened.

It's a tool that is, in part, **used to design itself** — most of its governing decisions came out of the three AIs deliberating about Switchboard's own architecture (see [`docs/decisions/`](docs/decisions/INDEX.md)).

---

## What it does

Three AIs sit "in the house" and talk to each other through a mediator. The mediator (Switchboard) controls cost with time/round backstops, records every exchange to SQLite, surfaces a live view of the deliberation, and produces a structured final result. Above every prompt sits the **Conclave Charter** — a constitutional layer (currently v1.2) that governs reasoning norms, dissent norms, multimodal-disagreement handling, the "operability before capability" principle, permissions, and decision records.

You drive it from inside whichever CLI you're already working in — Claude Code, Codex, or Gemini — via slash commands, or from the web dashboard, or directly over HTTP.

### Three deliberation modes

| Mode | Shape | Termination | Use for |
|---|---|---|---|
| **`conclave`** | N **equal** participants, full-mesh visibility, no primary. Every round, every participant posts one position + a convergence signal. | When ≥ `convergence_threshold` (default 1.0 = unanimous) of participants signal `i_am_done`. Weak convergence triggers a synthesis round + a judge pass. | Genuine multi-AI deliberation. "Ask the conclave." |
| **`resolve`** | Open-ended, primary-driven loop. Each turn the primary signals `resolved` / `needs_more_rounds` / `needs_user_input` / `cannot_resolve`. | When the primary signals done (and consultants concur), or a cost/time/repetition backstop fires. Goal-based, not turn-capped. | "Let Codex handle this." Drilling to a real answer. |
| **`consult`** | Bounded second-opinion exchange: primary proposes → consultant(s) critique → primary finalizes. Fixed three-step. | After the primary's final message. | Quick review, not a full deliberation. "Get a second opinion." |

### Highlights

- **Three real AI adapters** — Codex (`codex exec --json`), Gemini (`gemini -p -o json`), Claude Code (`claude -p --output-format json`). All read-only by default, all JSON-output-disciplined.
- **Pluggable open-weight council seats** — `deepseek` / `glm` / `qwen` / `kimi` (and anything else you list in config) appear in the same checkbox list as the CLI agents. Backing is **OpenRouter** — pay-per-token, no subscription, ~$0.001–$0.02 per conclave turn — via a single config-driven adapter. Brings a genuinely outside-the-OpenAI/Google/Anthropic-axis voice to a deliberation.
- **Settings panel** — a narrow left rail with a gear icon → Settings → API Keys: store/reveal the OpenRouter key (password field + eyeball toggle), kept in the local DB. Rule: the env var (`OPENROUTER_API_KEY`) wins, else the DB value.
- **Charter v1.2**, embedded in every participant prompt. Amendments go through a conclave-mode deliberation, get ratified by the user, and land as a numbered decision record.
- **Multimodal attachments** — text / Markdown / PDF inlined; images passed natively to each adapter (no lossy text conversion). The charter's *Multimodal Disagreement* section forbids synthesizing visual-perception disputes — they get escalated to the user instead.
- **Project sandbox** — a per-task read-only copy of your code project so agents can browse source during a deliberation without write/execute risk. (In-conclave write/execute — "Layer 2" — was [considered and intentionally not built](docs/ROADMAP.md).)
- **Threading** — `parent_task_id`, ancestry walks, prior-thread context auto-injected into follow-ups (`/continue`).
- **Decision records** — significant work closes with a structured record (what was chosen, why, what was rejected, known risks, open questions, who keeps continuity, and — for capability/infrastructure changes — an Operability Impact field). See [`docs/decisions/INDEX.md`](docs/decisions/INDEX.md).
- **Decision Memory** — every new task auto-retrieves the most relevant past decision records (TF-IDF over `docs/decisions/`) and surfaces them as a *Prior Art* section both in agent prompts and on the dashboard, so settled questions don't get re-litigated.
- **Confidence-weighted convergence** — every conclave's final result carries an aggregate confidence stat (min/max/mean) plus a per-agent round-by-round trajectory, so you can see whether `consensus` was 4×0.95 (robust) or 1×0.95 + 3×0.4 (conformist drift). A wide-spread caveat fires automatically when participants converged with materially different certainty levels.
- **Retention policy** — tier-based: Tier 1 (never trimmed — decisions, charter amendments, unresolved dissent), Tier 2 (retain until exported), Tier 3 (agent messages — trimmed first). Operational triggers at 2 GB DB size / 1,000 tasks; a 6-hour worker. Opt-in `trim_tier2_after_export` lets the worker also drop `final_results` for tasks already exported to disk.
- **Tier 2 export/archive** — `exported_at` tracking, bulk export endpoint, inbox filter.
- **Orphan task reaper** — startup sweep marks any task stuck in `running` for >1h as `failed` with preserved transcript and a `task_orphaned` audit-log entry. Bare-minimum recoverability without UI surface — the full recovery console is intentionally deferred until stuck tasks are observed in practice.
- **Live deliberation visibility** — the dashboard shows the currently-active agent + elapsed time + recent runs while a task is in flight.
- **Cost/usage tracking** — per-`agent_run` token counts and (where the provider reports it) USD-equivalent cost; per-message inline + aggregate on terminal tasks.
- **Provenance** — every task records which CLI submitted it (`source_agent`: `claude-code` / `codex` / `gemini` / `dashboard` / `api`).
- **SQLite concurrency hardening** — WAL mode, `busy_timeout=30s`, a `with_retry()` wrapper on the heaviest write paths.
- **Dashboard** — single-page vanilla-JS app served from FastAPI at `/`. Inbox with status/mode/search/export filters, detail view with transcript, decision panel, drag-a-folder upload, git-diff attachment.
- **Test suite** (210 tests) covering protocol, modes, threading, retention (incl. Tier 2 trim), attachments, sandbox, sandbox-inline, judge, DB concurrency, export tracking, exporter, provenance, document export, the OpenRouter adapter, the settings API, orphan reaper, confidence aggregate, and Decision Memory.

---

## Quick start

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml      # edit if you want to change ports / adapter paths
uvicorn app.main:app --host 127.0.0.1 --port 8787
```

Verify:

```bash
curl http://127.0.0.1:8787/api/health
# {"status":"ok"}
```

Open the dashboard at **http://127.0.0.1:8787/**.

### Submit a task over HTTP

```bash
# conclave mode — three real AIs deliberate
curl -X POST http://127.0.0.1:8787/api/tasks \
  -H "Content-Type: application/json" \
  -d @examples/task_request_conclave.json

# or use the bundled fake adapter for a fast offline smoke test
curl -X POST http://127.0.0.1:8787/api/tasks \
  -H "Content-Type: application/json" \
  -d @examples/task_request_fake.json
```

Either returns `{"task_id":"tsk_...","status":"pending"}`. Poll for the result:

```bash
curl http://127.0.0.1:8787/api/tasks/tsk_<id>
```

If a task pauses asking you a question (`status: awaiting_user_input`):

```bash
curl -X POST http://127.0.0.1:8787/api/tasks/tsk_<id>/answer \
  -H "Content-Type: application/json" \
  -d '{"answer": "your answer here"}'
```

It moves back to `pending`, the worker re-claims it, and the loop continues.

---

## Driving it from your CLI (slash commands)

The conclave is invokable from inside **Claude Code**, **Codex**, and **Gemini** sessions with feature parity. Source of truth lives in [`clients/`](clients/README.md); deploy with:

```bash
python clients/install.py            # install all three
python clients/install.py --check    # report what's installed
```

| Command | What it does |
|---|---|
| `/conclave <question>` | 3-AI conclave deliberation |
| `/consult <agent> <question>` | quick second opinion from one named agent |
| `/secondopinion [topic]` | second opinion on the current conversation |
| `/decide <task\|latest> <text>` | record your authoritative decision on a task |
| `/decision <task\|latest>` | fetch a task's decision + context, ready to act on |
| `/continue <parent\|latest> <question>` | threaded follow-up (inherits mode + agents, auto-loads prior context) |
| `/thread <task\|latest>` | show the ancestry chain |
| `/answer <task\|latest> <text>` | answer a paused task |

> **Note on Codex:** Codex has no literal `/<name>` slash commands — it activates the `switchboard-conclave` skill from trigger phrases ("ask the conclave", "record my decision", …). Claude Code and Gemini get literal slash commands. See [`clients/README.md`](clients/README.md) for why the three shapes differ.

Every CLI invocation passes `--invoked-by <tool>` so the task's provenance is recorded accurately.

---

## The four-step coding loop

Switchboard's intended workflow for using the conclave during real coding work (see [`docs/CODING_WORKFLOW.md`](docs/CODING_WORKFLOW.md)):

1. **Deliberate** — `/conclave <design question>` (optionally with a project sandbox or git diff attached).
2. **Decide** — `/decide latest <your call, in your own words>`. This becomes the task's permanent record.
3. **Execute** — act on the decision in your interactive CLI session. The CLI *is* the execution layer; the conclave is the deliberation layer.
4. **Record** — significant work closes with a decision record in `docs/decisions/`.

In-conclave execution was deliberately not built — the conclave's value depends on three agents reasoning about *the same stable situation*; letting any participant mutate the filesystem mid-loop softens the deliberation. See the ROADMAP's "Considered and Intentionally Not Built" section.

---

## Model selection and pricing

The design rule for this app is **top-shelf model quality over per-token cost**. The conclave's value depends on each agent reasoning well — saving a few cents per turn by picking a weaker model defeats the purpose. The shipped defaults are the current top-tier model in each provider's lineup:

| Seat | Declared model | Per-million $ in · out | Rationale |
|---|---|---|---|
| `codex` | `openai/gpt-5.5` | $5 · $30 | Flagship general. `gpt-5.5-pro` is 6× the cost for marginal gains. |
| `claude-code` | `anthropic/claude-opus-4.7` | $5 · $25 | Top reasoning. The `-fast` variant is 6× the cost for marginal speed. |
| `gemini` | `google/gemini-2.5-pro` | $1.25 · $10 | Stable flagship. `3.1-pro-preview` is newer but preview-tagged. |
| Open-weight (OpenRouter) | `deepseek-chat`, `glm-4.6`, `qwen3-coder`, `kimi-k2.6` | $0.22–$0.73 · $0.89–$3.49 | Outside-axis voices — different training data, different blind spots. |

Override any of these by editing `agents.<name>.model_slug` (for CLIs) or `openrouter.models[]` (for open-weight seats) in `config.yaml` and restarting.

### The Pricing view

The violet `$` glyph in the dashboard sidebar opens a sortable pricing table. It shows, per seat:

- The **type** (CLI in subscription mode, CLI in API mode, OpenRouter)
- The **model in use** (detected from the CLI's own config when possible, otherwise declared in `config.yaml`)
- **$/M input + output rates** (pulled live from OpenRouter's catalog; cached 5 minutes)
- **Estimated per-turn cost** (input × 5K + output × 1K tokens)
- A **drift indicator** when the CLI's actually-configured model disagrees with `config.yaml`

### Auth-mode detection (subscription vs API)

Each frontier CLI can authenticate via OAuth/subscription (default — your Claude Pro / ChatGPT Plus / Gemini Advanced subscription) or via API key (per-token billing at provider rates). The dashboard detects which mode each CLI is currently in by reading the CLI's own auth-state file:

- **Codex** — `~/.codex/auth.json` carries an explicit `auth_mode` field (`"apikey"` = API, `"chatgpt"` = subscription)
- **Gemini** — `~/.gemini/settings.json` has `security.auth.selectedType` (substrings `"api"` or `"key"` = API, `"oauth-personal"` = subscription)
- **Claude Code** — `~/.claude/.credentials.json` presence = OAuth subscription. (If Claude Code introduces an explicit mode marker in a future version, the detection logic in `app/api/agents.py` extends the same way as Codex's.)

Env vars (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` / `GOOGLE_API_KEY`) are a **fallback signal** of API intent only when the file gives no clear answer — the file's explicit selection wins.

Switching auth modes on the CLI side (e.g., `codex logout` + log back in with API) requires a Switchboard service restart to be reflected in the dashboard. The auth files are re-read on every request, but uvicorn doesn't auto-reload Python source unless launched with `--reload`.

See [`docs/help` section 4.5–4.8](app/dashboard/help.html) for the full operational reference, including caveats on each CLI's file format and how to extend detection when a CLI version ships a new schema.

---

## Running the tests

```bash
pytest
```

187 tests. Key files:

| File | Covers |
|---|---|
| `tests/test_protocol.py` | schema validation, round-trip, mode/permission invariants, versioning |
| `tests/test_fake_adapter.py` | end-to-end consult flow, primary timeout, consultant consensus |
| `tests/test_resolve_flow.py` | resolve termination paths (immediate, multi-round, user-input pause/resume, cannot-resolve, repetition guard) |
| `tests/test_conclave_flow.py` | conclave convergence, synthesis round, majority thresholds |
| `tests/test_judge.py` | convergence judge upgrading agreement level after synthesis |
| `tests/test_thread_flow.py` | threading, ancestry walks, cycle guard |
| `tests/test_retention.py` | tier-based selection, operational triggers |
| `tests/test_sandbox.py` | project sandbox copy, ignore patterns, cap, cleanup |
| `tests/test_export_tracking.py` | Tier 2 export marking, bulk export, inbox filter |
| `tests/test_db_concurrency.py` | busy_timeout, WAL, `with_retry` semantics |
| `tests/test_provenance.py` | `source_agent` round-trip, `--invoked-by` flag parser |
| `tests/test_exporter.py` | decision-record markdown export |

---

## Layout

| Path | Contents |
|---|---|
| `app/` | FastAPI service |
| `app/protocol/` | Pydantic models for the wire format |
| `app/agents/` | Adapter base class + per-tool adapters (codex, gemini, claude-code, fake) |
| `app/services/` | Orchestrator, agent registry, result builder, retention, exporter, prompt builder |
| `app/workers/` | Background task worker |
| `app/api/` | HTTP endpoints |
| `app/dashboard/` | Single-page web UI served at `/` |
| `clients/` | Slash-command / skill source of truth for Claude Code, Codex, Gemini + `install.py` |
| `skills/` | Behavioral instructions embedded in agent prompts — including `skills/generic/conclave_charter.md` (the binding charter) |
| `docs/` | Design documents and decision records |
| `examples/` | Protocol example JSON |
| `tests/` | pytest suite |
| `data/` | Runtime: SQLite DB, per-task sandboxes, uploads, exports (gitignored) |

---

## Documents worth reading first

0. [`INSTALL.md`](INSTALL.md) — first-run setup, smoke test, troubleshooting
1. [`docs/CODING_WORKFLOW.md`](docs/CODING_WORKFLOW.md) — the canonical four-step loop
2. [`docs/SWITCHBOARD_PROTOCOL.md`](docs/SWITCHBOARD_PROTOCOL.md) — wire format, mode definitions, message schemas
3. [`docs/CONCLAVE_CHARTER.md`](docs/CONCLAVE_CHARTER.md) + [`skills/generic/conclave_charter.md`](skills/generic/conclave_charter.md) — the binding agreement embedded in every prompt (v1.2)
4. [`docs/ROADMAP.md`](docs/ROADMAP.md) — shipped, next, and *intentionally not built* (read before proposing new features)
5. [`docs/SAFETY_MODEL.md`](docs/SAFETY_MODEL.md) — permission model and approval rules
6. [`docs/AGENT_ADAPTERS.md`](docs/AGENT_ADAPTERS.md) — interface every adapter must satisfy
7. [`docs/TASK_LIFECYCLE.md`](docs/TASK_LIFECYCLE.md) — the state machine, including the resolve-mode user-input pause
8. [`docs/decisions/INDEX.md`](docs/decisions/INDEX.md) — every ratified decision record with one-line summaries
9. [`clients/README.md`](clients/README.md) — how the slash-command parity is structured across the three CLIs

---

## Status

Beyond proof-of-concept. All three real adapters run end-to-end. Conclave, resolve, and consult modes are live, including the user-input pause/resume cycle, threading, multimodal attachments, the project sandbox, the convergence judge, retention (with opt-in Tier 2 trim after export), export tracking, the orphan-task reaper, confidence-weighted convergence, Decision Memory retrieval, the dashboard, and cross-CLI slash commands.

Current "Next" items (see [`docs/ROADMAP.md`](docs/ROADMAP.md)): re-draft of DR0013 (pre-fetched URL attachments v2), tool-loop architecture for the API-based seats, `dashboard.js` modularization, inbox tagging. New capability proposals are evaluated against the Charter v1.2 *Operability before capability* principle.

---

## Note on this repo

This is a single-user, local-only project. The runtime database (`data/switchboard.db`) — which contains the full text of every deliberation, including any source code copied into per-task sandboxes — is **not** committed. Neither are `data/sandboxes/`, `data/uploads/`, or a local `config.yaml`. See `.gitignore`.

---

Copyright © 2026 digitalgods.ai. All rights reserved.
