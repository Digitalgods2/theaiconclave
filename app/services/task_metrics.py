"""Decision feedback and measured compute; no dependency on agent execution."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.database import connect

TERMINAL = {"completed", "failed", "cancelled"}
SIGNALS = ("decision_changed", "material_risk_found", "extra_review_worth_it")


def compute_summary(runs: list[dict]) -> dict[str, Any]:
    count = len(runs)
    cost_count = sum(r.get("cost_usd") is not None for r in runs)
    token_count = sum(r.get("input_tokens") is not None and r.get("output_tokens") is not None for r in runs)
    times = [r["duration_ms"] for r in runs if r.get("duration_ms") is not None]
    starts = [datetime.fromisoformat(r["started_at"]) for r in runs if r.get("started_at")]
    ends = [datetime.fromisoformat(r["finished_at"]) for r in runs if r.get("finished_at")]
    return {
        "agent_runs": count,
        "input_tokens": sum(r.get("input_tokens") or 0 for r in runs)
            if any(r.get("input_tokens") is not None for r in runs) else None,
        "output_tokens": sum(r.get("output_tokens") or 0 for r in runs)
            if any(r.get("output_tokens") is not None for r in runs) else None,
        "known_cost_usd": sum(r.get("cost_usd") or 0 for r in runs) if cost_count else None,
        "cost_reported_runs": cost_count, "token_reported_runs": token_count,
        "cost_coverage": cost_count / count if count else None,
        "token_coverage": token_count / count if count else None,
        "agent_duration_ms": sum(times) if times else None,
        "elapsed_ms": max(0, int((max(ends) - min(starts)).total_seconds() * 1000))
            if starts and ends and len(ends) == count else None,
    }


def get_feedback(task_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM task_feedback WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    for field in SIGNALS:
        result[field] = bool(result[field]) if result[field] is not None else None
    return result


def mode_metrics() -> dict[str, Any]:
    # Fetch aggregate inputs only: no questions, answers, decisions, or feedback notes.
    with connect() as conn:
        tasks = conn.execute(
            "SELECT t.id, t.mode, t.status, f.task_id AS rated, "
            "f.decision_changed, f.material_risk_found, f.extra_review_worth_it "
            "FROM tasks t LEFT JOIN task_feedback f ON f.task_id = t.id"
        ).fetchall()
        runs = conn.execute(
            "SELECT task_id, started_at, finished_at, duration_ms, input_tokens, output_tokens, cost_usd "
            "FROM agent_runs"
        ).fetchall()
    per_task: dict[str, list[dict]] = {}
    for run in runs:
        per_task.setdefault(run["task_id"], []).append(dict(run))
    groups = []
    for mode in ("resolve", "consult", "conclave"):
        rows = [row for row in tasks if row["mode"] == mode]
        count = len(rows)
        terminal = sum(row["status"] in TERMINAL for row in rows)
        completed = sum(row["status"] == "completed" for row in rows)
        rated = sum(bool(row["rated"]) for row in rows)
        valuable = sum(row["decision_changed"] == 1 or row["material_risk_found"] == 1 for row in rows)
        mode_runs = [run for row in rows for run in per_task.get(row["id"], [])]
        summary = compute_summary(mode_runs)
        # Across tasks wall-clock span is not a useful elapsed-time statistic.
        summary.pop("elapsed_ms")
        signals = {field: {
            "answered": sum(row[field] is not None for row in rows),
            "yes": sum(row[field] == 1 for row in rows),
        } for field in SIGNALS}
        groups.append({
            "mode": mode, "task_count": count, "terminal_tasks": terminal,
            "completed_tasks": completed, "failed_tasks": sum(r["status"] == "failed" for r in rows),
            "cancelled_tasks": sum(r["status"] == "cancelled" for r in rows),
            "completion_rate": completed / terminal if terminal else None,
            "rated_tasks": rated, "feedback_coverage": rated / terminal if terminal else None,
            "valuable_tasks": valuable, "value_rate": valuable / rated if rated else None,
            "runs_per_task": len(mode_runs) / count if count else None,
            "compute_per_valuable_decision": len(mode_runs) / valuable if valuable else None,
            "signals": signals, "compute_summary": summary,
        })
    return {"modes": groups, "definitions": {
        "completion_rate": "completed / terminal tasks",
        "feedback_coverage": "feedback rows / terminal tasks",
        "value_rate": "decision changed or material risk found / rated tasks",
        "compute_per_valuable_decision": "all mode runs / positively rated decisions",
        "cost": "reported costs only; unknown costs are not zero",
    }}
