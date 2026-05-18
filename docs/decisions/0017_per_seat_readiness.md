# Decision Record 0017 — Per-seat readiness + configurable CLI command paths

**Date**: 2026-05-18
**Mode**: Glen-directed (ratified at implementation greenlight)
**Keeper**: claude-code
**Related**: [DR0006](0006_charter_v1_2_operability_before_capability.md), [DR0016](0016_user_data_root_and_lazy_config.md).
**Source**: implementation session 2026-05-18; companion to DR0016 ("Batch B" of the packaging-hygiene work surfaced by the architecture consult thread ending at `tsk_01KRVN7VT7NEQ8VNR6NV8CFYFZ`).

## What Was Chosen

Three small, coordinated changes that turn "the user's CLI didn't run" from a runtime mystery into a startup-visible, actionable signal:

- **`Readiness` model** in `app/agents/base.py`: a structured `(available: bool, reason: str, hint: str)` record. `reason` is a short machine-stable token (`ok`, `command_not_found`, `configured_path_missing`, `api_key_missing`, `readiness_check_failed`); `hint` is user-facing remediation text.
- **`BaseAdapter.readiness()`** method, defaulting to a generic wrap of `is_available()` so adapters that don't override still produce a valid `Readiness`. CLI adapters and the OpenRouter adapter override with specific reason/hint values.
- **Configurable `command_path` per CLI adapter**:
  - New `AgentConfig.command_path: Optional[str] = None` field. When set, the adapter uses it in preference to `shutil.which(command)`.
  - `CodexAdapter`, `ClaudeCodeAdapter`, `GeminiAdapter` each accept `command_path` via constructor and check it first in `_resolve_command()`. **Strict precedence**: if `command_path` is set but the file doesn't exist, the adapter reports `configured_path_missing` rather than silently falling back to PATH — a misconfiguration shouldn't mask itself.
  - `agent_registry.init_registry(config=None)` plumbs the per-adapter `command_path` from `config.agents.<name>` at registration. `config=None` preserves the no-arg form for tests.
- **`/api/health` extended** with a `seats: [...]` array per registered, user-facing adapter (internal/test adapters filtered out). Each seat entry is `{name, kind, available, reason, hint}`. `kind` is `cli` for subprocess-backed adapters, `api` for HTTP-backed (OpenRouter), `test` for internal — though `test` is filtered out before serving.
- **30-second seats-cache** in `app/api/health.py` so rapid dashboard polling doesn't spawn `shutil.which` and OpenRouter HTTP probes on every call. Cache lock prevents concurrent readiness storms during dashboard reload. A `_invalidate_seats_cache()` hook exists for tests.
- **Config documentation**: `config.example.yaml` and the live `config.yaml` document the new `command_path` field next to each CLI agent's `command`.

## Why It Was Chosen

The packaging-architecture consult thread surfaced two related operability gaps for CLI adapters: (1) `shutil.which`-based discovery breaks for GUI-launched apps because Windows double-click and macOS Finder don't inherit shell PATH; (2) the only feedback when a CLI is missing is a generic `agent_unavailable` `AdapterError` raised mid-task. Both consultants flagged this as a load-bearing blocker for non-technical distribution: an installed user's "everything works in the terminal" experience would silently break in the packaged binary, and the error message wouldn't tell them how to fix it.

Adding `command_path` is a one-field config schema change that lets the user paste an absolute path through Settings (or edit `config.yaml`) and bypass PATH entirely. Adding `readiness()` is the dual: it lets the dashboard show "Codex CLI: not on PATH — install via npm or set agents.codex.command_path to the binary's absolute path" *before* a task is submitted. Together they close the unprovable-misconfiguration loop without requiring any change to task semantics.

The 30-second cache is operability-minded: a chatty dashboard poll shouldn't churn subprocess and HTTP probes. The cache lock prevents a thundering herd if multiple panel tabs reload at once.

## What Was Rejected

