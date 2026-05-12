"""Task API endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException

from app.database import connect, now_iso
from app.protocol.validators import MessageType, TaskRequest
from app.services.exporter import export_to_markdown
from app.utils.ids import message_id, task_id as new_task_id

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# Where decision-record exports land on disk. Kept deterministic so the user can
# re-export idempotently (same task_id -> same file). The directory is created
# on demand to avoid surprising the user with an empty folder on a fresh install.
EXPORTS_DIR = Path("data") / "exports"


_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


@router.post("")
async def create_task(request: TaskRequest) -> dict[str, Any]:
    tid = new_task_id()
    now = now_iso()
    # Validate parent exists if specified.
    if request.parent_task_id:
        with connect() as conn:
            parent_row = conn.execute(
                "SELECT id FROM tasks WHERE id = ?", (request.parent_task_id,)
            ).fetchone()
        if parent_row is None:
            raise HTTPException(
                status_code=400,
                detail=f"parent_task_id {request.parent_task_id} does not exist",
            )
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks
            (id, created_at, updated_at, status, source, source_agent, mode,
             task_type, user_request, primary_agent, consultants, project_path,
             context_json, permissions_json, limits_json, parent_task_id)
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tid,
                now,
                now,
                request.source.value,
                request.source_agent,
                request.mode.value,
                request.task_type.value,
                request.user_request,
                request.primary_agent,
                json.dumps(request.consultants),
                request.project_path,
                json.dumps(request.context.model_dump(), sort_keys=True),
                json.dumps(request.permissions.model_dump(), sort_keys=True),
                json.dumps(request.limits.model_dump(), sort_keys=True),
                request.parent_task_id,
            ),
        )
    return {
        "task_id": tid,
        "status": "pending",
        "created_at": now,
        "parent_task_id": request.parent_task_id,
    }


@router.get("")
async def list_tasks(
    status: Optional[str] = None,
    limit: int = 50,
    exported: Optional[str] = None,
) -> dict[str, Any]:
    """List tasks, newest first.

    Filters:
      status=<value>: server-side filter on status column (existing).
      exported=true: only tasks where exported_at IS NOT NULL.
      exported=false: only tasks where exported_at IS NULL.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if exported is not None:
        norm = str(exported).lower()
        if norm in ("true", "1", "yes"):
            clauses.append("exported_at IS NOT NULL")
        elif norm in ("false", "0", "no"):
            clauses.append("exported_at IS NULL")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        f"""SELECT id, status, mode, task_type, primary_agent, consultants,
                   created_at, updated_at, exported_at, export_path,
                   source, source_agent
            FROM tasks {where}
            ORDER BY created_at DESC LIMIT ?"""
    )
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    return {
        "tasks": [
            {
                "id": r["id"],
                "status": r["status"],
                "mode": r["mode"],
                "task_type": r["task_type"],
                "primary_agent": r["primary_agent"],
                "consultants": json.loads(r["consultants"]),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "exported_at": _column_or_none(r, "exported_at"),
                "export_path": _column_or_none(r, "export_path"),
                "source": _column_or_none(r, "source"),
                "source_agent": _column_or_none(r, "source_agent"),
            }
            for r in rows
        ]
    }


def _row_to_final_result(row) -> dict[str, Any]:
    return {
        "task_id": row["task_id"],
        "final_answer": row["final_answer"],
        "agreement_level": row["agreement_level"],
        "resolution_status": row["resolution_status"],
        "disagreements": json.loads(row["disagreements_json"]),
        "recommended_actions": json.loads(row["recommended_actions_json"]),
        "risks": json.loads(row["risks_json"]),
        "commands_requiring_approval": json.loads(row["commands_requiring_approval_json"]),
        "patches_requiring_approval": json.loads(row["patches_requiring_approval_json"]),
        "errors": json.loads(row["errors_json"]),
        "created_at": row["created_at"],
    }


