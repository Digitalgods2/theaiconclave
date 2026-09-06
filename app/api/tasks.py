"""Task API endpoints."""

from __future__ import annotations

import json
import shutil
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response

from app.database import connect, now_iso, with_retry
from app.protocol.validators import MessageType, TaskRequest
from app.services.artifacts import list_artifacts
from app.services.results import serialize_final_result
from app.services.task_metrics import compute_summary, get_feedback
from app.services.exporter import export_to_markdown
from app.services import doc_export
from app.utils.ids import message_id, task_id as new_task_id
from app.utils.paths import artifacts_root, exports_root, sandboxes_root

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# Sibling router for trajectory operations that don't fit cleanly under
# /api/tasks/{id}/... — currently just the bulk export-all sweep. Lives in
# this module so the trajectory surface stays in one place. Wired into the
# FastAPI app from app/main.py alongside the main tasks router.
trajectories_router = APIRouter(prefix="/api/trajectories", tags=["trajectories"])

# Decision-record exports land under `<user_data_root>/exports/` (DR0016).
# Kept deterministic so the user can re-export idempotently (same task_id ->
# same file). The directory is created on demand by `exports_root()`.


_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}

# Statuses where the worker may still be mid-run on the task (or it is paused
# waiting on the user). Deleting one of these would race the worker, so the
# delete endpoint refuses them — cancel first. Mirrors the active set swept in
# app/main.py's startup sandbox sweep.
_ACTIVE_STATUSES = {"pending", "running", "awaiting_user_input", "waiting_for_user"}


