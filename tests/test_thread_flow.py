"""Tests for task threading via parent_task_id.

Covers: schema migration applies, ancestry walk works, prompt includes the
Prior Thread Context section when ancestors exist, depth cap and cycle guard.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app.database import connect, init_database, now_iso
from app.protocol.validators import Limits, Permissions
from app.services import agent_registry
from app.services.orchestrator import _load_thread_ancestors, run_task
from app.services.prompt_builder import build_primary_prompt
from app.utils.ids import task_id as new_task_id


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_database(str(db_path))
        agent_registry.clear()
        agent_registry.init_registry()
        yield db_path


def _insert_task(parent_task_id: str | None, behavior: str = "resolve_immediately") -> str:
    tid = new_task_id()
    now = now_iso()
    permissions = Permissions(
        can_read_files=True, can_write_files=False, can_run_commands=False,
        can_access_network=False, can_install_packages=False,
        can_apply_patches=False, can_read_env_files=False, can_read_secrets=False,
    )
    limits = Limits(max_rounds=3, timeout_seconds=30, max_seconds=60)
    context = {"files": [], "error": None, "git_diff": None, "extra": {"fake_behavior": behavior}}
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
            (id, created_at, updated_at, status, source, source_agent, mode, task_type,
             user_request, primary_agent, consultants, project_path,
             context_json, permissions_json, limits_json, parent_task_id)
            VALUES (?, ?, ?, 'pending', 'api', NULL, 'resolve', 'general_consultation',
                    'Test request', 'fake', '[]', NULL, ?, ?, ?, ?)""",
            (
                tid, now, now,
                json.dumps(context, sort_keys=True),
                json.dumps(permissions.model_dump(), sort_keys=True),
                json.dumps(limits.model_dump(), sort_keys=True),
                parent_task_id,
            ),
        )
    return tid


def _record_decision(tid: str, decision: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE tasks SET user_decision = ?, user_decided_at = ? WHERE id = ?",
            (decision, now_iso(), tid),
        )


# ---------------------------------------------------------------------------
# Schema: parent_task_id column exists after migration
# ---------------------------------------------------------------------------

def test_parent_task_id_column_present(temp_db):
    with connect() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "parent_task_id" in cols


# ---------------------------------------------------------------------------
# Ancestry walk: empty when no parent
# ---------------------------------------------------------------------------

def test_no_ancestors_when_no_parent(temp_db):
    tid = _insert_task(parent_task_id=None)
    chain = _load_thread_ancestors(tid)
    assert chain == []


# ---------------------------------------------------------------------------
# Ancestry walk: linear chain of three
# ---------------------------------------------------------------------------

def test_linear_ancestry_oldest_first(temp_db):
    grandparent = _insert_task(parent_task_id=None)
    _record_decision(grandparent, "Grandparent's decision.")
    parent = _insert_task(parent_task_id=grandparent)
    _record_decision(parent, "Parent's decision.")
    child = _insert_task(parent_task_id=parent)

    chain = _load_thread_ancestors(child)
    # Oldest first: grandparent, then parent. Child itself is not included.
    assert [a["id"] for a in chain] == [grandparent, parent]
    assert chain[0]["user_decision"] == "Grandparent's decision."
    assert chain[1]["user_decision"] == "Parent's decision."


# ---------------------------------------------------------------------------
# Depth cap: chain exceeding _MAX_ANCESTRY_DEPTH is truncated
# ---------------------------------------------------------------------------

def test_depth_cap(temp_db):
    # Build a chain of 8 tasks: t0 → t1 → ... → t7
    ids = []
    parent = None
    for i in range(8):
        tid = _insert_task(parent_task_id=parent)
        ids.append(tid)
        parent = tid
    # _MAX_ANCESTRY_DEPTH is 5; chain from the last task should yield 5 ancestors.
    chain = _load_thread_ancestors(ids[-1])
    assert len(chain) == 5
    # The 5 returned should be the most recent ancestors (closest to the leaf),
    # ordered oldest-first within that window.
    # The leaf is ids[7]; its parent is ids[6], grandparent ids[5], etc.
    # Walking up 5 from the leaf hits ids[6], ids[5], ids[4], ids[3], ids[2].
    # Reversed to oldest-first: ids[2], ids[3], ids[4], ids[5], ids[6].
    assert [a["id"] for a in chain] == [ids[2], ids[3], ids[4], ids[5], ids[6]]


# ---------------------------------------------------------------------------
# Cycle guard: pathological parent loop is broken safely
# ---------------------------------------------------------------------------

def test_cycle_guard(temp_db):
    a = _insert_task(parent_task_id=None)
    b = _insert_task(parent_task_id=a)
    # Force a cycle: make a's parent point to b. Pathological but defensive.
    with connect() as conn:
        conn.execute("UPDATE tasks SET parent_task_id = ? WHERE id = ?", (b, a))
    chain = _load_thread_ancestors(b)
    # We walk a, then a's parent is b (already visited) — break.
    assert len(chain) <= 2
    ids = {a_["id"] for a_ in chain}
    assert ids.issubset({a, b})


# ---------------------------------------------------------------------------
# Prompt: Prior Thread Context section appears when ancestors exist
# ---------------------------------------------------------------------------

async def test_prompt_includes_thread_context(temp_db):
    parent = _insert_task(parent_task_id=None)
    _record_decision(parent, "Use PostgreSQL.")
    # Insert a final_result for the parent so the section can include it
    with connect() as conn:
        from app.utils.ids import result_id
        conn.execute(
            """INSERT INTO final_results
            (id, task_id, final_answer, agreement_level, created_at)
            VALUES (?, ?, ?, 'consensus', ?)""",
            (result_id(), parent, "Recommend PostgreSQL with managed instance.", now_iso()),
        )

    child = _insert_task(parent_task_id=parent)
    # Run the child task — the orchestrator should load the ancestor and
    # the prompt builder should embed the Prior Thread Context section.
    await run_task(child)

    # Verify by re-loading the child task and rebuilding its prompt
    from app.services.orchestrator import _load_task
    child_task = _load_task(child)
    assert child_task is not None
    # Attach ancestors as the orchestrator would
    child_task.context.extra["thread_ancestors"] = _load_thread_ancestors(child)
    prompt = build_primary_prompt(child_task, child, "fake", [])
    assert "Prior Thread Context" in prompt
    assert "PostgreSQL" in prompt  # parent's final_answer made it into the prompt
    assert "Use PostgreSQL." in prompt  # parent's decision made it into the prompt
