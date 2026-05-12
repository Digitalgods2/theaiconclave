"""Tests for the DB retention policy (decision 0003).

Covers:
- Tier 3 raw messages get trimmed when over budget
- Tier 1 task rows and Tier 2 final_results rows survive trimming
- Tasks newer than min_age_days are not trimmed
- Tasks that are parents of other tasks (live thread) are not trimmed
- Tasks with unresolved dissent and no user_decision are not trimmed
- Under-budget passes are no-ops
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.database import connect, init_database, now_iso
from app.services import agent_registry
from app.services.retention import (
    completed_task_count,
    db_size_bytes,
    find_trimmable_tasks,
    trim_to_budget,
)
from app.utils.ids import message_id, result_id, task_id as new_task_id


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_database(str(db_path))
        agent_registry.clear()
        agent_registry.init_registry()
        yield db_path


def _insert_task(
    *,
    age_days: int = 0,
    status: str = "completed",
    agreement_level: str | None = "consensus",
    user_decision: str | None = None,
    parent_task_id: str | None = None,
    with_messages: int = 3,
) -> str:
    """Insert a synthetic task + final_result + agent_messages for testing."""
    tid = new_task_id()
    created = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
            (id, created_at, updated_at, status, source, source_agent, mode, task_type,
             user_request, primary_agent, consultants, project_path,
             context_json, permissions_json, limits_json,
             user_decision, user_decided_at, parent_task_id)
            VALUES (?, ?, ?, ?, 'api', NULL, 'conclave', 'general_consultation',
                    'Test', NULL, '["fake"]', NULL,
                    '{}', '{}', '{}', ?, ?, ?)""",
            (
                tid, created, created, status,
                user_decision,
                now_iso() if user_decision else None,
                parent_task_id,
            ),
        )
        if agreement_level is not None:
            conn.execute(
                """INSERT INTO final_results
                (id, task_id, final_answer, agreement_level, created_at)
                VALUES (?, ?, 'Test final answer.', ?, ?)""",
                (result_id(), tid, agreement_level, created),
            )
        for i in range(with_messages):
            conn.execute(
                """INSERT INTO agent_messages
                (id, task_id, agent_run_id, agent_name, role, message_type,
                 direction, content, structured_json, created_at)
                VALUES (?, ?, NULL, 'fake', 'participant', 'conclave_turn',
                        'from_agent', ?, NULL, ?)""",
                (message_id(), tid, f"msg {i}", created),
            )
    return tid


def _message_count_for(task_id: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_messages WHERE task_id = ?", (task_id,)
        ).fetchone()
    return row["n"]


def _task_exists(task_id: str) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row is not None


def _final_result_exists(task_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM final_results WHERE task_id = ?", (task_id,)
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Under-budget pass is a no-op
# ---------------------------------------------------------------------------

def test_under_budget_no_op(temp_db):
    _insert_task(age_days=120)
    result = trim_to_budget(
        max_db_size_bytes=10 * 1024 * 1024,  # 10 MB — easily over
        max_task_count=100,
        min_age_days=90,
        db_path=temp_db,
    )
    assert result["ran"] is False
    assert "under budget" in result["reason"]


# ---------------------------------------------------------------------------
# Tier 3 messages deleted; Tier 1 (task) and Tier 2 (final_results) preserved
# ---------------------------------------------------------------------------

def test_trim_deletes_only_tier3(temp_db):
    tid = _insert_task(age_days=120, agreement_level="consensus")
    assert _message_count_for(tid) == 3

    # Force trigger by setting absurdly low task-count budget (0).
    result = trim_to_budget(
        max_db_size_bytes=1,            # 1 byte — guaranteed over
        max_task_count=0,                # 0 — also guaranteed over
        min_age_days=90,
        db_path=temp_db,
    )
    assert result["ran"] is True
    assert tid in result["trimmed_task_ids"]
    assert _message_count_for(tid) == 0     # Tier 3 deleted
    assert _task_exists(tid)                # Tier 1 preserved
    assert _final_result_exists(tid)        # Tier 2 preserved


# ---------------------------------------------------------------------------
# Tasks newer than min_age_days are not trimmed
# ---------------------------------------------------------------------------

def test_min_age_protects_recent_tasks(temp_db):
    recent = _insert_task(age_days=30, agreement_level="consensus")
    old = _insert_task(age_days=200, agreement_level="consensus")

    eligible = find_trimmable_tasks(min_age_days=90)
    assert recent not in eligible
    assert old in eligible


# ---------------------------------------------------------------------------
# Tasks that are parents (live thread) are not trimmed
# ---------------------------------------------------------------------------

def test_parent_in_thread_not_trimmed(temp_db):
    parent = _insert_task(age_days=200, agreement_level="consensus")
    child = _insert_task(age_days=200, agreement_level="consensus", parent_task_id=parent)

    eligible = find_trimmable_tasks(min_age_days=90)
    assert parent not in eligible            # parent of a live child — protected
    assert child in eligible                 # leaf — trimmable


# ---------------------------------------------------------------------------
# Unresolved dissent without a decision is not trimmed
# ---------------------------------------------------------------------------

def test_unresolved_dissent_without_decision_protected(temp_db):
    unresolved = _insert_task(
        age_days=200,
        agreement_level="major_disagreement",
        user_decision=None,
    )
    minor_with_decision = _insert_task(
        age_days=200,
        agreement_level="minor_disagreement",
        user_decision="Glen's call.",
    )
    consensus = _insert_task(
        age_days=200,
        agreement_level="consensus",
    )

    eligible = find_trimmable_tasks(min_age_days=90)
    assert unresolved not in eligible            # Tier 1 (open question)
    assert minor_with_decision in eligible       # resolved by Glen's decision
    assert consensus in eligible                 # clean consensus


# ---------------------------------------------------------------------------
# Tasks without final_results (not summarized yet) are not trimmed
# ---------------------------------------------------------------------------

def test_unsummarized_tasks_not_trimmed(temp_db):
    tid = _insert_task(age_days=200, agreement_level=None)  # no final_result row

    eligible = find_trimmable_tasks(min_age_days=90)
    assert tid not in eligible


# ---------------------------------------------------------------------------
# Triggers reported correctly
# ---------------------------------------------------------------------------

def test_triggers_reported(temp_db):
    _insert_task(age_days=120, agreement_level="consensus")
    result = trim_to_budget(
        max_db_size_bytes=1,
        max_task_count=0,
        min_age_days=90,
        db_path=temp_db,
    )
    assert result["ran"] is True
    # Both triggers should fire given the tiny budgets.
    assert any("db_size" in t for t in result["triggers"])
    assert any("task_count" in t for t in result["triggers"])
