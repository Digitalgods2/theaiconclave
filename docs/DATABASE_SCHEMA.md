# Database Schema

The MVP uses SQLite at the path defined by `database.path` in `config.yaml` (default `data/switchboard.db`). This document defines every table, its columns, constraints, and indexes. The `app/database.py` module owns schema creation and migrations.

The protocol (`SWITCHBOARD_PROTOCOL.md`) defines the *wire* format. This document defines the *storage* format. Conversions between them are the orchestrator's responsibility — never expose SQL directly to adapters or to the API layer.

## 1. Conventions

- **Primary keys are TEXT (ULIDs from `app/utils/ids.py`).** Format: `<prefix>_<26-char ULID>`. Prefixes: `tsk` (task), `run` (agent_run), `msg` (agent_message), `res` (final_result), `apr` (approval), `art` (task artifact), `log` (log).
- **All timestamps are ISO 8601 UTC strings.** SQLite has no native datetime; ISO 8601 sorts correctly as text and round-trips through Pydantic without ambiguity.
- **Structured fields use `_json` columns.** They are validated against the protocol schema before write. Reading them returns parsed dicts; writing serializes deterministically (sorted keys, no whitespace).
- **Foreign keys are enforced.** Every connection sets `PRAGMA foreign_keys = ON`.
- **WAL mode.** Every connection sets `PRAGMA journal_mode = WAL` for concurrent reads during writes.

## 2. Tables

### `tasks`

The top-level row for every task submitted to the AI Conclave Switchboard.

```sql
CREATE TABLE tasks (
    id                TEXT PRIMARY KEY,                 -- tsk_<ULID>
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    status            TEXT NOT NULL,                    -- pending | running | waiting_for_user | completed | failed | cancelled
    source            TEXT NOT NULL,                    -- dashboard | api | webhook | cli | watcher
    source_agent      TEXT,                             -- agent that submitted, if any
    mode              TEXT NOT NULL,                    -- consult | handoff | poll
    task_type         TEXT NOT NULL,
    user_request      TEXT NOT NULL,
    primary_agent     TEXT,                             -- null in poll mode
    consultants       TEXT NOT NULL DEFAULT '[]',       -- JSON array of agent names
    project_path      TEXT,
    context_json      TEXT NOT NULL DEFAULT '{}',
    permissions_json  TEXT NOT NULL,
    limits_json       TEXT NOT NULL,
    error_message     TEXT
);

CREATE INDEX idx_tasks_status     ON tasks(status);
CREATE INDEX idx_tasks_created_at ON tasks(created_at DESC);
```

### `agent_runs`

One row per agent invocation. A `consult` task with one consultant produces three runs (primary proposal, consultant critique, primary final). A `poll` task with N peers produces N runs.

```sql
CREATE TABLE agent_runs (
    id              TEXT    PRIMARY KEY,                -- run_<ULID>
    task_id         TEXT    NOT NULL,
    agent_name      TEXT    NOT NULL,
    role            TEXT    NOT NULL,                   -- primary | consultant | peer
    round_number    INTEGER NOT NULL,                   -- 1-indexed within the task
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    status          TEXT    NOT NULL,                   -- pending | running | completed | failed | timed_out
    exit_code       INTEGER,                            -- subprocess exit, null for non-subprocess adapters
    duration_ms     INTEGER,
    error_code      TEXT,                               -- protocol error code on failure
    error_message   TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX idx_agent_runs_task ON agent_runs(task_id, round_number);
```

### `agent_messages`

Every message produced by or sent to an agent. Stored in full. Never summarized.

```sql
CREATE TABLE agent_messages (
    id              TEXT PRIMARY KEY,                   -- msg_<ULID>
    task_id         TEXT NOT NULL,
    agent_run_id    TEXT,                               -- null for synthetic/orchestrator-injected messages
    agent_name      TEXT NOT NULL,
    role            TEXT NOT NULL,
    message_type    TEXT NOT NULL,                      -- primary_proposal | consultant_critique | primary_final | peer_answer | error
    direction       TEXT NOT NULL,                      -- to_agent | from_agent
    content         TEXT,                               -- raw text (prompt or response body)
    structured_json TEXT,                               -- protocol-validated JSON if applicable
    created_at      TEXT NOT NULL,
    FOREIGN KEY (task_id)      REFERENCES tasks(id)      ON DELETE CASCADE,
    FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id) ON DELETE SET NULL
);

CREATE INDEX idx_agent_messages_task ON agent_messages(task_id, created_at);
```

### `final_results`

The final, user-facing answer. One row per completed task.

```sql
CREATE TABLE final_results (
    id                                TEXT PRIMARY KEY,      -- res_<ULID>
    task_id                           TEXT NOT NULL UNIQUE,  -- one final result per task
    final_answer                      TEXT NOT NULL,
    agreement_level                   TEXT NOT NULL,         -- consensus | minor_disagreement | major_disagreement | unresolved
    disagreements_json                TEXT NOT NULL DEFAULT '[]',
    recommended_actions_json          TEXT NOT NULL DEFAULT '[]',
    action_plan_json                  TEXT NOT NULL DEFAULT '[]',
    risks_json                        TEXT NOT NULL DEFAULT '[]',
    commands_requiring_approval_json  TEXT NOT NULL DEFAULT '[]',
    patches_requiring_approval_json   TEXT NOT NULL DEFAULT '[]',
    errors_json                       TEXT NOT NULL DEFAULT '[]',
    created_at                        TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
```