@router.post("")
async def create_task(request: TaskRequest) -> dict[str, Any]:
    from app.config import get_config
    config = get_config()
    if request.mode.value == "conclave":
        configured_judge = request.judge_agent or config.orchestration.judge_agent
        configured_synthesis = request.synthesis_agent or config.orchestration.synthesis_agent
        participants = set(request.consultants)
        if configured_judge in participants or configured_synthesis in participants:
            raise HTTPException(
                status_code=400,
                detail="configured judge/synthesis seats must not also be conclave participants",
            )
        request = TaskRequest.model_validate({
            **request.model_dump(mode="json"),
            "judge_agent": configured_judge,
            "synthesis_agent": configured_synthesis,
        })
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
    project_row = None
    if request.decision_project_id:
        with connect() as conn:
            project_row = conn.execute(
                """SELECT id, instructions, default_evidence_urls_json
                   FROM decision_projects WHERE id = ? AND archived_at IS NULL""",
                (request.decision_project_id,),
            ).fetchone()
        if project_row is None:
            raise HTTPException(status_code=400, detail="decision_project_id does not exist or is archived")
        request.context.extra["decision_project_instructions"] = project_row["instructions"]
    # Validate every named agent is currently registered. Catching this at
    # submit time gives the user a clean 400 instead of a buried error in the
    # task's final_results.errors_json after the conclave runs without them.
    from app.services import agent_registry as _registry
    registered = set(_registry.list_names())
    referenced: list[str] = []
    if request.primary_agent:
        referenced.append(request.primary_agent)
    referenced.extend(request.consultants or [])
    if request.judge_agent:
        referenced.append(request.judge_agent)
    if request.synthesis_agent:
        referenced.append(request.synthesis_agent)
    missing = [a for a in referenced if a not in registered]
    if missing:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "agent_unavailable",
                "message": (
                    "Agent(s) not registered: " + ", ".join(missing) + ". "
                    "Available: " + ", ".join(sorted(registered)) + ". "
                    "If the agent is listed in config.yaml, the service may need a restart."
                ),
                "missing": missing,
                "available": sorted(registered),
            },
        )
    # Decision Memory retrieval — Phase 2.5 of post-DR plan
    # tsk_01KRSW6AS3M66B4RRJE3JFAPRV. Frozen at create time so the user later
    # sees exactly the prior art the agents saw.
    from app.services.decision_memory import find_relevant
    try:
        prior_art = find_relevant(request.user_request, top_k=3)
    except Exception:  # noqa: BLE001 — never block task creation on retrieval failure
        prior_art = []
    prior_art_json = json.dumps(prior_art) if prior_art else None

    requested_evidence = request.context.extra.get("evidence_snapshot_ids") or []
    if not isinstance(requested_evidence, list) or not all(isinstance(v, str) for v in requested_evidence):
        raise HTTPException(status_code=400, detail="context.extra.evidence_snapshot_ids must be a list of IDs")
    evidence_ids = list(dict.fromkeys(requested_evidence))
    if request.decision_project_id and "evidence_snapshot_ids" not in request.context.extra:
        with connect() as conn:
            project_evidence = conn.execute(
                """SELECT id FROM evidence_snapshots WHERE project_id = ?
                   ORDER BY created_at DESC LIMIT 20""",
                (request.decision_project_id,),
            ).fetchall()
        evidence_ids = list(dict.fromkeys([row["id"] for row in project_evidence] + evidence_ids))
    if evidence_ids:
        with connect() as conn:
            found = {
                row["id"] for row in conn.execute(
                    f"SELECT id FROM evidence_snapshots WHERE id IN ({','.join('?' for _ in evidence_ids)})",
                    evidence_ids,
                ).fetchall()
            }
        missing_evidence = [value for value in evidence_ids if value not in found]
        if missing_evidence:
            raise HTTPException(status_code=400, detail={
                "code": "evidence_not_found", "missing": missing_evidence,
            })
    request.context.extra["evidence_snapshot_ids"] = evidence_ids

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks
            (id, created_at, updated_at, status, source, source_agent, mode,
             task_type, user_request, primary_agent, consultants, project_path,
             context_json, permissions_json, limits_json, parent_task_id,
             prior_art_json, decision_project_id, judge_agent, synthesis_agent,
             evidence_snapshot_ids_json)
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                prior_art_json,
                request.decision_project_id,
                request.judge_agent,
                request.synthesis_agent,
                json.dumps(evidence_ids),
            ),
        )
    return {
        "task_id": tid,
        "status": "pending",
        "created_at": now,
        "parent_task_id": request.parent_task_id,
        "prior_art": prior_art,
        "decision_project_id": request.decision_project_id,
        "evidence_snapshot_ids": evidence_ids,
    }


@router.get("")
async def list_tasks(
    status: Optional[str] = None,
    limit: int = 50,
    exported: Optional[str] = None,
    q: Optional[str] = None,
) -> dict[str, Any]:
    """List tasks, newest first.

    Filters:
      status=<value>: server-side filter on status column.
      exported=true: only tasks where exported_at IS NOT NULL.
      exported=false: only tasks where exported_at IS NULL.
      q=<text>: case-insensitive substring match across task id,
        user_request, user_decision, and the linked final_results.final_answer.
        Whole-DB search (bypasses the `limit` paging behavior — the search
        runs first, then the newest `limit` matches are returned).
    """
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("tasks.status = ?")
        params.append(status)
    if exported is not None:
        norm = str(exported).lower()
        if norm in ("true", "1", "yes"):
            clauses.append("tasks.exported_at IS NOT NULL")
        elif norm in ("false", "0", "no"):
            clauses.append("tasks.exported_at IS NULL")
    if q:
        # Case-insensitive LIKE across id + user_request + user_decision +
        # joined final_results.final_answer. Uses LOWER() on both sides to
        # handle non-ASCII case-insensitively (SQLite's LIKE is only ASCII
        # case-insensitive by default).
        needle = f"%{q.lower()}%"
        clauses.append(
            "(LOWER(tasks.id) LIKE ?"
            " OR LOWER(tasks.user_request) LIKE ?"
            " OR LOWER(COALESCE(tasks.user_decision, '')) LIKE ?"
            " OR tasks.id IN (SELECT task_id FROM final_results"
            "                 WHERE LOWER(final_answer) LIKE ?))"
        )
        params.extend([needle, needle, needle, needle])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    # LEFT JOIN final_results so each inbox row carries its failure_cause_tags;
    # the client filters by tag inline (DR0022).
    sql = (
        f"""SELECT tasks.id AS id, tasks.status AS status, tasks.mode AS mode,
                   tasks.task_type AS task_type,
                   tasks.primary_agent AS primary_agent,
                   tasks.consultants AS consultants,
                   tasks.created_at AS created_at, tasks.updated_at AS updated_at,
                   tasks.exported_at AS exported_at, tasks.export_path AS export_path,
                   tasks.source AS source, tasks.source_agent AS source_agent,
                   tasks.decision_project_id AS decision_project_id,
                   SUBSTR(tasks.user_request, 1, 120) AS user_request_snippet,
                   final_results.failure_cause_tags_json AS failure_cause_tags_json
            FROM tasks
            LEFT JOIN final_results ON final_results.task_id = tasks.id
            {where}
            ORDER BY tasks.created_at DESC LIMIT ?"""
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
                "decision_project_id": _column_or_none(r, "decision_project_id"),
                "user_request_snippet": _column_or_none(r, "user_request_snippet"),
                "failure_cause_tags": _safe_parse_json(
                    _column_or_none(r, "failure_cause_tags_json"), []
                ),
            }
            for r in rows
        ]
    }


