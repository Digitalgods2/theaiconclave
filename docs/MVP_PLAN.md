# MVP Plan

The first useful version of AI Switchboard. The goal is to prove the core loop: an AI agent can request review from another AI agent, the Switchboard mediates the exchange, and the user reads the result in a dashboard.

## 1. The Demo Scenario

A user opens the Switchboard dashboard, submits a Python traceback, and selects:

- **Mode**: `consult`
- **Primary**: Codex
- **Consultant**: Claude Code
- **Permissions**: read-only (defaults)

Codex proposes a fix. Claude Code critiques it. Codex finalizes. The dashboard shows the proposal, the critique, the disagreement, and the final answer. No commands are executed; recommended commands appear with approval buttons.

If this works end-to-end, MVP is done.

## 2. Included

### Service
- FastAPI app, single process, listens on `127.0.0.1:8787`
- SQLite database at `data/switchboard.db`
- Background worker polling the `tasks` table every 2 seconds
- Health check at `/api/health`

### Protocol
- Full v1.0 protocol per `SWITCHBOARD_PROTOCOL.md`
- Validation of incoming task requests against the schema
- `consult` mode only

### Modes
- `resolve` — shipped (open-ended primary-driven loop)
- `consult` — shipped (bounded second opinion)
- `conclave` — shipped (N equal participants, full-mesh, convergence termination)
- `handoff` — deferred to v0.2
- `poll` — deferred to v0.2

The default mode for non-trivial tasks is `resolve`. Use `consult` for a quick second opinion. Use `conclave` when you want N agents (≥2) to deliberate as equals — no primary, full mesh visibility, terminates on convergence threshold.

### Adapters
- `codex_adapter` — wraps Codex CLI, real
- `claude_adapter` — wraps Claude Code, real
- `fake_adapter` — returns canned responses for tests

Other adapters (Gemini, OpenClaw) ship as stubs returning `agent_unavailable`.

### Orchestration
- Linear consult flow: primary proposal → consultant critique → primary final
- `max_rounds` default 3, configurable per task
- Repetition detection — if two consecutive primary responses share >80% n-gram overlap, stop with `loop_detected`
- Hard timeout per agent call: `limits.timeout_seconds`, default 180s

### Safety
- All eight permissions enforced per `SAFETY_MODEL.md`
- Hard blocklist active and non-overridable
- Soft list active — flagged for approval, never auto-run in MVP
- Context sanitizer strips known secret patterns
- Approval gate writes `approvals` rows; resolution via API in MVP

### Skills
Shipped:
- `skills/generic/switchboard_connector.md`
- `skills/generic/primary_agent_behavior.md`
- `skills/generic/consultant_behavior.md`
- `skills/generic/safety_behavior.md`
- `skills/generic/user_invocation_triggers.md`
- `skills/generic/role_disambiguation.md`
- `skills/codex/codex_switchboard_skill.md`
- `skills/claude-code/claude_switchboard_skill.md`

### Dashboard
- New task form (description, primary, consultant, permissions)
- Task inbox (list with status, agents, created time)
- Task detail (request, agent transcript, final answer, disagreements, approval buttons)
- No real-time updates in MVP — page reload polls

### API
Implemented per `API_REFERENCE.md`:
- `POST /api/tasks`
- `GET /api/tasks`
- `GET /api/tasks/{id}`
- `POST /api/tasks/{id}/cancel`
- `GET /api/agents`
- `POST /api/agents/{name}/test`
- `GET /api/approvals`
- `POST /api/approvals/{id}/approve`
- `POST /api/approvals/{id}/reject`
- `GET /api/health`

### Configuration
- `config.example.yaml` with all defaults
- `.env.example` for paths and credentials
- Per-agent command paths configurable

## 3. Excluded

Explicitly out of scope for MVP:

- `handoff` and `poll` modes
- Auto-applied patches (MVP only surfaces them)
- Webhooks, folder watchers, git watchers, log watchers, scheduled triggers
- VS Code extension, browser extension, desktop app
- Slack / email / GitHub integration
- Remote / multi-user authentication
- Cloud sync, team management
- Real Gemini / OpenClaw adapters (stubs only)
- Real-time dashboard updates (websockets / SSE)
- Plugin marketplace
- Voice interface
- Docker packaging

## 4. Milestones

Milestones must complete in order. Each milestone has at least one test in `tests/` proving its success criterion.

| # | Milestone | Success criterion |
|---|---|---|
| 1 | Protocol locked | Schemas in `SWITCHBOARD_PROTOCOL.md`, JSON examples in `examples/`, schema validator in `app/protocol/validators.py` passes all examples |
| 2 | Service shell | `uvicorn app.main:app` boots, `/api/health` returns 200, SQLite tables created, `POST /api/tasks` persists a task |
| 3 | Worker + fake adapter | A task with `primary_agent: fake` and `consultants: [fake]` runs end-to-end, stores all messages, transitions to `completed` |
| 4 | Real adapters | Codex and Claude Code adapters pass `POST /api/agents/{name}/test`, return parseable structured responses |
| 5 | Debate loop | A real consult task — Codex primary, Claude Code consultant — completes with three real agent calls and a populated `disagreements` list |
| 6 | Dashboard | User can submit a task and read its result in a browser without using the API directly |
| 7 | Skills shipped | The eight skill files in section 2 above exist and are referenced from the connector skill |
| 8 | Approval gate | A task requesting a soft-list command transitions to `waiting_for_user`, surfaces an approval row, and resumes on approve / cancels-action on reject |

## 5. Test Scenarios

The MVP is "done" when these scenarios all pass on a clean install:

1. **Happy path** — submit a debug task with two real agents, get a final result with `disagreements` populated.
2. **Permission denial** — submit a task with `can_run_commands: false`; primary recommends a command; final result lists it under `commands_requiring_approval` and the action is not executed.
3. **Hard-blocked command** — primary recommends `rm -rf /`; orchestrator emits `permission_denied`, command is not surfaced as approvable.
4. **Agent timeout** — consultant exceeds `timeout_seconds`; task completes with the consultant's slot in `errors` and the primary's proposal returned as the final answer with a noted gap.
5. **Loop detection** — fake adapter configured to return identical content on rounds 2 and 3; orchestrator stops with `loop_detected`.
6. **Approval flow** — primary recommends `pip install requests`; task transitions to `waiting_for_user`; after `POST /api/approvals/{id}/approve` the task resumes.
7. **Cancellation** — long-running task receives `POST /api/tasks/{id}/cancel`; task ends in status `cancelled`, no further agent calls.
8. **Schema validation** — malformed task request rejected with `invalid_request` and a field-level error.

## 6. Success Criteria

The MVP ships when:

- All 8 milestones pass their tests
- The 8 test scenarios pass on a clean install
- The dashboard demo runs end-to-end without manual API calls
- A new user can install, configure agents, and run their first task in under 15 minutes following `INSTALLATION.md`

## 7. Explicit Non-Goals

These are not failures of the MVP — they are intentionally deferred:

- The MVP does not handle agent disagreement intelligently; it just surfaces it.
- The MVP does not optimize context size beyond a hardcoded ceiling.
- The MVP does not retry failed agents.
- The MVP does not support concurrent tasks beyond the worker's serial loop.
- The MVP does not enforce per-user rate limits (single-user assumption).
- The MVP does not encrypt the SQLite database (local-only assumption).

These appear on the roadmap.
