# Decision Record 0005 — DB concurrency hardening + Tier 2 export/archive

**Date**: 2026-05-11
**Mode**: Glen-directed additions to the post-conclave prioritization (no separate conclave)
**Keeper**: claude-code

## What Was Chosen

Two distinct items, both shipped:

### A. DB concurrency hardening

- `PRAGMA busy_timeout = 30000` set on every SQLite connection so writers block-wait on a lock for up to 30 seconds rather than returning busy immediately.
- New `with_retry()` helper in `app/database.py` that retries a callable on `OperationalError` matching `"locked"` or `"busy"` with exponential backoff (default 5 attempts, 0.1s base delay). Other `OperationalError`s and all `ProgrammingError`s propagate unchanged.
- Applied `with_retry()` to:
  - `app/workers/task_worker.py::_claim_next_pending` — runs every 2 seconds and is the most contention-prone write.
  - `app/services/retention.py::_vacuum` — long-lock operation that can collide with concurrent writers despite busy_timeout.

### B. Tier 2 export/archive flow (enhanced)

- Two new columns on `tasks`: `exported_at TEXT`, `export_path TEXT`. Migration is additive via the existing `_add_column_if_missing` pattern.
- New index `idx_tasks_exported_at` for filter queries.
- `POST /api/tasks/{id}/export` now sets `exported_at` + `export_path` on the task after the markdown is written.
- New endpoint `POST /api/tasks/export-batch` accepting either `{"task_ids": [...]}` or `{"filter": "unexported_terminal"}` (default). Returns `{exported, skipped, errors}` arrays with per-task results.
- `GET /api/tasks?exported=true|false` filters server-side.
- `GET /api/tasks` and `GET /api/tasks/{id}` both return `exported_at` and `export_path` on every task object.
- Dashboard: export-status filter in the inbox (replacing the "coming soon" decision-filter stub), bulk-export button with `confirm()` guard, row-level green dot indicator for exported rows, Detail view shows "Re-export" label + "Last exported: ..." when applicable.

## Why It Was Chosen

### DB concurrency

Three writers contend for the SQLite file: the API handlers (every task creation, decision recording, answer submission), the task worker (every 2 seconds for claim, plus writes for every agent message and run), and the retention loop (every 6 hours for trim + VACUUM). Default SQLite returns `OperationalError: database is locked` immediately on contention. We observed no production failures yet, but the failure mode is real and prevents-by-default rather than diagnose-after.

`busy_timeout=30s` is the standard SQLite fix and handles ~all normal contention. `with_retry()` covers the residual case where a write blocks longer than 30s (typically only during VACUUM on a large DB). Together these turn "occasional opaque lock error" into "slight latency spike" — same semantics, different observability.

### Tier 2 export/archive

The retention policy (decision 0003) says Tier 2 is *"retain indefinitely until exported."* The export side existed (`/export` endpoint, dashboard button) but had no tracking — re-exports didn't surface, no batch flow, no inbox visibility into what was already archived versus what wasn't.

The contract isn't fulfilled if you can't tell what's been exported. Adding the columns + the filter + the bulk endpoint closes that loop. It also unblocks a future amendment to the retention policy ("optionally trim Tier 2 after export") because the system can now answer "is this task safe to trim?" in O(1).

## What Was Rejected

- **Auto-export-then-trim Tier 2 in the retention worker**. Considered but deferred: too aggressive without an explicit user opt-in. Listed in ROADMAP as a future Next item.
- **Connection pool / lock manager**. SQLite doesn't benefit from connection pooling the way Postgres does; `busy_timeout` + retry is the canonical Pythonic approach.
- **Migrating to a heavier DB (Postgres)**. Rejected per the conclave's "architecture simplicity" principle in decision 0003 / ROADMAP.
- **A dedicated "Archive" tab in the dashboard**. Rejected in favor of an export-status filter on the existing Inbox — same intent, less UI surface to maintain.

## Known Risks

- **Sustained write contention beyond 30s still fails.** Mitigated by `with_retry` on the heaviest paths; not mitigated for casual API writes. If a write fails outside the wrapped paths during a long VACUUM, the API request returns 500. Mitigation: keep VACUUM-fast (current DB is small enough that VACUUM takes <1s on the dev workstation; revisit at scale).
- **Bulk export of many tasks can take a while.** No per-task progress streaming — the user gets one big response when finished. Acceptable for the current scale (dozens of unexported tasks); revisit at hundreds.
- **`export_path` is platform-specific.** Stored as a raw absolute path — Windows backslashes on Windows, slashes elsewhere. Acceptable for a local-only service; would matter if Switchboard moves cross-machine.
- **The dashboard's green-dot row indicator** is subtle and may be missed. Mitigation: explicit "Exported" filter makes the state queryable; per-row tooltip shows the path.

## Open Questions

- **Should the retention worker eventually trim Tier 2 after export?** Currently it never does. The infrastructure now exists to do so safely (we know which tasks are exported and when). A new charter amendment or retention policy decision would be needed to enable it. Listed as a Next item.
- **Should re-exporting update the timestamp every time?** Currently yes (overwrites `exported_at` on each call). Alternative: only set on first export, or maintain a history. Current behavior is simplest and matches "latest export wins" semantics.
- **Should the bulk export be streaming?** Currently it returns one big JSON after all exports finish. For a 1000-task batch, this could take a minute. Streaming via SSE or chunked-transfer would be nicer UX. Defer until needed.

## Who Is Keeping Continuity

**`claude-code`** as keeper. Implementation lives at:

- `app/database.py` — `_BUSY_TIMEOUT_MS`, `with_retry()`, migration of `exported_at` / `export_path` columns + index
- `app/workers/task_worker.py` — `_claim_next_pending` wrapped in retry
- `app/services/retention.py` — `_vacuum` wrapped in retry
- `app/api/tasks.py` — `export_task` now sets `exported_at`; `export_batch` endpoint; `list_tasks` accepts `exported` query param
- `app/dashboard/index.html`, `dashboard.css`, `dashboard.js` — inbox export filter, bulk button, row indicator, Detail re-export label
- `tests/test_db_concurrency.py` — 9 tests
- `tests/test_export_tracking.py` — 8 tests
