"""Tests for the inbox search query parameter (`/api/tasks?q=…`).

The server-side search is case-insensitive substring match across:
- task id
- user_request (the question text)
- user_decision
- the linked final_results.final_answer
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import init_database, connect, now_iso
from app.services import agent_registry
from app.utils.ids import task_id as new_task_id, result_id


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    init_database(str(tmp_path / "test.db"))
    agent_registry.clear()
    agent_registry.init_registry()
    from app.api import tasks as tasks_module
    app = FastAPI()
    app.include_router(tasks_module.router)
    return TestClient(app)


def _insert_task(
    *,
    user_request: str,
    user_decision: Optional[str] = None,
    final_answer: Optional[str] = None,
    status: str = "completed",
) -> str:
    tid = new_task_id()
    now = now_iso()
    perms = {"can_read_files": True, "can_write_files": False, "can_run_commands": False,
             "can_access_network": False, "can_install_packages": False,
             "can_apply_patches": False, "can_read_env_files": False, "can_read_secrets": False}
    limits = {"max_rounds": 5, "timeout_seconds": 180}
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
            (id, created_at, updated_at, status, source, mode, task_type,
             user_request, consultants, context_json, permissions_json, limits_json,
             user_decision)
            VALUES (?, ?, ?, ?, 'api', 'conclave', 'general_consultation',
                    ?, '["fake"]', '{}', ?, ?, ?)""",
            (tid, now, now, status, user_request, json.dumps(perms), json.dumps(limits), user_decision),
        )
        if final_answer is not None:
            conn.execute(
                """INSERT INTO final_results
                (id, task_id, final_answer, agreement_level, created_at)
                VALUES (?, ?, ?, 'consensus', ?)""",
                (result_id(), tid, final_answer, now),
            )
    return tid


def _ids(resp) -> set[str]:
    return {t["id"] for t in resp.json()["tasks"]}


def test_q_matches_user_request(client):
    a = _insert_task(user_request="How should we ship this overcooked release?")
    b = _insert_task(user_request="Unrelated planning question.")
    r = client.get("/api/tasks?q=overcooked")
    assert r.status_code == 200
    assert _ids(r) == {a}
    assert b not in _ids(r)


def test_q_matches_user_decision(client):
    a = _insert_task(user_request="Generic question", user_decision="We will roll over and try again later.")
    b = _insert_task(user_request="Another question", user_decision="Different verdict entirely.")
    r = client.get("/api/tasks?q=roll over")
    assert _ids(r) == {a}


def test_q_matches_final_answer_via_join(client):
    """Match against final_results.final_answer (different table, joined by task_id)."""
    a = _insert_task(user_request="Q", final_answer="The verdict crosses three independent reviewers.")
    b = _insert_task(user_request="Q", final_answer="Something else.")
    r = client.get("/api/tasks?q=crosses three")
    assert _ids(r) == {a}


def test_q_matches_task_id_substring(client):
    a = _insert_task(user_request="x")
    b = _insert_task(user_request="x")
    # Take the last 6 chars of `a` (the ulid) as the needle
    needle = a[-6:]
    r = client.get(f"/api/tasks?q={needle}")
    ids = _ids(r)
    assert a in ids
    assert b not in ids


def test_q_case_insensitive(client):
    a = _insert_task(user_request="Database PostgreSQL vs MongoDB tradeoffs")
    _ = _insert_task(user_request="Network reliability discussion")
    r_lower = client.get("/api/tasks?q=postgresql")
    r_upper = client.get("/api/tasks?q=POSTGRESQL")
    r_mixed = client.get("/api/tasks?q=PostGres")
    assert _ids(r_lower) == _ids(r_upper) == _ids(r_mixed) == {a}


def test_q_matches_in_any_field(client):
    """A single q value can hit any one of the four searchable surfaces."""
    a = _insert_task(user_request="zebra crossing question")
    b = _insert_task(user_request="other", user_decision="approved with zebra caveat")
    c = _insert_task(user_request="other", final_answer="The final word: zebra.")
    d = _insert_task(user_request="entirely off-topic")
    r = client.get("/api/tasks?q=zebra")
    assert _ids(r) == {a, b, c}
    assert d not in _ids(r)


def test_q_returns_empty_when_no_match(client):
    _insert_task(user_request="hello world")
    r = client.get("/api/tasks?q=thereisnomatch")
    assert r.json()["tasks"] == []


def test_q_combines_with_status_filter(client):
    _ = _insert_task(user_request="zebra", status="completed")
    b = _insert_task(user_request="zebra", status="failed")
    r = client.get("/api/tasks?q=zebra&status=failed")
    assert _ids(r) == {b}


def test_q_no_param_returns_all_recent(client):
    """No q parameter behaves as before — returns the most recent N tasks."""
    a = _insert_task(user_request="alpha")
    b = _insert_task(user_request="bravo")
    r = client.get("/api/tasks")
    assert {a, b}.issubset(_ids(r))


def test_q_handles_null_user_decision_safely(client):
    """user_decision is nullable. The LIKE against COALESCE(...) must not
    crash when the column is NULL."""
    a = _insert_task(user_request="match me", user_decision=None)
    r = client.get("/api/tasks?q=match me")
    assert _ids(r) == {a}


def test_q_handles_tasks_without_final_results(client):
    """final_results join is via subquery — tasks with no final_result row
    are still searchable on their other fields."""
    a = _insert_task(user_request="planning notes", final_answer=None)
    r = client.get("/api/tasks?q=planning")
    assert _ids(r) == {a}
