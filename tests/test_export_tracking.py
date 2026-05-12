"""Tests for the enhanced Tier 2 export/archive flow.

Covers:
- POST /api/tasks/{id}/export now marks the task with exported_at + export_path
- GET /api/tasks/{id} returns the new fields
- GET /api/tasks?exported=true|false filters correctly
- POST /api/tasks/export-batch bulk-exports unexported terminal tasks
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.database import init_database, connect, now_iso
from app.services import agent_registry
from app.utils.ids import task_id as new_task_id, result_id


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """Spin up a TestClient against a fresh DB and exports dir."""
    db_path = tmp_path / "test.db"
    init_database(str(db_path))
    agent_registry.clear()
    agent_registry.init_registry()

    # Redirect EXPORTS_DIR to the temp tree so writes don't pollute data/exports
    from app.api import tasks as tasks_module
    monkeypatch.setattr(tasks_module, "EXPORTS_DIR", tmp_path / "exports")

    # Build a minimal FastAPI app that mounts only the tasks router.
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(tasks_module.router)
    return TestClient(app)


def _insert_terminal_task(status: str = "completed", final_answer: str = "Test final answer.") -> str:
    """Insert a synthetic terminal task with a final_result row."""
    tid = new_task_id()
    now = now_iso()
    perms = {"can_read_files": True, "can_write_files": False, "can_run_commands": False,
             "can_access_network": False, "can_install_packages": False,
             "can_apply_patches": False, "can_read_env_files": False, "can_read_secrets": False}
    limits = {"max_rounds": 5, "timeout_seconds": 180}
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
            (id, created_at, updated_at, status, source, source_agent, mode, task_type,
             user_request, primary_agent, consultants, project_path,
             context_json, permissions_json, limits_json)
            VALUES (?, ?, ?, ?, 'api', NULL, 'conclave', 'general_consultation',
                    'Test', NULL, '["fake"]', NULL,
                    '{}', ?, ?)""",
            (tid, now, now, status, json.dumps(perms), json.dumps(limits)),
        )
        conn.execute(
            """INSERT INTO final_results
            (id, task_id, final_answer, agreement_level, created_at)
            VALUES (?, ?, ?, 'consensus', ?)""",
            (result_id(), tid, final_answer, now),
        )
    return tid


# ---------------------------------------------------------------------------
# Export endpoint sets exported_at + export_path
# ---------------------------------------------------------------------------

def test_export_marks_task_as_exported(client):
    tid = _insert_terminal_task()
    resp = client.post(f"/api/tasks/{tid}/export")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == tid
    assert "export_path" in body
    assert body["exported_at"] is not None
    # GET should now reflect this
    get_resp = client.get(f"/api/tasks/{tid}")
    task = get_resp.json()["task"]
    assert task["exported_at"] == body["exported_at"]
    assert task["export_path"] == body["export_path"]


def test_re_exporting_overwrites_timestamp(client):
    tid = _insert_terminal_task()
    r1 = client.post(f"/api/tasks/{tid}/export")
    first_ts = r1.json()["exported_at"]
    # Re-export
    r2 = client.post(f"/api/tasks/{tid}/export")
    second_ts = r2.json()["exported_at"]
    # The second timestamp should be >= the first (lex-comparable ISO 8601)
    assert second_ts >= first_ts


# ---------------------------------------------------------------------------
# Inbox filter on exported status
# ---------------------------------------------------------------------------

def test_inbox_filter_exported_true(client):
    tid_a = _insert_terminal_task()
    tid_b = _insert_terminal_task()
    client.post(f"/api/tasks/{tid_a}/export")
    # tid_b not exported
    resp = client.get("/api/tasks?exported=true")
    ids = [t["id"] for t in resp.json()["tasks"]]
    assert tid_a in ids
    assert tid_b not in ids


def test_inbox_filter_exported_false(client):
    tid_a = _insert_terminal_task()
    tid_b = _insert_terminal_task()
    client.post(f"/api/tasks/{tid_a}/export")
    resp = client.get("/api/tasks?exported=false")
    ids = [t["id"] for t in resp.json()["tasks"]]
    assert tid_b in ids
    assert tid_a not in ids


def test_inbox_returns_exported_at_field(client):
    tid = _insert_terminal_task()
    client.post(f"/api/tasks/{tid}/export")
    resp = client.get("/api/tasks")
    for t in resp.json()["tasks"]:
        if t["id"] == tid:
            assert t["exported_at"] is not None
            return
    pytest.fail(f"task {tid} not found in inbox response")


# ---------------------------------------------------------------------------
# Bulk export endpoint
# ---------------------------------------------------------------------------

def test_batch_export_unexported_terminal(client):
    """Default filter exports every unexported terminal task in one shot."""
    tid_a = _insert_terminal_task()
    tid_b = _insert_terminal_task()
    tid_c = _insert_terminal_task()
    client.post(f"/api/tasks/{tid_c}/export")  # already exported
    resp = client.post("/api/tasks/export-batch", json={})
    body = resp.json()
    exported_ids = {item["task_id"] for item in body["exported"]}
    assert tid_a in exported_ids
    assert tid_b in exported_ids
    assert tid_c not in exported_ids  # already exported, not re-exported
    assert body["exported_count"] == 2


def test_batch_export_with_explicit_ids(client):
    """When task_ids is provided, export exactly those (regardless of exported state)."""
    tid_a = _insert_terminal_task()
    tid_b = _insert_terminal_task()
    resp = client.post("/api/tasks/export-batch", json={"task_ids": [tid_a]})
    body = resp.json()
    exported_ids = {item["task_id"] for item in body["exported"]}
    assert tid_a in exported_ids
    assert tid_b not in exported_ids


def test_batch_export_skips_non_terminal(client):
    """Non-terminal tasks in the explicit list are skipped, not errored."""
    tid_pending = new_task_id()
    now = now_iso()
    perms = {"can_read_files": True, "can_write_files": False, "can_run_commands": False,
             "can_access_network": False, "can_install_packages": False,
             "can_apply_patches": False, "can_read_env_files": False, "can_read_secrets": False}
    limits = {"max_rounds": 5, "timeout_seconds": 180}
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
            (id, created_at, updated_at, status, source, source_agent, mode, task_type,
             user_request, primary_agent, consultants, project_path,
             context_json, permissions_json, limits_json)
            VALUES (?, ?, ?, 'pending', 'api', NULL, 'conclave', 'general_consultation',
                    'Test', NULL, '["fake"]', NULL,
                    '{}', ?, ?)""",
            (tid_pending, now, now, json.dumps(perms), json.dumps(limits)),
        )
    resp = client.post("/api/tasks/export-batch", json={"task_ids": [tid_pending]})
    body = resp.json()
    assert body["exported_count"] == 0
    assert body["skipped_count"] == 1
    assert body["skipped"][0]["task_id"] == tid_pending
