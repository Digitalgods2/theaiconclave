"""Tests for the orphan task reaper (Phase 1 of post-DR plan
tsk_01KRSW6AS3M66B4RRJE3JFAPRV).

Covers:
- No orphans: no-op, returns 0
- Old `running` task gets marked `failed` with reason + log entry
- Recently-updated `running` task is left alone
- Non-running tasks (pending, completed, awaiting_user_input) are left alone
- Transcript (agent_messages) is preserved verbatim
- Custom threshold is honored
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.database import connect, init_database, now_iso
from app.services.orphan_reaper import reap_orphans
from app.utils.ids import message_id, task_id as new_task_id


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_database(str(db_path))
        yield db_path


def _insert_task(*, status: str = "running", age_hours: float = 2.0,
                 with_messages: int = 0) -> str:
    """Insert a synthetic task whose updated_at is age_hours in the past."""
    tid = new_task_id()
    ts = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
            (id, created_at, updated_at, status, source, mode, task_type,
             user_request, consultants, context_json, permissions_json, limits_json)
            VALUES (?, ?, ?, ?, 'api', 'conclave', 'general_consultation',
                    'Test request', '["fake"]', '{}', '{}', '{}')""",
            (tid, ts, ts, status),
        )
        for i in range(with_messages):
            conn.execute(
                """INSERT INTO agent_messages
                (id, task_id, agent_name, role, message_type, direction, content, created_at)
                VALUES (?, ?, 'fake', 'participant', 'conclave_turn', 'from_agent',
                        ?, ?)""",
                (message_id(), tid, f"transcript line {i}", ts),
            )
    return tid


def _row(tid: str) -> dict:
    with connect() as conn:
        r = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        return dict(r) if r else {}


def _logs_for(tid: str) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM logs WHERE task_id = ?", (tid,)
        ).fetchall()]


def _messages_for(tid: str) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM agent_messages WHERE task_id = ?", (tid,)
        ).fetchall()]


def test_no_orphans_is_noop(temp_db):
    assert reap_orphans() == 0


def test_old_running_task_is_reaped(temp_db):
    tid = _insert_task(status="running", age_hours=2.0, with_messages=3)
    assert reap_orphans() == 1
    row = _row(tid)
    assert row["status"] == "failed"
    assert "orphaned" in (row["error_message"] or "")
    # Audit log entry was written
    logs = _logs_for(tid)
    assert len(logs) == 1
    assert logs[0]["event_type"] == "task_orphaned"
    assert logs[0]["level"] == "warn"


def test_transcript_is_preserved(temp_db):
    tid = _insert_task(status="running", age_hours=3.0, with_messages=5)
    reap_orphans()
    msgs = _messages_for(tid)
    assert len(msgs) == 5  # Nothing touched


def test_recent_running_task_is_left_alone(temp_db):
    tid = _insert_task(status="running", age_hours=0.1)  # 6 min ago
    assert reap_orphans() == 0
    assert _row(tid)["status"] == "running"
    assert _logs_for(tid) == []


def test_non_running_statuses_are_left_alone(temp_db):
    for status in ("pending", "completed", "failed", "cancelled", "awaiting_user_input"):
        tid = _insert_task(status=status, age_hours=24.0)
        reap_orphans()
        assert _row(tid)["status"] == status, (
            f"reaper touched a {status} task — should only touch 'running'"
        )


def test_custom_threshold(temp_db):
    # 30 minutes old; with the default 1h threshold this would survive…
    tid = _insert_task(status="running", age_hours=0.5)
    assert reap_orphans(threshold_hours=1.0) == 0
    # …but with a 15-minute threshold it should be reaped.
    assert reap_orphans(threshold_hours=0.25) == 1
    assert _row(tid)["status"] == "failed"


def test_multiple_orphans_are_all_reaped(temp_db):
    ids = [_insert_task(status="running", age_hours=h) for h in (2.0, 3.0, 5.0)]
    fresh = _insert_task(status="running", age_hours=0.1)
    pending = _insert_task(status="pending", age_hours=10.0)
    assert reap_orphans() == 3
    for tid in ids:
        assert _row(tid)["status"] == "failed"
    assert _row(fresh)["status"] == "running"
    assert _row(pending)["status"] == "pending"
