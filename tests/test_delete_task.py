"""Tests for DELETE /api/tasks/{id} — permanent hard delete of a task.

Covers:
- the task row and all FK-cascaded child rows (agent_runs, agent_messages,
  final_results, approvals, task_artifacts) are removed
- the on-disk per-task sandbox / artifact directories are removed
- a threaded child task's parent_task_id is cleared, not left dangling
- a deleted task no longer appears in GET /api/tasks
- 404 for an unknown task, 409 for a task still in flight
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import connect, init_database, now_iso
from app.services import agent_registry
from app.utils.ids import task_id as new_task_id
from app.utils.paths import artifacts_root, sandboxes_root


@pytest.fixture
def client(tmp_path: Path):
    """TestClient over a fresh DB with only the tasks router mounted."""
    init_database(str(tmp_path / "test.db"))
    agent_registry.clear()
    agent_registry.init_registry()

    from app.api import tasks as tasks_module

    app = FastAPI()
    app.include_router(tasks_module.router)
    return TestClient(app)


_PERMS = {
    "can_read_files": True, "can_write_files": False, "can_run_commands": False,
    "can_access_network": False, "can_install_packages": False,
    "can_apply_patches": False, "can_read_env_files": False, "can_read_secrets": False,
}
_LIMITS = {"max_rounds": 5, "timeout_seconds": 180}


def _insert_task(status: str = "completed", parent_task_id: str | None = None) -> str:
    """Insert a bare task row in the given status."""
    tid = new_task_id()
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
               (id, created_at, updated_at, status, source, source_agent, mode,
                task_type, user_request, primary_agent, consultants, project_path,
                context_json, permissions_json, limits_json, parent_task_id)
               VALUES (?, ?, ?, ?, 'api', NULL, 'conclave', 'general_consultation',
                       'Test', NULL, '["fake"]', NULL, '{}', ?, ?, ?)""",
            (tid, now, now, status, json.dumps(_PERMS), json.dumps(_LIMITS), parent_task_id),
        )
    return tid


def _attach_children(tid: str) -> None:
    """Give a task one row in each FK-cascaded child table."""
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """INSERT INTO agent_runs
               (id, task_id, agent_name, role, round_number, started_at, status)
               VALUES (?, ?, 'fake', 'conclave', 1, ?, 'completed')""",
            (f"run_{tid}", tid, now),
        )
        conn.execute(
            """INSERT INTO agent_messages
               (id, task_id, agent_run_id, agent_name, role, message_type,
                direction, content, created_at)
               VALUES (?, ?, ?, 'fake', 'conclave', 'conclave_turn', 'inbound',
                       'hi', ?)""",
            (f"msg_{tid}", tid, f"run_{tid}", now),
        )
        conn.execute(
            """INSERT INTO final_results
               (id, task_id, final_answer, agreement_level, created_at)
               VALUES (?, ?, 'answer', 'consensus', ?)""",
            (f"res_{tid}", tid, now),
        )
        conn.execute(
            """INSERT INTO approvals
               (id, task_id, approval_type, description, payload_json, status, created_at)
               VALUES (?, ?, 'command', 'do x', '{}', 'pending', ?)""",
            (f"apr_{tid}", tid, now),
        )
        conn.execute(
            """INSERT INTO task_artifacts
               (id, task_id, created_at, updated_at, kind, title, filename,
                mime_type, size_bytes, storage_path, metadata_json)
               VALUES (?, ?, ?, ?, 'file', 'draft', 'd.txt', 'text/plain', 3, ?, '{}')""",
            (f"art_{tid}", tid, now, now, f"artifacts/{tid}/art/d.txt"),
        )


def _child_counts(tid: str) -> dict[str, int]:
    with connect() as conn:
        return {
            tbl: conn.execute(
                f"SELECT COUNT(*) AS n FROM {tbl} WHERE task_id = ?", (tid,)
            ).fetchone()["n"]
            for tbl in ("agent_runs", "agent_messages", "final_results",
                        "approvals", "task_artifacts")
        }


def test_delete_removes_task_and_cascades(client):
    tid = _insert_task("completed")
    _attach_children(tid)
    assert all(v == 1 for v in _child_counts(tid).values())

    resp = client.delete(f"/api/tasks/{tid}")
    assert resp.status_code == 200
    assert resp.json() == {"task_id": tid, "status": "deleted"}

    with connect() as conn:
        assert conn.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone() is None
    assert all(v == 0 for v in _child_counts(tid).values())


def test_delete_unknown_task_returns_404(client):
    resp = client.delete("/api/tasks/tsk_does_not_exist")
    assert resp.status_code == 404


@pytest.mark.parametrize("status", ["pending", "running", "awaiting_user_input", "waiting_for_user"])
def test_delete_in_flight_task_returns_409(client, status):
    tid = _insert_task(status)
    resp = client.delete(f"/api/tasks/{tid}")
    assert resp.status_code == 409
    # The task must still be there — a refused delete changes nothing.
    with connect() as conn:
        assert conn.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone() is not None


def test_delete_clears_child_parent_pointer(client):
    parent = _insert_task("completed")
    child = _insert_task("completed", parent_task_id=parent)

    resp = client.delete(f"/api/tasks/{parent}")
    assert resp.status_code == 200

    with connect() as conn:
        row = conn.execute(
            "SELECT parent_task_id FROM tasks WHERE id = ?", (child,)
        ).fetchone()
    # The child survives; its dangling parent pointer is cleared.
    assert row is not None
    assert row["parent_task_id"] is None


def test_delete_removes_on_disk_dirs(client):
    tid = _insert_task("completed")
    sandbox_dir = sandboxes_root() / tid
    artifact_dir = artifacts_root() / tid
    sandbox_dir.mkdir(parents=True)
    artifact_dir.mkdir(parents=True)
    (sandbox_dir / "copy.py").write_text("x = 1", encoding="utf-8")
    (artifact_dir / "draft.txt").write_text("draft", encoding="utf-8")

    resp = client.delete(f"/api/tasks/{tid}")
    assert resp.status_code == 200
    assert not sandbox_dir.exists()
    assert not artifact_dir.exists()


def test_deleted_task_absent_from_listing(client):
    keep = _insert_task("completed")
    drop = _insert_task("completed")

    client.delete(f"/api/tasks/{drop}")

    listed = {t["id"] for t in client.get("/api/tasks").json()["tasks"]}
    assert keep in listed
    assert drop not in listed
