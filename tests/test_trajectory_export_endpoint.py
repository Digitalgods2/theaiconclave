"""HTTP-surface tests for the trajectory export endpoints.

Covers:
  - POST /api/tasks/{id}/trajectory/export returns 200 + writes the file.
  - POST /api/tasks/{id}/trajectory/export returns 404 for an unknown task.
  - POST /api/trajectories/export-all returns a summary including the new file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import tasks as tasks_api
from app.database import connect, init_database, now_iso
from app.utils.ids import message_id, result_id, run_id, task_id as new_task_id
from app.utils.paths import trajectories_root


@pytest.fixture
def client(tmp_path):
    """Spin up a TestClient with only the tasks routers mounted."""
    init_database(str(tmp_path / "test.db"))
    app = FastAPI()
    app.include_router(tasks_api.router)
    app.include_router(tasks_api.trajectories_router)
    return TestClient(app)


def _seed_terminal_task(*, status: str = "completed") -> str:
    """Insert a minimal terminal task + run + message + final_result row."""
    tid = new_task_id()
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
               (id, created_at, updated_at, status, source, source_agent, mode,
                task_type, user_request, primary_agent, consultants, project_path,
                context_json, permissions_json, limits_json)
               VALUES (?, ?, ?, ?, 'api', NULL, 'consult', 'general_consultation',
                       'q', 'alpha', ?, NULL, ?, ?, ?)""",
            (
                tid, now, now, status,
                json.dumps(["beta"]),
                json.dumps({"files": [], "error": None, "git_diff": None, "extra": {}}, sort_keys=True),
                json.dumps({
                    "can_read_files": True, "can_write_files": False,
                    "can_run_commands": False, "can_access_network": False,
                    "can_install_packages": False, "can_apply_patches": False,
                    "can_read_env_files": False, "can_read_secrets": False,
                }, sort_keys=True),
                json.dumps({"max_rounds": 3, "timeout_seconds": 30}, sort_keys=True),
            ),
        )
        rid = run_id()
        conn.execute(
            """INSERT INTO agent_runs
               (id, task_id, agent_name, role, round_number, started_at, finished_at,
                status, duration_ms)
               VALUES (?, ?, 'alpha', 'primary', 1, ?, ?, 'completed', 100)""",
            (rid, tid, now, now),
        )
        conn.execute(
            """INSERT INTO agent_messages
               (id, task_id, agent_run_id, agent_name, role, message_type,
                direction, content, structured_json, created_at)
               VALUES (?, ?, ?, 'alpha', 'primary', 'primary_final', 'from_agent', 'done', NULL, ?)""",
            (message_id(), tid, rid, now),
        )
        conn.execute(
            """INSERT INTO final_results
               (id, task_id, final_answer, agreement_level, resolution_status,
                disagreements_json, recommended_actions_json, action_plan_json, risks_json,
                commands_requiring_approval_json, patches_requiring_approval_json,
                errors_json, confidence_aggregate_json, failure_cause_tags_json, created_at)
               VALUES (?, ?, 'Final.', 'consensus', NULL, '[]', '[]', '[]', '[]',
                       '[]', '[]', '[]', NULL, '[]', ?)""",
            (result_id(), tid, now),
        )
    return tid


# ---------------------------------------------------------------------------
# Single-task export
# ---------------------------------------------------------------------------

def test_export_endpoint_returns_200_and_writes_file(client):
    tid = _seed_terminal_task()
    resp = client.post(f"/api/tasks/{tid}/trajectory/export")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == tid
    assert body["bytes"] > 0

    written = Path(body["path"])
    assert written.exists()
    assert written.parent == trajectories_root()
    # Single-line JSONL.
    text = written.read_text(encoding="utf-8")
    assert text.endswith("\n")
    record = json.loads(text)
    assert record["task_id"] == tid


def test_export_endpoint_returns_404_for_unknown_task(client):
    resp = client.post("/api/tasks/tsk_does_not_exist/trajectory/export")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Bulk export-all
# ---------------------------------------------------------------------------

def test_export_all_endpoint_returns_summary(client):
    tid1 = _seed_terminal_task(status="completed")
    tid2 = _seed_terminal_task(status="failed")
    # Non-terminal task — should be skipped by the sweep.
    tid_pending = _seed_terminal_task(status="completed")
    with connect() as conn:
        conn.execute("UPDATE tasks SET status = 'pending' WHERE id = ?", (tid_pending,))

    resp = client.post("/api/trajectories/export-all")
    assert resp.status_code == 200
    body = resp.json()
    exported_ids = {e["task_id"] for e in body["exported"]}
    assert tid1 in exported_ids
    assert tid2 in exported_ids
    assert tid_pending not in exported_ids
    assert body["error_count"] == 0