@router.get("/usage")
async def usage_summary() -> dict[str, Any]:
    """Aggregate token and cost data from agent_runs for the Usage & Spend panel.

    Returns today-by-agent, last-7-days-by-agent, a 7-day daily series, and
    all-time totals. Only agent_runs with recorded `cost_usd` contribute to
    the spend columns; CLI subscription runs show — for spend but non-zero
    token counts when the adapters record them.
    """
    with connect() as conn:
        today_rows = conn.execute(
            """
            SELECT agent_name,
                   SUM(COALESCE(cost_usd, 0))      AS total_cost,
                   SUM(COALESCE(input_tokens, 0))  AS total_input,
                   SUM(COALESCE(output_tokens, 0)) AS total_output,
                   COUNT(*)                         AS run_count
            FROM agent_runs
            WHERE DATE(started_at) = DATE('now')
            GROUP BY agent_name
            ORDER BY total_cost DESC
            """
        ).fetchall()

        # Per-agent breakdown over the same 7-day window the chart uses. Lets
        # the user see who contributed to weekly spend even on days when
        # today's conclave didn't include the OpenRouter seats.
        last7d_rows = conn.execute(
            """
            SELECT agent_name,
                   SUM(COALESCE(cost_usd, 0))      AS total_cost,
                   SUM(COALESCE(input_tokens, 0))  AS total_input,
                   SUM(COALESCE(output_tokens, 0)) AS total_output,
                   COUNT(*)                         AS run_count
            FROM agent_runs
            WHERE started_at >= DATE('now', '-6 days')
            GROUP BY agent_name
            ORDER BY total_cost DESC, agent_name ASC
            """
        ).fetchall()

        daily_rows = conn.execute(
            """
            SELECT DATE(started_at)                  AS day,
                   SUM(COALESCE(cost_usd, 0))        AS total_cost,
                   SUM(COALESCE(input_tokens, 0))    AS total_input,
                   SUM(COALESCE(output_tokens, 0))   AS total_output
            FROM agent_runs
            WHERE started_at >= DATE('now', '-6 days')
            GROUP BY day
            ORDER BY day ASC
            """
        ).fetchall()

        totals = conn.execute(
            """
            SELECT SUM(COALESCE(cost_usd, 0))      AS total_cost,
                   SUM(COALESCE(input_tokens, 0))  AS total_input,
                   SUM(COALESCE(output_tokens, 0)) AS total_output,
                   COUNT(DISTINCT task_id)          AS task_count
            FROM agent_runs
            """
        ).fetchone()

    def _agent_row(r):
        return {
            "agent_name": r["agent_name"],
            "cost_usd": r["total_cost"],
            "input_tokens": r["total_input"],
            "output_tokens": r["total_output"],
            "run_count": r["run_count"],
        }

    return {
        "today_by_agent": [_agent_row(r) for r in today_rows],
        "last7d_by_agent": [_agent_row(r) for r in last7d_rows],
        "daily_7d": [
            {
                "day": r["day"],
                "cost_usd": r["total_cost"],
                "input_tokens": r["total_input"],
                "output_tokens": r["total_output"],
            }
            for r in daily_rows
        ],
        "all_time": {
            "cost_usd": totals["total_cost"] if totals else 0,
            "input_tokens": totals["total_input"] if totals else 0,
            "output_tokens": totals["total_output"] if totals else 0,
            "task_count": totals["task_count"] if totals else 0,
        },
    }


