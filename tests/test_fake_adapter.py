"""End-to-end test of the consult flow using the fake adapter.

Exercises the full pipeline:
- task creation in SQLite
- orchestrator dispatch to the fake adapter
- three-round consult flow (primary proposal, consultant critique, primary final)
- final result persistence with disagreements populated
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


def _create_pending_task(extra: dict | None = None) -> str:
    """Insert a pending task using the fake adapter as both primary and consultant."""
    tid = new_task_id()
    now = now_iso()
    permissions = Permissions(
        can_read_files=True,
        can_write_files=False,
        can_run_commands=False,
        can_access_network=False,
        can_install_packages=False,
        can_apply_patches=False,
        can_read_env_files=False,
        can_read_secrets=False,
    )
    limits = Limits(max_rounds=3, timeout_seconds=30)
    context = {"files": [], "error": None, "git_diff": None, "extra": extra or {}}

    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
            (id, created_at, updated_at, status, source, source_agent, mode, task_type,
             user_request, primary_agent, consultants, project_path,
             context_json, permissions_json, limits_json)
            VALUES (?, ?, ?, 'pending', 'api', NULL, 'consult', 'general_consultation',
                    'Test request', 'fake', '["fake"]', NULL, ?, ?, ?)""",
            (
                tid,
                now,
                now,
                json.dumps(context, sort_keys=True),
                json.dumps(permissions.model_dump(), sort_keys=True),
                json.dumps(limits.model_dump(), sort_keys=True),
            ),
        )
    return tid


async def test_consult_flow_end_to_end(temp_db):
    tid = _create_pending_task()
    await run_task(tid)

    with connect() as conn:
        task = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        result = conn.execute(
            "SELECT * FROM final_results WHERE task_id = ?", (tid,)
        ).fetchone()
        messages = conn.execute(
            "SELECT * FROM agent_messages WHERE task_id = ? ORDER BY created_at",
            (tid,),
        ).fetchall()
        runs = conn.execute(
            "SELECT * FROM agent_runs WHERE task_id = ? ORDER BY round_number, started_at",
            (tid,),
        ).fetchall()

    assert task["status"] == "completed"
    assert result is not None
    assert "Fake final answer" in result["final_answer"]

    # Three runs: primary proposal, consultant critique, primary final
    assert len(runs) == 3
    assert runs[0]["role"] == "primary"
    assert runs[1]["role"] == "consultant"
    assert runs[2]["role"] == "primary"

    # One agent_messages row per run
    assert len(messages) == 3

    # Default fake behavior produces "partial" agreement → at least one disagreement
    disagreements = json.loads(result["disagreements_json"])
    assert len(disagreements) >= 1
    assert disagreements[0]["topic"]
    assert disagreements[0]["primary_position"]
    assert disagreements[0]["consultant_position"]


async def test_primary_timeout_yields_failed_task(temp_db):
    tid = _create_pending_task(extra={"fake_behavior": "timeout"})
    await run_task(tid)

    with connect() as conn:
        task = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        result = conn.execute(
            "SELECT * FROM final_results WHERE task_id = ?", (tid,)
        ).fetchone()

    assert task["status"] == "failed"
    assert result is not None
    errors = json.loads(result["errors_json"])
    assert any(e["code"] == "agent_timeout" for e in errors)


async def test_consultant_agree_yields_no_disagreements(temp_db):
    tid = _create_pending_task(extra={"fake_behavior": "loop"})
    await run_task(tid)

    with connect() as conn:
        result = conn.execute(
            "SELECT * FROM final_results WHERE task_id = ?", (tid,)
        ).fetchone()

    assert result is not None
    assert result["agreement_level"] == "consensus"
    disagreements = json.loads(result["disagreements_json"])
    assert disagreements == []
