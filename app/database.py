"""SQLite connection and schema init for the AI Conclave Switchboard.

For MVP the schema lives inline (SCHEMA_SQL below) rather than in migration
files. Migration framework is deferred until the schema needs to evolve.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    status            TEXT NOT NULL,
    source            TEXT NOT NULL,
    source_agent      TEXT,
    mode              TEXT NOT NULL,
    task_type         TEXT NOT NULL,
    user_request      TEXT NOT NULL,
    primary_agent     TEXT,
    consultants       TEXT NOT NULL DEFAULT '[]',
    project_path      TEXT,
    context_json      TEXT NOT NULL DEFAULT '{}',
    permissions_json  TEXT NOT NULL,
    limits_json       TEXT NOT NULL,
    error_message     TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status     ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at DESC);

CREATE TABLE IF NOT EXISTS agent_runs (
    id              TEXT    PRIMARY KEY,
    task_id         TEXT    NOT NULL,
    agent_name      TEXT    NOT NULL,
    role            TEXT    NOT NULL,
    round_number    INTEGER NOT NULL,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    status          TEXT    NOT NULL,
    exit_code       INTEGER,
    duration_ms     INTEGER,
    error_code      TEXT,
    error_message   TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_task ON agent_runs(task_id, round_number);

CREATE TABLE IF NOT EXISTS agent_messages (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    agent_run_id    TEXT,
    agent_name      TEXT NOT NULL,
    role            TEXT NOT NULL,
    message_type    TEXT NOT NULL,
    direction       TEXT NOT NULL,
    content         TEXT,
    structured_json TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (task_id)      REFERENCES tasks(id)      ON DELETE CASCADE,
    FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_messages_task ON agent_messages(task_id, created_at);

CREATE TABLE IF NOT EXISTS final_results (
    id                                TEXT PRIMARY KEY,
    task_id                           TEXT NOT NULL UNIQUE,
    final_answer                      TEXT NOT NULL,
    agreement_level                   TEXT NOT NULL,
    resolution_status                 TEXT,
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

CREATE TABLE IF NOT EXISTS approvals (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    approval_type   TEXT NOT NULL,
    description     TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    resolution_note TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_task   ON approvals(task_id);

CREATE TABLE IF NOT EXISTS task_artifacts (
    id             TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    kind           TEXT NOT NULL,
    title          TEXT,
    filename       TEXT NOT NULL,
    mime_type      TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL,
    storage_path   TEXT NOT NULL,
    metadata_json  TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_task_artifacts_task ON task_artifacts(task_id, created_at);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id            TEXT PRIMARY KEY,
    task_id       TEXT,
    level         TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    message       TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_logs_task       ON logs(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_logs_event_type ON logs(event_type, created_at);
"""


_db_path: Optional[Path] = None


def init_database(path: str | Path) -> None:
    """Initialize the database. Idempotent — safe to call repeatedly."""
    global _db_path
    _db_path = Path(path)
    _db_path.parent.mkdir(parents=True, exist_ok=True)

    with connect() as conn:
        conn.executescript(SCHEMA_SQL)
        # Additive migrations — SQLite has no IF NOT EXISTS on ADD COLUMN.
        _add_column_if_missing(conn, "tasks", "user_decision", "TEXT")
        _add_column_if_missing(conn, "tasks", "user_decided_at", "TEXT")
        _add_column_if_missing(conn, "tasks", "parent_task_id", "TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id)")
        _add_column_if_missing(conn, "agent_runs", "input_tokens", "INTEGER")
        _add_column_if_missing(conn, "agent_runs", "output_tokens", "INTEGER")
        _add_column_if_missing(conn, "agent_runs", "cost_usd", "REAL")
        # Tier 2 archive tracking — the timestamp when this task's transcript+decision
        # was exported to disk. NULL means never exported. Used by the inbox filter
        # and (future) the retention policy's eventual Tier 2 trim-after-export option.
        _add_column_if_missing(conn, "tasks", "exported_at", "TEXT")
        _add_column_if_missing(conn, "tasks", "export_path", "TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_exported_at ON tasks(exported_at)")
        # Confidence aggregate {min,max,mean,count,missing_count} stored as JSON.
        # NULL for tasks that finalized before Phase 2 of the post-DR plan on
        # tsk_01KRSW6AS3M66B4RRJE3JFAPRV. New tasks: populated by the orchestrator.
        _add_column_if_missing(conn, "final_results", "confidence_aggregate_json", "TEXT")
        _add_column_if_missing(conn, "final_results", "action_plan_json", "TEXT")
        # Prior Art — TF-IDF-matched past decision records, computed at task
        # creation and frozen so the user sees exactly what the agents saw.
        # NULL for tasks created before Phase 2.5 of the post-DR plan. Shape:
        # [{number, title, date, summary, path, score}, ...].
        _add_column_if_missing(conn, "tasks", "prior_art_json", "TEXT")


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, type_decl: str) -> None:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_decl}")


_BUSY_TIMEOUT_MS = 30_000  # SQLite waits up to 30s on a write lock before returning busy.


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open a database connection. Autocommit mode (isolation_level=None).

    Concurrency notes:
      - WAL mode lets readers and a single writer coexist.
      - busy_timeout=30s makes SQLite block-wait on a busy lock rather than
        returning immediately. Covers ~all contention between the API
        handlers, the task worker, and the retention worker for normal load.
      - For paths that may still hit a locked DB despite busy_timeout (e.g.,
        VACUUM during a heavy write burst), use `with_retry()` below.
    """
    if _db_path is None:
        raise RuntimeError("init_database must be called before connect()")

    conn = sqlite3.connect(str(_db_path), isolation_level=None, timeout=_BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    try:
        yield conn
    finally:
        conn.close()


_DEFAULT_MAX_ATTEMPTS = 5
_DEFAULT_BASE_DELAY = 0.1


def with_retry(
    fn,
    *args,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    base_delay: float = _DEFAULT_BASE_DELAY,
    **kwargs,
):
    """Retry a callable on 'database is locked' / 'database is busy' with
    exponential backoff. Re-raises other OperationalErrors unchanged.

    Use this for write-heavy operations that may hit the busy_timeout ceiling
    during VACUUM or other long-lock events (e.g., retention's trim pass).
    Read-only operations rarely need this because they share the WAL.
    """
    import time
    last_err: Optional[sqlite3.OperationalError] = None
    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                # Different error — don't retry, propagate
                raise
            last_err = e
            if attempt == max_attempts - 1:
                raise
            time.sleep(base_delay * (2 ** attempt))
    # Should be unreachable, but in case the loop falls through:
    if last_err is not None:
        raise last_err


def now_iso() -> str:
    """Return current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()