def _safe_parse_json(raw, default):
    """Best-effort json.loads — return `default` on any failure. Used for
    columns where a malformed value shouldn't blow up the whole API call."""
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def _enriched_prior_art(task_row) -> list[dict[str, Any]]:
    """Read frozen prior_art_json off the task row and annotate each entry with
    its CURRENT supersession state. Pre-supersession-tracking tasks (frozen
    before the supersession parser shipped) get the badge retroactively;
    fresh tasks just round-trip their stored values."""
    raw = _column_or_none(task_row, "prior_art_json")
    if not raw:
        return []
    try:
        matches = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(matches, list):
        return []
    from app.services.decision_memory import enrich_with_supersession
    return enrich_with_supersession(matches)


def _row_to_final_result(row) -> dict[str, Any]:
    return serialize_final_result(row)


def _compute_confidence_trajectory(messages) -> list[dict[str, Any]]:
    """Per-participant confidence trajectory across rounds, reconstructed from agent_messages.

    Returns [{"agent": "codex", "rounds": [{"round": 1, "confidence": 0.86,
    "convergence": "i_am_done"}, ...]}, ...]. Used by the dashboard to surface
    the who-persuaded-whom narrative on weak convergence. Phase 2 of post-DR
    plan on tsk_01KRSW6AS3M66B4RRJE3JFAPRV.
    """
    per_agent: dict[str, list[dict[str, Any]]] = {}
    for m in messages:
        if m["message_type"] != MessageType.CONCLAVE_TURN.value:
            continue
        if not m["structured_json"]:
            continue
        try:
            s = json.loads(m["structured_json"])
        except (ValueError, TypeError):
            continue
        agent = m["agent_name"]
        per_agent.setdefault(agent, []).append({
            "round": len(per_agent[agent]) + 1,
            "confidence": s.get("confidence"),
            "convergence": s.get("convergence"),
        })
    return [{"agent": a, "rounds": rounds} for a, rounds in per_agent.items()]


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

        runs = conn.execute(
            """SELECT id, agent_name, role, round_number, started_at, finished_at,
                      status, exit_code, duration_ms, error_code, error_message,
                      input_tokens, output_tokens, cost_usd
               FROM agent_runs WHERE task_id = ? ORDER BY started_at""",
            (task_id,),
        ).fetchall()

    return {
        "feedback": get_feedback(task_id),
        "compute_summary": compute_summary([dict(run) for run in runs]),
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
            "decision_project_id": _column_or_none(task_row, "decision_project_id"),
            "judge_agent": _column_or_none(task_row, "judge_agent"),
            "synthesis_agent": _column_or_none(task_row, "synthesis_agent"),
            "exported_at": _column_or_none(task_row, "exported_at"),
            "export_path": _column_or_none(task_row, "export_path"),
            "source": _column_or_none(task_row, "source"),
            "source_agent": _column_or_none(task_row, "source_agent"),
            "prior_art": _enriched_prior_art(task_row),
            # The full context (user input + orchestrator-mutated fields like
            # sandbox_path/thread_ancestors/prior_art). The dashboard's
            # "Continue thread" reads context.extra.include_sandbox to
            # inherit the sandbox checkbox from the parent — without this,
            # the follow-up task drops sandbox access silently.
            "context": _safe_parse_json(_column_or_none(task_row, "context_json"), default={}),
        },
        "messages": [
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
        ],
        "final_result": (
            {**_row_to_final_result(result_row),
             "confidence_trajectory": _compute_confidence_trajectory(messages)}
            if result_row else None
        ),
        # Compatibility key for pre-1.2 clients. Action policy is represented
        # by final_result.action_plan; no runtime approval workflow exists.
        "approvals": [],
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
        "artifacts": list_artifacts(task_id, include_content=True),
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

    artifacts_list = list_artifacts(task_id, include_content=True)
    markdown = export_to_markdown(
        task_dict, messages_list, final_result_dict, agent_runs_list, artifacts_list
    )

    export_path = (exports_root() / f"{task_id}.md").resolve()
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


