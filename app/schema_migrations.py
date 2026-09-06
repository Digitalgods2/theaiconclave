"""Numbered, transactional SQLite schema migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone


Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _add_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _legacy_additive(conn: sqlite3.Connection) -> None:
    for column, declaration in (
        ("user_decision", "TEXT"), ("user_decided_at", "TEXT"),
        ("parent_task_id", "TEXT"), ("exported_at", "TEXT"),
        ("export_path", "TEXT"), ("prior_art_json", "TEXT"),
    ):
        _add_column(conn, "tasks", column, declaration)
    for column, declaration in (
        ("input_tokens", "INTEGER"), ("output_tokens", "INTEGER"), ("cost_usd", "REAL"),
    ):
        _add_column(conn, "agent_runs", column, declaration)
    for column, declaration in (
        ("confidence_aggregate_json", "TEXT"), ("action_plan_json", "TEXT"),
        ("failure_cause_tags_json", "TEXT NOT NULL DEFAULT '[]'"),
    ):
        _add_column(conn, "final_results", column, declaration)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_exported_at ON tasks(exported_at)")


def _projects_and_evidence(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_projects (
            id                         TEXT PRIMARY KEY,
            name                       TEXT NOT NULL UNIQUE,
            description                TEXT NOT NULL DEFAULT '',
            instructions               TEXT NOT NULL DEFAULT '',
            default_evidence_urls_json TEXT NOT NULL DEFAULT '[]',
            created_at                 TEXT NOT NULL,
            updated_at                 TEXT NOT NULL,
            archived_at                TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evidence_snapshots (
            id             TEXT PRIMARY KEY,
            task_id        TEXT,
            project_id     TEXT,
            url            TEXT NOT NULL,
            title          TEXT,
            publisher      TEXT,
            retrieved_at   TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            content_text   TEXT NOT NULL,
            quality_json   TEXT NOT NULL DEFAULT '{}',
            metadata_json  TEXT NOT NULL DEFAULT '{}',
            created_at     TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (project_id) REFERENCES decision_projects(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_evidence_task ON evidence_snapshots(task_id, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_evidence_project ON evidence_snapshots(project_id, created_at)"
    )
    _add_column(
        conn, "tasks", "decision_project_id",
        "TEXT REFERENCES decision_projects(id) ON DELETE SET NULL",
    )
    _add_column(conn, "tasks", "judge_agent", "TEXT")
    _add_column(conn, "tasks", "synthesis_agent", "TEXT")
    _add_column(conn, "tasks", "evidence_snapshot_ids_json", "TEXT NOT NULL DEFAULT '[]'")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_decision_project ON tasks(decision_project_id)"
    )


def _citations(conn: sqlite3.Connection) -> None:
    _add_column(conn, "final_results", "citations_json", "TEXT NOT NULL DEFAULT '[]'")
    _add_column(conn, "final_results", "citation_coverage_json", "TEXT")
    _add_column(conn, "final_results", "synthesis_agent", "TEXT")


def _feedback(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS task_feedback (
        task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
        decision_changed INTEGER CHECK(decision_changed IN (0,1)),
        material_risk_found INTEGER CHECK(material_risk_found IN (0,1)),
        extra_review_worth_it INTEGER CHECK(extra_review_worth_it IN (0,1)),
        note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")


MIGRATIONS: tuple[Migration, ...] = (
    (1, "legacy_additive_columns", _legacy_additive),
    (2, "decision_projects_and_evidence", _projects_and_evidence),
    (3, "final_result_citations", _citations),
    (4, "task_feedback", _feedback),
)


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in order and return their version numbers."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY,
               name TEXT NOT NULL,
               applied_at TEXT NOT NULL
           )"""
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    completed: list[int] = []
    for version, name, migration in MIGRATIONS:
        if version in applied:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            migration(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, _now()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        completed.append(version)
    return completed


__all__ = ["MIGRATIONS", "apply_migrations"]
