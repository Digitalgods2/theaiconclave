"""End-to-end tests for resolve mode.

Covers the new termination semantics: goal-based stopping, user-input pause + resume,
cannot-resolve, repetition guard, consultant-driven continuation.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app.database import connect, init_database, now_iso
from app.protocol.validators import Limits, Permissions
from app.services import agent_registry
from app.services.orchestrator import run_task
from app.utils.ids import task_id as new_task_id


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_database(str(db_path))
        agent_registry.clear()
        agent_registry.init_registry()
        yield db_path


def _create_resolve_task(extra: dict | None = None, max_seconds: int = 60, max_rounds: int = 20) -> str:
    tid = new_task_id()
    now = now_iso()
    permissions = Permissions(
        can_read_files=True, can_write_files=False, can_run_commands=False,
        can_access_network=False, can_install_packages=False,
        can_apply_patches=False, can_read_env_files=False, can_read_secrets=False,
    )
    limits = Limits(max_rounds=max_rounds, timeout_seconds=30, max_seconds=max_seconds)
    context = {"files": [], "error": None, "git_diff": None, "extra": extra or {}}

    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
            (id, created_at, updated_at, status, source, source_agent, mode, task_type,
             user_request, primary_agent, consultants, project_path,
             context_json, permissions_json, limits_json)
            VALUES (?, ?, ?, 'pending', 'api', NULL, 'resolve', 'general_consultation',
                    'Test resolve request', 'fake', '["fake"]', NULL, ?, ?, ?)""",
            (
                tid, now, now,
                json.dumps(context, sort_keys=True),
                json.dumps(permissions.model_dump(), sort_keys=True),
                json.dumps(limits.model_dump(), sort_keys=True),
            ),
        )
    return tid


def _task_status(tid: str) -> str:
    with connect() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    return row["status"]


def _final_result(tid: str):
    with connect() as conn:
        row = conn.execute("SELECT * FROM final_results WHERE task_id = ?", (tid,)).fetchone()
    return row


def _primary_round_count(tid: str) -> int:
    with connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM agent_runs
               WHERE task_id = ? AND role = 'primary'""",
            (tid,),
        ).fetchone()
    return row["n"]


# ---------------------------------------------------------------------------
# Happy path: primary returns RESOLVED on first call.
# ---------------------------------------------------------------------------

async def test_resolve_immediately(temp_db):
    tid = _create_resolve_task(extra={"fake_behavior": "resolve_immediately"})
    await run_task(tid)

    assert _task_status(tid) == "completed"
    result = _final_result(tid)
    assert result is not None
    assert "resolved immediately" in result["final_answer"]
    # Single primary call — resolve_immediately doesn't iterate.
    assert _primary_round_count(tid) == 1


# ---------------------------------------------------------------------------
# Multi-round: primary asks for another round, then resolves.
# ---------------------------------------------------------------------------

async def test_resolve_after_one_round(temp_db):
    tid = _create_resolve_task(extra={"fake_behavior": "resolve_after_one_round"})
    await run_task(tid)

    assert _task_status(tid) == "completed"
    # Two primary rounds: one NEEDS_MORE_ROUNDS, one RESOLVED.
    assert _primary_round_count(tid) == 2


# ---------------------------------------------------------------------------
# User-input pause + resume.
# ---------------------------------------------------------------------------

async def test_user_input_pause_and_resume(temp_db):
    tid = _create_resolve_task(extra={"fake_behavior": "ask_then_resolve"})
    await run_task(tid)

    # First run pauses for user input.
    assert _task_status(tid) == "awaiting_user_input"

    # Verify the synthetic user_input_request message exists.
    with connect() as conn:
        msg = conn.execute(
            """SELECT content FROM agent_messages
               WHERE task_id = ? AND message_type = 'user_input_request'""",
            (tid,),
        ).fetchone()
    assert msg is not None
    assert "error message" in msg["content"]

    # Simulate the user posting an answer (the API endpoint does this; we replicate inline).
    from app.utils.ids import message_id
    with connect() as conn:
        conn.execute(
            """INSERT INTO agent_messages
               (id, task_id, agent_run_id, agent_name, role, message_type,
                direction, content, structured_json, created_at)
               VALUES (?, ?, NULL, 'user', 'user', 'user_input_response',
                       'from_user', ?, NULL, ?)""",
            (message_id(), tid, "ConnectionRefusedError on port 5432.", now_iso()),
        )
        conn.execute(
            "UPDATE tasks SET status = 'pending', updated_at = ? WHERE id = ?",
            (now_iso(), tid),
        )

    # Resume by re-running the orchestrator.
    await run_task(tid)

    assert _task_status(tid) == "completed"
    result = _final_result(tid)
    assert result is not None
    assert "resolved with user input" in result["final_answer"]


# ---------------------------------------------------------------------------
# Cannot resolve.
# ---------------------------------------------------------------------------

async def test_cannot_resolve(temp_db):
    tid = _create_resolve_task(extra={"fake_behavior": "cannot_resolve"})
    await run_task(tid)

    assert _task_status(tid) == "completed"
    result = _final_result(tid)
    assert result is not None
    assert result["agreement_level"] == "unresolved"
    assert result["final_answer"].startswith("Cannot resolve.")


# ---------------------------------------------------------------------------
# Repetition guard fires.
# ---------------------------------------------------------------------------

async def test_loop_detection(temp_db):
    tid = _create_resolve_task(extra={"fake_behavior": "loop_forever"})
    await run_task(tid)

    # Status should still be completed (we wrote a final result), but with loop_detected error.
    result = _final_result(tid)
    assert result is not None
    errors = json.loads(result["errors_json"])
    assert any(e["code"] == "loop_detected" for e in errors)
    # Should NOT have run unbounded — primary was called twice (round 1 + round 2 detection).
    assert _primary_round_count(tid) <= 3


# ---------------------------------------------------------------------------
# Consultant-driven continuation: consultant blocks first round, accepts second.
# ---------------------------------------------------------------------------

async def test_consultant_drives_continuation(temp_db):
    tid = _create_resolve_task(extra={"fake_behavior": "consultant_blocks"})
    await run_task(tid)

    assert _task_status(tid) == "completed"
    # Two primary rounds: first one had wants_continuation=True, second was accepted.
    assert _primary_round_count(tid) == 2