def _load_task_bundle(task_id: str) -> tuple[dict, list[dict], dict | None, list[dict], list[dict]]:
    """Load the full task envelope (task dict, messages, final_result, agent_runs)
    in the shape the exporters expect. Raises HTTPException(404) if not found.

    Unlike the markdown decision-record export, this does NOT require a terminal
    status and does NOT mark the task as exported - it's a read-only snapshot.
    """
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
        "source": _column_or_none(task_row, "source"),
        "source_agent": _column_or_none(task_row, "source_agent"),
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
    return task_dict, messages_list, final_result_dict, agent_runs_list, list_artifacts(task_id, include_content=True)


_DOWNLOAD_FORMATS = {
    "pdf": ("application/pdf", "pdf"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "md": ("text/markdown; charset=utf-8", "md"),
    "txt": ("text/plain; charset=utf-8", "txt"),
}


@router.get("/{task_id}/download")
async def download_task_detail(task_id: str, format: str = "pdf") -> Response:
    """Stream the full task detail as a downloadable document.

    Query param `format`: pdf | docx | md | txt (default pdf).

    Returns the file with a Content-Disposition: attachment header and a
    suggested filename derived from the mode + question + task id. The browser's
    own Save dialog handles where it lands; this endpoint never writes to disk
    and never modifies the task (distinct from POST /export, the Tier 2 archive).
    """
    fmt = (format or "pdf").lower()
    if fmt not in _DOWNLOAD_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported format '{format}'; choose one of: {', '.join(sorted(_DOWNLOAD_FORMATS))}",
        )

    task_dict, messages_list, final_result_dict, agent_runs_list, artifacts_list = _load_task_bundle(task_id)

    if fmt == "pdf":
        data = doc_export.render_pdf(task_dict, messages_list, final_result_dict, agent_runs_list)
    elif fmt == "docx":
        data = doc_export.render_docx(task_dict, messages_list, final_result_dict, agent_runs_list)
    else:  # md or txt
        text = export_to_markdown(
            task_dict, messages_list, final_result_dict, agent_runs_list, artifacts_list
        )
        data = text.encode("utf-8")

    media_type, ext = _DOWNLOAD_FORMATS[fmt]
    filename = f"{doc_export.filename_stem(task_dict)}.{ext}"
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@trajectories_router.post("/export-all")
async def export_all_trajectories() -> dict[str, Any]:
    """Sweep every terminal task and (re)write its trajectory JSONL.

    Returns `{exported_count, error_count, exported, errors}` — per-task
    failures are caught so one bad task doesn't abort the batch (useful when
    sweeping a long history for the first time). Files land under
    `<exports_root>/trajectories/`.
    """
    from app.services import trajectory_exporter
    return trajectory_exporter.export_all_terminal()