@router.get("/{task_id}")
async def get_task(task_id: str) -> dict[str, Any]:
    with connect() as conn:
        task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task_row:
            raise HTTPException(status_code=404, detail="task not found")

        messages = conn.execute(
            "SELECT * FROM agent_messages WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        ).fetchall()

        result_row = conn.execute(
            "SELECT * FROM final_results WHERE task_id = ?", (task_id,)
        ).fetchone()

        approvals = conn.execute(
            "SELECT * FROM approvals WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        ).fetchall()

        runs = conn.execute(
            """SELECT id, agent_name, role, round_number, started_at, finished_at,
                      status, exit_code, duration_ms, error_code, error_message,
                      input_tokens, output_tokens, cost_usd
               FROM agent_runs WHERE task_id = ? ORDER BY started_at""",
            (task_id,),
        ).fetchall()

    return {
        "task": {
            "id": task_row["id"],
            "status": task_row["status"],
            "mode": task_row["mode"],
            "task_type": task_row["task_type"],
            "user_request": task_row["user_request"],
            "primary_agent": task_row["primary_agent"],
            "consultants": json.loads(task_row["consultants"]),
            "project_path": task_row["project_path"],
            "permissions": json.loads(task_row["permissions_json"]),
            "limits": json.loads(task_row["limits_json"]),
            "created_at": task_row["created_at"],
            "updated_at": task_row["updated_at"],
            "error_message": task_row["error_message"],
            "user_decision": _column_or_none(task_row, "user_decision"),
            "user_decided_at": _column_or_none(task_row, "user_decided_at"),
            "parent_task_id": _column_or_none(task_row, "parent_task_id"),
            "exported_at": _column_or_none(task_row, "exported_at"),
            "export_path": _column_or_none(task_row, "export_path"),
            "source": _column_or_none(task_row, "source"),
            "source_agent": _column_or_none(task_row, "source_agent"),
        },
        "messages": [
            {
                "id": m["id"],
                "agent_name": m["agent_name"],
                "role": m["role"],
                "message_type": m["message_type"],
                "direction": m["direction"],
                "structured": json.loads(m["structured_json"]) if m["structured_json"] else None,
                "created_at": m["created_at"],
            }
            for m in messages
        ],
        "final_result": _row_to_final_result(result_row) if result_row else None,
        "approvals": [
            {
                "id": a["id"],
                "approval_type": a["approval_type"],
                "description": a["description"],
                "payload": json.loads(a["payload_json"]),
                "status": a["status"],
                "created_at": a["created_at"],
                "resolved_at": a["resolved_at"],
            }
            for a in approvals
        ],
        "agent_runs": [
            {
                "id": r["id"],
                "agent_name": r["agent_name"],
                "role": r["role"],
                "round_number": r["round_number"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "status": r["status"],
                "duration_ms": r["duration_ms"],
                "error_code": r["error_code"],
                "error_message": r["error_message"],
                "input_tokens": _column_or_none(r, "input_tokens"),
                "output_tokens": _column_or_none(r, "output_tokens"),
                "cost_usd": _column_or_none(r, "cost_usd"),
            }
            for r in runs
        ],
    }


@router.get("/{task_id}/thread")
async def get_thread(task_id: str, max_depth: int = 10) -> dict[str, Any]:
    """
    Return the ancestry chain for a task, oldest first.
    Each entry: id, mode, user_request, final_answer (or null), user_decision (or null), created_at.
    Cycle-safe (each ID visited at most once) and depth-capped (max_depth).
    """
    chain: list[dict[str, Any]] = []
    visited: set[str] = set()
    current: Optional[str] = task_id
    with connect() as conn:
        # First confirm the target exists.
        target = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="task not found")
        while current and len(chain) < max_depth:
            if current in visited:
                break
            visited.add(current)
            row = conn.execute(
                """SELECT t.id, t.mode, t.user_request, t.created_at,
                          t.user_decision, t.user_decided_at, t.parent_task_id,
                          fr.final_answer, fr.agreement_level
                   FROM tasks t LEFT JOIN final_results fr ON t.id = fr.task_id
                   WHERE t.id = ?""",
                (current,),
            ).fetchone()
            if row is None:
                break
            chain.append({
                "id": row["id"],
                "mode": row["mode"],
                "user_request": row["user_request"],
                "created_at": row["created_at"],
                "user_decision": row["user_decision"],
                "user_decided_at": row["user_decided_at"],
                "final_answer": row["final_answer"],
                "agreement_level": row["agreement_level"],
            })
            current = row["parent_task_id"]
    chain.reverse()  # oldest first
    return {"task_id": task_id, "thread": chain, "depth": len(chain)}


@router.post("/{task_id}/answer")
async def answer_task(task_id: str, body: dict = Body(...)) -> dict[str, Any]:
    """
    Provide the user's answer to a primary's pending question (resolve mode).
    Body: {"answer": "..."}
    Sets the task back to pending so the worker re-claims and resumes the resolve loop.
    """
    answer = body.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise HTTPException(status_code=400, detail="answer (non-empty string) required")

    with connect() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="task not found")
        if row["status"] != "awaiting_user_input":
            raise HTTPException(
                status_code=400,
                detail=f"task is in status '{row['status']}', not awaiting_user_input",
            )

        conn.execute(
            """INSERT INTO agent_messages
               (id, task_id, agent_run_id, agent_name, role, message_type,
                direction, content, structured_json, created_at)
               VALUES (?, ?, NULL, 'user', 'user', ?, 'from_user', ?, NULL, ?)""",
            (
                message_id(),
                task_id,
                MessageType.USER_INPUT_RESPONSE.value,
                answer,
                now_iso(),
            ),
        )
        conn.execute(
            "UPDATE tasks SET status = 'pending', updated_at = ? WHERE id = ?",
            (now_iso(), task_id),
        )

    return {"task_id": task_id, "status": "pending", "message": "answer received; task will resume"}


def _column_or_none(row, key: str):
    """sqlite3.Row supports keys() at runtime; defensive lookup so old DBs don't 500."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


@router.post("/{task_id}/decide")
async def record_decision(task_id: str, body: dict = Body(...)) -> dict[str, Any]:
    """
    Record the user's authoritative decision on a task. Per the Conclave Charter,
    significant work closes with a decision record. The decision is free-form text
    so the user can structure it as they see fit (chosen / why / rejected / risks).

    Body: {"decision": "..."}
    Allowed when the task is in any terminal status (completed, failed, cancelled);
    re-POSTing overwrites the prior decision and updates the timestamp.
    """
    decision = body.get("decision")
    if not isinstance(decision, str) or not decision.strip():
        raise HTTPException(status_code=400, detail="decision (non-empty string) required")

    with connect() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="task not found")
        if row["status"] not in ("completed", "failed", "cancelled"):
            raise HTTPException(
                status_code=400,
                detail=f"task is in status '{row['status']}'; can only record decision on a terminal task",
            )
        decided_at = now_iso()
        conn.execute(
            "UPDATE tasks SET user_decision = ?, user_decided_at = ?, updated_at = ? WHERE id = ?",
            (decision.strip(), decided_at, decided_at, task_id),
        )
    return {"task_id": task_id, "user_decision": decision.strip(), "user_decided_at": decided_at}


@router.post("/{task_id}/export")
async def export_task(task_id: str) -> dict[str, Any]:
    """Export a terminal task as a markdown decision record.

    Writes data/exports/<task_id>.md (overwriting any prior export for the same
    task) and returns the absolute path. Read-only with respect to the DB - this
    endpoint never modifies task rows. Allowed only on terminal tasks (completed,
    failed, cancelled) because the transcript is otherwise still in flight.
    """
    with connect() as conn:
        task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task_row:
            raise HTTPException(status_code=404, detail="task not found")
        if task_row["status"] not in _TERMINAL_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"task is in status '{task_row['status']}'; can only export a "
                    f"terminal task (completed, failed, cancelled)"
                ),
            )

        messages = conn.execute(
            "SELECT * FROM agent_messages WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        ).fetchall()

        result_row = conn.execute(
            "SELECT * FROM final_results WHERE task_id = ?", (task_id,)
        ).fetchone()

        runs = conn.execute(
            """SELECT id, agent_name, role, round_number, started_at, finished_at,
                      status, exit_code, duration_ms, error_code, error_message,
                      input_tokens, output_tokens, cost_usd
               FROM agent_runs WHERE task_id = ? ORDER BY started_at""",
            (task_id,),
        ).fetchall()

    task_dict = {
        "id": task_row["id"],
        "status": task_row["status"],
        "mode": task_row["mode"],
        "task_type": task_row["task_type"],
        "user_request": task_row["user_request"],
        "primary_agent": task_row["primary_agent"],
        "consultants": json.loads(task_row["consultants"]),
        "project_path": task_row["project_path"],
        "permissions": json.loads(task_row["permissions_json"]),
        "limits": json.loads(task_row["limits_json"]),
        "created_at": task_row["created_at"],
        "updated_at": task_row["updated_at"],
        "error_message": task_row["error_message"],
        "user_decision": _column_or_none(task_row, "user_decision"),
        "user_decided_at": _column_or_none(task_row, "user_decided_at"),
        "parent_task_id": _column_or_none(task_row, "parent_task_id"),
    }
    messages_list = [
        {
            "id": m["id"],
            "agent_name": m["agent_name"],
            "role": m["role"],
            "message_type": m["message_type"],
            "direction": m["direction"],
            "content": m["content"],
            "structured": json.loads(m["structured_json"]) if m["structured_json"] else None,
            "created_at": m["created_at"],
        }
        for m in messages
    ]
    final_result_dict = _row_to_final_result(result_row) if result_row else None
    agent_runs_list = [
        {
            "id": r["id"],
            "agent_name": r["agent_name"],
            "role": r["role"],
            "round_number": r["round_number"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "status": r["status"],
            "duration_ms": r["duration_ms"],
            "error_code": r["error_code"],
            "error_message": r["error_message"],
            "input_tokens": _column_or_none(r, "input_tokens"),
            "output_tokens": _column_or_none(r, "output_tokens"),
            "cost_usd": _column_or_none(r, "cost_usd"),
        }
        for r in runs
    ]

    markdown = export_to_markdown(task_dict, messages_list, final_result_dict, agent_runs_list)

    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    export_path = (EXPORTS_DIR / f"{task_id}.md").resolve()
    bytes_written = export_path.write_text(markdown, encoding="utf-8")

    # Mark the task as exported so the inbox can filter / surface this and so
    # a future "trim Tier 2 after export" policy has the data to act on.
    exported_at = now_iso()
    with connect() as conn:
        conn.execute(
            "UPDATE tasks SET exported_at = ?, export_path = ?, updated_at = ? WHERE id = ?",
            (exported_at, str(export_path), exported_at, task_id),
        )

    return {
        "task_id": task_id,
        "export_path": str(export_path),
        "bytes_written": bytes_written,
        "exported_at": exported_at,
    }


@router.post("/export-batch")
async def export_batch(body: dict = Body(default={})) -> dict[str, Any]:
    """Bulk export multiple Tier 2 tasks at once.

    Body:
      {
        "task_ids": ["tsk_...", ...]  # optional explicit list
        OR
        "filter": "unexported_terminal"  # all completed/failed/cancelled tasks without exported_at
      }

    If both omitted, defaults to filter=unexported_terminal.

    Returns:
      {"exported": [...], "skipped": [...], "errors": [...]}
    """
    task_ids = body.get("task_ids")
    filter_name = body.get("filter", "unexported_terminal")

    target_ids: list[str] = []
    if task_ids:
        if not isinstance(task_ids, list):
            raise HTTPException(status_code=400, detail="task_ids must be a list")
        target_ids = [str(t) for t in task_ids]
    elif filter_name == "unexported_terminal":
        with connect() as conn:
            rows = conn.execute(
                """SELECT id FROM tasks
                   WHERE status IN ('completed', 'failed', 'cancelled')
                     AND exported_at IS NULL
                   ORDER BY created_at"""
            ).fetchall()
        target_ids = [r["id"] for r in rows]
    else:
        raise HTTPException(status_code=400, detail=f"unknown filter: {filter_name}")

    exported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for tid in target_ids:
        try:
            resp = await export_task(tid)
            exported.append({"task_id": tid, "export_path": resp["export_path"]})
        except HTTPException as e:
            if e.status_code == 400:
                skipped.append({"task_id": tid, "reason": e.detail})
            elif e.status_code == 404:
                skipped.append({"task_id": tid, "reason": "not found"})
            else:
                errors.append({"task_id": tid, "status_code": e.status_code, "detail": e.detail})
        except Exception as e:  # noqa: BLE001
            errors.append({"task_id": tid, "error": str(e)})

    return {
        "exported_count": len(exported),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "exported": exported,
        "skipped": skipped,
        "errors": errors,
    }


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="task not found")
        if row["status"] in ("completed", "failed", "cancelled"):
            return {
                "task_id": task_id,
                "status": row["status"],
                "message": "task already terminal",
            }
        conn.execute(
            "UPDATE tasks SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (now_iso(), task_id),
        )
    return {"task_id": task_id, "status": "cancelled"}