### `approvals`

User-pending decisions. A task in `waiting_for_user` has at least one row here in `pending` status.

```sql
CREATE TABLE approvals (
    id              TEXT PRIMARY KEY,                   -- apr_<ULID>
    task_id         TEXT NOT NULL,
    approval_type   TEXT NOT NULL,                      -- command | patch | package_install | file_write | network | other
    description     TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    status          TEXT NOT NULL,                      -- pending | approved | rejected
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    resolution_note TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX idx_approvals_status ON approvals(status, created_at);
CREATE INDEX idx_approvals_task   ON approvals(task_id);
```

### `task_artifacts`

App-owned draft outputs captured from final recommendations. These files live under `<user_data_root>/artifacts/<task_id>/<artifact_id>/` and are reviewable/downloadable. Applying a supported artifact to `project_path` is a separate user action.

```sql
CREATE TABLE task_artifacts (
    id             TEXT PRIMARY KEY,          -- art_<ULID>
    task_id        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    kind           TEXT NOT NULL,             -- file | edit | patch
    title          TEXT,
    filename       TEXT NOT NULL,
    mime_type      TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL,
    storage_path   TEXT NOT NULL,
    metadata_json  TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX idx_task_artifacts_task ON task_artifacts(task_id, created_at);
```

### `settings`

Mutable runtime configuration. Anything in here overrides `config.yaml`.

```sql
CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,                           -- JSON-encoded
    updated_at TEXT NOT NULL
);
```

### `logs`

Audit trail. Every safety-relevant event from `SAFETY_MODEL.md §8` lands here. Never auto-deleted in MVP.

```sql
CREATE TABLE logs (
    id            TEXT PRIMARY KEY,                     -- log_<ULID>
    task_id       TEXT,                                 -- nullable; some events are service-level
    level         TEXT NOT NULL,                        -- debug | info | warn | error
    event_type    TEXT NOT NULL,                        -- file_read | command_blocked | approval_granted | ...
    message       TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
);

CREATE INDEX idx_logs_task       ON logs(task_id, created_at);
CREATE INDEX idx_logs_event_type ON logs(event_type, created_at);
```

## 3. Protocol → Storage Mapping

| Protocol artifact | Storage |
|---|---|
| `TaskRequest` | One `tasks` row; `permissions`, `limits`, `context` go to `_json` columns. |
| `PrimaryResponse` (proposal or final) | One `agent_messages` row with `direction=from_agent`; the prompt that produced it is a separate row with `direction=to_agent`. |
| `ConsultantCritique` | One `agent_messages` row, `direction=from_agent`. |
| `PeerAnswer` | One `agent_messages` row per peer, `direction=from_agent`. |
| `FinalResult` | One `final_results` row. |
| Draft artifacts | Zero or more `task_artifacts` rows, plus files under `artifacts/`. |
| `Approval` | One `approvals` row. |
| `ProtocolError` | Run-scoped errors → `agent_runs.error_code` + `error_message`. Task-scoped errors → `final_results.errors_json`. |

## 4. Migrations

- The current schema version lives in `settings` under key `schema_version` (integer, JSON-encoded).
- Migrations live in `app/database/migrations/<NNNN>_<slug>.sql`, four-digit zero-padded.
- On startup, `app/database.py` reads `schema_version`, then applies every migration with a higher number in order, in a single transaction per file.
- **Forward-only in MVP.** No down-migrations. Recovery is by restore-from-backup, not by reverse-applying SQL.
- The initial schema (everything in section 2) is migration `0001_initial.sql`.

## 5. Performance Notes

- All indexed lookups are exact matches on indexed columns. No table scans expected for the MVP workload.
- Worker poll query — `SELECT id FROM tasks WHERE status = 'pending' ORDER BY created_at LIMIT 1` — is O(log n) thanks to `idx_tasks_status` and `idx_tasks_created_at`.
- `agent_messages` is append-only and will be the largest table by row count. Plan for rotation or archival in a future migration once a single task can routinely exceed thousands of messages (post-MVP).
- WAL mode allows the dashboard to read while the worker writes without blocking.

## 6. Backup Strategy

- Out of scope for MVP. Local-only assumption: the user's normal disk backup covers `data/switchboard.db` and `data/switchboard.db-wal`.
- Future: scheduled `VACUUM INTO data/backups/switchboard-<timestamp>.db` driven by a built-in cron.

## 7. Data Hygiene

- The orchestrator never deletes rows from `tasks`, `agent_runs`, `agent_messages`, `final_results`, or `logs`. Cancellation marks status, it does not erase history.
- `approvals` rows are never deleted; resolution updates `status` and `resolved_at`.
- `settings` is the only table where row deletion is normal (clearing an override).