@router.post("/{task_id}/trajectory/export")
async def export_task_trajectory(task_id: str) -> dict[str, Any]:
    """Re-export this task's trajectory as a single-line JSONL file.

    Writes `<exports_root>/trajectories/<task_id>.jsonl` and returns
    `{path, bytes}`. Idempotent — calling twice overwrites cleanly with the
    current DB state. Unlike `/export` (the markdown Tier 2 archive), this
    endpoint:
      - works on any task that exists (terminal or otherwise);
      - never mutates the task row;
      - is also fired automatically by the orchestrator when a task hits a
        terminal status (see `_post_finalize_hooks`). This endpoint is the
        on-demand path used by the dashboard / "Export all" sweep.
    """
    with connect() as conn:
        row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")

    from app.services import trajectory_exporter
    try:
        path = trajectory_exporter.export_trajectory(task_id)
    except ValueError as e:
        # Race: task was deleted between the 404 check and the export.
        raise HTTPException(status_code=404, detail=str(e))
    return {
        "task_id": task_id,
        "path":    str(path),
        "bytes":   path.stat().st_size,
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
    from app.services import task_control
    interrupted = task_control.cancel(task_id)
    return {
        "task_id": task_id,
        "status": "cancelled",
        "interrupted_active_call": interrupted,
    }


@router.post("/{task_id}/retry")
async def retry_task(task_id: str) -> dict[str, Any]:
    """Start a linked attempt without rewriting the original audit history."""
    from app.services import task_control
    from app.services.orchestrator import _load_task
    with connect() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    if row["status"] not in {"failed", "cancelled"} or task_control.is_active(task_id):
        raise HTTPException(status_code=409, detail="retry requires a stopped failed or cancelled task")
    try:
        request = _load_task(task_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail="legacy task cannot be retried; submit a new task") from error
    if request is None:
        raise HTTPException(status_code=404, detail="task not found")
    request.parent_task_id = task_id
    for key in ("sandbox_path", "thread_ancestors", "prior_art", "prior_artifacts", "evidence_snapshots"):
        request.context.extra.pop(key, None)
    return await create_task(request)


@router.delete("/{task_id}")
async def delete_task(task_id: str) -> dict[str, Any]:
    """Permanently delete a task and everything attached to it.

    Hard delete: the task row plus its agent runs, transcript messages, final
    result, approvals, and draft-artifact rows are removed (FK `ON DELETE
    CASCADE`), the on-disk per-task sandbox and artifact directories are
    deleted, and any threaded child task has its `parent_task_id` cleared so
    the thread does not dangle. There is no undo.

    Refuses a task that is still in flight (pending / running / awaiting input)
    — cancel it first, so the delete cannot race the worker.
    """
    with connect() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    from app.services import task_control
    if row["status"] in _ACTIVE_STATUSES or task_control.is_active(task_id):
        raise HTTPException(
            status_code=409,
            detail=(
                f"task is still in flight (status={row['status']}); "
                f"cancel it before deleting"
            ),
        )

    def _purge() -> None:
        with connect() as conn:
            conn.execute("BEGIN")
            try:
                # Detach threaded children so their parent_task_id doesn't dangle.
                conn.execute(
                    "UPDATE tasks SET parent_task_id = NULL WHERE parent_task_id = ?",
                    (task_id,),
                )
                # FK cascade removes agent_runs, agent_messages, final_results,
                # approvals, and task_artifacts; logs.task_id is set NULL. FK
                # enforcement is on for every connection (see app/database.py).
                conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    with_retry(_purge)

    # Best-effort on-disk cleanup. The DB is already consistent; a leftover
    # directory is harmless (orphan sandboxes are swept on the next startup),
    # so a failure here must not turn a successful delete into an error.
    for base in (sandboxes_root(), artifacts_root()):
        target = base / task_id
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)

    return {"task_id": task_id, "status": "deleted"}
