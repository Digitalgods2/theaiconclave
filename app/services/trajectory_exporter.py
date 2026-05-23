"""Trajectory exporter — write one terminal task's whole story to JSONL.

A trajectory is a self-contained, portable snapshot of one finished task:
the question, every prompt + reply, agent runtimes / cost / tokens, the final
result (with failure-cause tags), the user's decision, and lightweight
confidence aggregates. Output is a single JSON object on a single line at
`<exports_root>/trajectories/<task_id>.jsonl`. One file per task makes ad-hoc
analysis (load into a dataframe, share one trajectory with a collaborator)
trivial without writing a custom SQL query.

Design notes:
- Read-only on the DB. Idempotent: re-exporting overwrites cleanly.
- Defensive on older rows: any missing column / NULL falls back to a sane
  default, so this never raises mid-export on a pre-migration task.
- Schema-stable: see `_TRAJECTORY_SCHEMA_KEYS` for the contract. Adding a key
  is fine; removing or repurposing one needs a charter amendment because
  external readers depend on the shape.
- Called from the orchestrator's terminal-state hook AND from explicit API
  endpoints (POST /api/tasks/{id}/trajectory/export). Both share this one
  module so the on-disk format never diverges between auto-export and
  on-demand export.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.database import connect
from app.protocol.validators import PROTOCOL_VERSION
from app.utils.paths import trajectories_root


# Frozen shape — bump this when the schema changes so downstream consumers can
# detect the format version they're reading.
_TRAJECTORY_SCHEMA_VERSION = 1


def _column_or_none(row: Any, key: str) -> Any:
    """sqlite3.Row has no .get(); look up defensively. Older DB rows may be
    missing columns added in later migrations."""
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _parse_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if not isinstance(value, str):
        return value
    if not value.strip():
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def _row_to_dict(row: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: _column_or_none(row, k) for k in keys}


_TASK_KEYS = (
    "id", "created_at", "updated_at", "status", "source", "source_agent",
    "mode", "task_type", "user_request", "primary_agent", "consultants",
    "project_path", "context_json", "permissions_json", "limits_json",
    "error_message", "user_decision", "user_decided_at", "parent_task_id",
    "exported_at", "export_path",
)

_MESSAGE_KEYS = (
    "id", "task_id", "agent_run_id", "agent_name", "role", "message_type",
    "direction", "content", "structured_json", "created_at",
)

_RUN_KEYS = (
    "id", "task_id", "agent_name", "role", "round_number", "started_at",
    "finished_at", "status", "exit_code", "duration_ms", "error_code",
    "error_message", "input_tokens", "output_tokens", "cost_usd",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _completed_at(task_row: Any) -> Optional[str]:
    """Best guess at terminal timestamp: `updated_at` is the last write the
    orchestrator made, which for a terminal task is the status flip."""
    return _column_or_none(task_row, "updated_at")


def _normalize_consultants(raw: Any) -> list[str]:
    """`consultants` is stored as a JSON array string; tolerate the older
    plain-list shape too."""
    if isinstance(raw, list):
        return [str(x) for x in raw]
    parsed = _parse_json(raw, default=[])
    if isinstance(parsed, list):
        return [str(x) for x in parsed]
    return []


def _build_record(task_id: str) -> dict[str, Any]:
    """Assemble the full trajectory payload for one task. Reads from the live
    DB; raises ValueError if the task does not exist."""
    with connect() as conn:
        task_row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task_row is None:
            raise ValueError(f"task {task_id} not found")
        messages = conn.execute(
            """SELECT id, task_id, agent_run_id, agent_name, role, message_type,
                      direction, content, structured_json, created_at
               FROM agent_messages WHERE task_id = ? ORDER BY created_at""",
            (task_id,),
        ).fetchall()
        runs = conn.execute(
            """SELECT id, task_id, agent_name, role, round_number, started_at,
                      finished_at, status, exit_code, duration_ms, error_code,
                      error_message, input_tokens, output_tokens, cost_usd
               FROM agent_runs WHERE task_id = ? ORDER BY started_at""",
            (task_id,),
        ).fetchall()
        final_row = conn.execute(
            "SELECT * FROM final_results WHERE task_id = ?", (task_id,)
        ).fetchone()

    consultants = _normalize_consultants(_column_or_none(task_row, "consultants"))

    rounds_payload: list[dict[str, Any]] = []
    for m in messages:
        rounds_payload.append({
            "agent_name":   _column_or_none(m, "agent_name"),
            "role":         _column_or_none(m, "role"),
            "message_type": _column_or_none(m, "message_type"),
            "direction":    _column_or_none(m, "direction"),
            "content":      _column_or_none(m, "content"),
            "structured_json": _parse_json(_column_or_none(m, "structured_json"), default=None),
            "created_at":   _column_or_none(m, "created_at"),
            "agent_run_id": _column_or_none(m, "agent_run_id"),
        })

    runs_payload: list[dict[str, Any]] = [
        _row_to_dict(r, _RUN_KEYS) for r in runs
    ]

    failure_cause_tags: list[str] = []
    confidence_aggregate: Optional[dict[str, Any]] = None
    final_payload: Optional[dict[str, Any]] = None
    if final_row is not None:
        failure_cause_tags = _parse_json(
            _column_or_none(final_row, "failure_cause_tags_json"), default=[]
        ) or []
        if not isinstance(failure_cause_tags, list):
            failure_cause_tags = []
        confidence_aggregate = _parse_json(
            _column_or_none(final_row, "confidence_aggregate_json"), default=None
        )
        final_payload = {
            "final_answer":      _column_or_none(final_row, "final_answer"),
            "agreement_level":   _column_or_none(final_row, "agreement_level"),
            "resolution_status": _column_or_none(final_row, "resolution_status"),
            "disagreements":     _parse_json(_column_or_none(final_row, "disagreements_json"), default=[]),
            "recommended_actions": _parse_json(
                _column_or_none(final_row, "recommended_actions_json"), default=[]
            ),
            "action_plan":       _parse_json(_column_or_none(final_row, "action_plan_json"), default=[]),
            "risks":             _parse_json(_column_or_none(final_row, "risks_json"), default=[]),
            "commands_requiring_approval": _parse_json(
                _column_or_none(final_row, "commands_requiring_approval_json"), default=[]
            ),
            "patches_requiring_approval": _parse_json(
                _column_or_none(final_row, "patches_requiring_approval_json"), default=[]
            ),
            "errors":            _parse_json(_column_or_none(final_row, "errors_json"), default=[]),
            "confidence_aggregate": confidence_aggregate,
            "failure_cause_tags":   failure_cause_tags,
            "created_at":        _column_or_none(final_row, "created_at"),
        }

    return {
        "task_id":          task_id,
        "created_at":       _column_or_none(task_row, "created_at"),
        "completed_at":     _completed_at(task_row),
        "mode":             _column_or_none(task_row, "mode"),
        "task_type":        _column_or_none(task_row, "task_type"),
        "status":           _column_or_none(task_row, "status"),
        "source":           _column_or_none(task_row, "source"),
        "source_agent":     _column_or_none(task_row, "source_agent"),
        "parent_task_id":   _column_or_none(task_row, "parent_task_id"),
        "question":         _column_or_none(task_row, "user_request"),
        "agents": {
            "primary":     _column_or_none(task_row, "primary_agent"),
            "consultants": consultants,
        },
        "rounds":              rounds_payload,
        "runs":                runs_payload,
        "final_result":        final_payload,
        "decision": {
            "text":        _column_or_none(task_row, "user_decision"),
            "decided_at":  _column_or_none(task_row, "user_decided_at"),
        },
        "confidence_aggregate": confidence_aggregate,
        # Duplicated at top level for quick scanning / filtering without having
        # to drill into `final_result`. Cheap, and consumers asked for it.
        "failure_cause_tags":   failure_cause_tags,
        "protocol_version":     PROTOCOL_VERSION,
        "schema_version":       _TRAJECTORY_SCHEMA_VERSION,
        "exported_at":          _now_iso(),
    }


def export_trajectory(task_id: str) -> Path:
    """Write the task's trajectory to `<trajectories_root>/<task_id>.jsonl`.

    Returns the absolute path written. Overwrites any prior export for the
    same task (the on-disk file is the live view of the DB at export time).
    Raises ValueError if the task does not exist.

    Used by:
      - the orchestrator's terminal-state hook (auto-export every finished
        task — see `run_task`).
      - the explicit API endpoints under `/api/tasks/{id}/trajectory/...` and
        `/api/trajectories/export-all`.
    """
    record = _build_record(task_id)
    path = (trajectories_root() / f"{task_id}.jsonl").resolve()
    # Single-line JSONL: one object, terminated with a newline so concatenation
    # is safe (`cat *.jsonl > all.jsonl` stays well-formed).
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
    path.write_text(payload + "\n", encoding="utf-8")
    return path


def export_all_terminal(
    statuses: tuple[str, ...] = ("completed", "failed", "cancelled"),
) -> dict[str, Any]:
    """Iterate every terminal task in the DB and export its trajectory.

    Returns a summary `{exported: [...], errors: [...]}`. Per-task failures
    are caught so one bad row doesn't abort the whole batch — useful when
    sweeping pre-migration tasks for the first time.
    """
    with connect() as conn:
        rows = conn.execute(
            f"SELECT id FROM tasks WHERE status IN ({','.join('?' * len(statuses))}) "
            "ORDER BY created_at",
            tuple(statuses),
        ).fetchall()
    exported: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for r in rows:
        tid = r["id"]
        try:
            path = export_trajectory(tid)
            exported.append({"task_id": tid, "path": str(path)})
        except Exception as e:  # noqa: BLE001 — keep the batch going
            errors.append({"task_id": tid, "error": str(e)})
    return {
        "exported_count": len(exported),
        "error_count":    len(errors),
        "exported":       exported,
        "errors":         errors,
    }
