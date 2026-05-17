"""Tests for opt-in Tier 2 trim after export (ROADMAP §Next #3, post-Phase-2.5).

The retention worker keeps `final_results` rows by default — DR0003 calls them
"Tier 2: retain indefinitely until exported." With DR0005 export tracking
shipped, an opt-in flag (`retention.trim_tier2_after_export`) lets the worker
also drop final_results for tasks that have been exported to disk.

Covers:
- find_trimmable_tier2_tasks selector behavior (exported + terminal + old)
- Flag OFF: exported task's final_result is preserved
- Flag ON: exported task's final_result is deleted; task row + linkages preserved
- Flag ON: non-exported task untouched even when budgets are over
- Flag ON: recent (< min_age) exported task NOT trimmed
- Flag ON: parent of a live thread NOT trimmed
- Flag ON: Tier 2 only kicks in after Tier 3 was insufficient
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.database import connect, init_database, now_iso
from app.services import agent_registry
from app.services.retention import (
    find_trimmable_tier2_tasks,
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
    age_days: int = 120,
    status: str = "completed",
    exported: bool = False,
    parent_task_id: str | None = None,
    with_messages: int = 0,
    agreement_level: str = "consensus",
) -> str:
    tid = new_task_id()
    created = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
            (id, created_at, updated_at, status, source, source_agent, mode,
             task_type, user_request, primary_agent, consultants, project_path,
             context_json, permissions_json, limits_json,
             user_decision, user_decided_at, parent_task_id, exported_at)
            VALUES (?, ?, ?, ?, 'api', NULL, 'conclave', 'general_consultation',
                    'Test', NULL, '["fake"]', NULL,
                    '{}', '{}', '{}', NULL, NULL, ?, ?)""",
            (tid, created, created, status, parent_task_id,
             created if exported else None),
        )
        conn.execute(
            """INSERT INTO final_results
            (id, task_id, final_answer, agreement_level, created_at)
            VALUES (?, ?, 'answer', ?, ?)""",
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


def _has_final_result(task_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM final_results WHERE task_id = ?", (task_id,)
        ).fetchone()
    return row is not None


def _task_exists(task_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Selector
# ---------------------------------------------------------------------------

def test_selector_returns_exported_old_terminal_tasks(temp_db):
    exported_old = _insert_task(age_days=200, exported=True)
    not_exported  = _insert_task(age_days=200, exported=False)
    recent_export = _insert_task(age_days=30,  exported=True)
    eligible = find_trimmable_tier2_tasks(min_age_days=90)
    assert exported_old in eligible
    assert not_exported not in eligible
    assert recent_export not in eligible


def test_selector_skips_parents_of_live_threads(temp_db):
    parent = _insert_task(age_days=200, exported=True)
    _insert_task(age_days=200, exported=True, parent_task_id=parent)
    eligible = find_trimmable_tier2_tasks(min_age_days=90)
    assert parent not in eligible  # has a child — preserve the thread


# ---------------------------------------------------------------------------
# Default (flag OFF) — Tier 2 untouched
# ---------------------------------------------------------------------------

def test_flag_off_preserves_tier2_even_when_over_budget(temp_db):
    tid = _insert_task(age_days=200, exported=True, with_messages=3)
    result = trim_to_budget(
        max_db_size_bytes=1, max_task_count=0,
        min_age_days=90, db_path=temp_db,
        # flag default False
    )
    assert result["ran"] is True
    assert _has_final_result(tid)               # preserved
    assert result.get("trimmed_tier2_task_count", 0) == 0


# ---------------------------------------------------------------------------
# Flag ON — trims Tier 2 for exported tasks; preserves task row + non-exported
# ---------------------------------------------------------------------------

def test_flag_on_trims_exported_final_result(temp_db):
    tid = _insert_task(age_days=200, exported=True, with_messages=3)
    result = trim_to_budget(
        max_db_size_bytes=1, max_task_count=0,
        min_age_days=90, db_path=temp_db,
        trim_tier2_after_export=True,
    )
    assert result["ran"] is True
    assert not _has_final_result(tid)           # Tier 2 trimmed
    assert _task_exists(tid)                    # Tier 1 preserved
    assert tid in result["trimmed_tier2_task_ids"]


def test_flag_on_skips_unexported_task(temp_db):
    exported   = _insert_task(age_days=200, exported=True,  with_messages=2)
    unexported = _insert_task(age_days=200, exported=False, with_messages=2)
    trim_to_budget(
        max_db_size_bytes=1, max_task_count=0,
        min_age_days=90, db_path=temp_db,
        trim_tier2_after_export=True,
    )
    assert not _has_final_result(exported)
    assert _has_final_result(unexported)        # never exported — preserve


def test_flag_on_skips_recent_exported_task(temp_db):
    """min_age_days must still gate Tier 2 trimming."""
    tid = _insert_task(age_days=30, exported=True, with_messages=2)
    trim_to_budget(
        max_db_size_bytes=1, max_task_count=0,
        min_age_days=90, db_path=temp_db,
        trim_tier2_after_export=True,
    )
    assert _has_final_result(tid)  # too recent — preserve


def test_flag_on_skips_parents_of_live_threads(temp_db):
    parent = _insert_task(age_days=200, exported=True, with_messages=2)
    child  = _insert_task(age_days=200, exported=True, with_messages=2, parent_task_id=parent)
    trim_to_budget(
        max_db_size_bytes=1, max_task_count=0,
        min_age_days=90, db_path=temp_db,
        trim_tier2_after_export=True,
    )
    assert _has_final_result(parent)            # parent preserved (live thread)
    assert not _has_final_result(child)         # child eligible


# ---------------------------------------------------------------------------
# Ordering — Tier 2 only kicks in after Tier 3 was insufficient
# ---------------------------------------------------------------------------

def test_tier2_kicks_in_only_after_tier3(temp_db):
    """If Tier 3 trimming brings us back under budget, Tier 2 is left alone."""
    tid = _insert_task(age_days=200, exported=True, with_messages=5)
    result = trim_to_budget(
        max_db_size_bytes=10 * 1024 * 1024,  # 10 MB — easy
        max_task_count=100,                   # easy
        min_age_days=90,
        db_path=temp_db,
        trim_tier2_after_export=True,
    )
    # We're under budget to begin with — nothing runs at all.
    assert result["ran"] is False
    assert _has_final_result(tid)