- **Falling back to PATH when `command_path` is set but missing.** Considered, rejected — that's the exact "silent misconfiguration" failure mode this record exists to prevent. Strict precedence with a clear `configured_path_missing` reason is more surfacing-friendly.
- **A separate `/api/seats` endpoint.** Considered, rejected — adding the array to `/api/health` keeps the dashboard's single health-poll cycle authoritative for both service status and seat readiness. Splitting would require a second poll loop without a real benefit.
- **Caching forever, with manual invalidation.** Considered, rejected — a 30-second TTL is short enough that fixing a missing CLI is detected within the next dashboard refresh without explicit user action, and long enough that ordinary polling cost is negligible. The `_invalidate_seats_cache()` hook covers the test path.
- **Probing CLI version (running `--version`) inside `readiness()`.** Rejected for v1 — `readiness()` runs on every dashboard poll within the cache window, and subprocess spawn on every call defeats the cache's purpose. The existing `test_connection()` method already runs `--version` for explicit "test this seat" requests; `readiness()` only needs to know whether the binary is present.
- **Blocking task submission to unavailable seats.** Considered, deferred — the dashboard should surface the `available: false` signal and show the hint, but the user may have local knowledge the adapter doesn't (e.g., they've just installed the CLI; the cache hasn't refreshed). Letting them override is more respectful than refusing. The follow-on dashboard work can add a "are you sure?" confirmation if a missing seat is checked at submission time.

## Operability Impact

(Ninth decision under Charter v1.2 §Decision Records.)

- **Observability**: **positive**. A seat that was previously failing silently at task time now surfaces in `/api/health` at startup and on every dashboard refresh with a specific reason and remediation hint.
- **Durability**: **neutral**. No schema change, no new state.
- **Recoverability**: **positive**. A user who sees a `command_not_found` hint can fix the configuration without restarting the service or filing a support request. The cache invalidation hook keeps the test path clean.
- **Audit trail**: **neutral**. Readiness state isn't persisted (it's transient per-poll) — the audit trail for adapter failures is still the per-task `AdapterError` recorded on the task row.
- **Retention/export**: **neutral**.
- **Complexity**: **low**. ~150 LOC of net new code (one new model + one new method on base + four adapter overrides + endpoint extension + cache) plus ~270 LOC of test coverage. The new model is a 3-field Pydantic class; the cache is two functions guarded by an asyncio.Lock.
- **Cost**: **slightly positive**. Replaces unbounded `shutil.which` and HTTP probe rate with a 30-second cap.
- **Accepted risks**:
  - **`command_path` mistakes are loud, not silent.** A user setting `command_path` to a wrong path will see `configured_path_missing` and the seat will not run, rather than silently falling back to PATH. Documented as intentional precedence.
  - **30-second cache means a freshly installed CLI takes up to 30s to appear available.** Acceptable; the user can also restart the service for immediate refresh.
  - **OpenRouter readiness uses a fast in-process check** (`_api_key() is not None`) — does not verify the key actually works. `test_connection()` covers that for explicit probes.
- **Exceptions to "Operability before capability"**: **none**. This is purely an operability improvement.

## Known Risks

- **GUI PATH inheritance is the underlying problem; `command_path` is the lever to work around it.** A future packager that injects a shell-resolved PATH into the GUI launcher would reduce reliance on `command_path` — that's a packaging concern, not an adapter concern.
- **The seats cache is per-process.** Two service instances (which DR0016 already prevents via pidlock + active-pid migration check) would each maintain their own cache, but this is moot under the single-instance invariant.

## Open Questions

- **Dashboard surface** for the new seats array — what does the "Council readiness" widget look like? Deferred to a separate, smaller, UI-only change; the API is the binding commitment in this record.
- **Should `is_available()` itself be deprecated?** It's now a strictly less informative version of `readiness()`. Leaving it in place for backward compatibility — every adapter still uses it internally — but it could be removed in a future cleanup if it stops carrying weight.

## Who Is Keeping Continuity

**`claude-code`** as keeper. Implementation landed in one commit alongside this record:

- `app/agents/base.py` — `Readiness` model, default `readiness()` method
- `app/agents/codex_adapter.py`, `claude_adapter.py`, `gemini_adapter.py` — `command_path` constructor arg, `_resolve_command()` strict precedence, `readiness()` override
- `app/agents/openrouter_adapter.py` — `readiness()` override for API-key state
- `app/config.py` — `AgentConfig.command_path: Optional[str] = None`
- `app/services/agent_registry.py` — `init_registry(config=None)` plumbing
- `app/main.py` — passes `config` to `init_registry`
- `app/api/health.py` — `seats: [...]` extension + 30-second cache
- `config.example.yaml`, `config.yaml` — `command_path: null` documented next to each CLI agent
- `tests/test_adapter_readiness.py`, `tests/test_health_endpoint.py` — 21 new tests covering all reason codes, precedence, cache behavior, error surfaces

All 316 tests pass.
