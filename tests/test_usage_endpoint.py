"""Tests for GET /api/tasks/usage — the Usage & Spend dashboard endpoint."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import connect, init_database, now_iso
from app.services import agent_registry
from app.utils.ids import task_id as new_task_id


@pytest.fixture
def client(tmp_path: Path):
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


def _insert_task() -> str:
    tid = new_task_id()
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
               (id, created_at, updated_at, status, source, source_agent, mode,
                task_type, user_request, primary_agent, consultants, project_path,
                context_json, permissions_json, limits_json)
               VALUES (?, ?, ?, 'completed', 'api', NULL, 'conclave', 'general_consultation',
                       'Test', NULL, '["fake"]', NULL, '{}', ?, ?)""",
            (tid, now, now, json.dumps(_PERMS), json.dumps(_LIMITS)),
        )
    return tid


def _insert_run(task_id: str, agent_name: str, *, started_at: str,
                cost_usd: float | None = None,
                input_tokens: int = 0, output_tokens: int = 0) -> None:
    rid = f"run_{uuid.uuid4().hex[:16]}"
    with connect() as conn:
        conn.execute(
            """INSERT INTO agent_runs
               (id, task_id, agent_name, role, round_number, started_at, status,
                input_tokens, output_tokens, cost_usd)
               VALUES (?, ?, ?, 'conclave', 1, ?, 'completed', ?, ?, ?)""",
            (rid, task_id, agent_name, started_at,
             input_tokens, output_tokens, cost_usd),
        )


def test_usage_returns_all_expected_keys(client):
    resp = client.get("/api/tasks/usage")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("today_by_agent", "last7d_by_agent", "daily_7d", "all_time"):
        assert key in body, f"missing key {key!r}: {list(body)}"


def test_last7d_by_agent_groups_across_the_window(client):
    """Same agent firing on two different days inside the 7-day window appears
    as one summed row in last7d_by_agent."""
    tid = _insert_task()
    # Two deepseek runs, one today, one two days back. Both inside the 7-day window.
    _insert_run(tid, "deepseek", started_at=now_iso(), cost_usd=0.25,
                input_tokens=1000, output_tokens=500)
    _insert_run(tid, "deepseek",
                started_at=now_iso().replace("T", "T", 1)[:10] + "T12:00:00+00:00",
                cost_usd=0.10, input_tokens=500, output_tokens=250)

    body = client.get("/api/tasks/usage").json()
    rows = {r["agent_name"]: r for r in body["last7d_by_agent"]}
    assert "deepseek" in rows
    # Sums across the window — the two runs collapse to one row.
    assert rows["deepseek"]["run_count"] == 2
    assert rows["deepseek"]["cost_usd"] == pytest.approx(0.35)
    assert rows["deepseek"]["input_tokens"] == 1500
    assert rows["deepseek"]["output_tokens"] == 750


def test_last7d_by_agent_includes_agents_with_no_cost(client):
    """A CLI-seat run with cost_usd=NULL still contributes token counts and
    a row to last7d_by_agent — only the Spend column reads as zero."""
    tid = _insert_task()
    _insert_run(tid, "gemini", started_at=now_iso(), cost_usd=None,
                input_tokens=2000, output_tokens=300)

    body = client.get("/api/tasks/usage").json()
    rows = {r["agent_name"]: r for r in body["last7d_by_agent"]}
    assert "gemini" in rows
    assert rows["gemini"]["run_count"] == 1
    assert rows["gemini"]["cost_usd"] == 0
    assert rows["gemini"]["input_tokens"] == 2000


def test_today_and_last7d_can_differ(client):
    """The whole point: when today's tasks didn't include the OpenRouter
    seats but earlier-in-the-week tasks did, today_by_agent and
    last7d_by_agent show different rosters."""
    tid = _insert_task()
    # Today: only CLI seats ran.
    _insert_run(tid, "codex", started_at=now_iso(), cost_usd=None,
                input_tokens=5000, output_tokens=400)
    # 3 days back: an OpenRouter seat fired.
    from datetime import datetime, timedelta, timezone
    three_days_back = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    _insert_run(tid, "deepseek", started_at=three_days_back, cost_usd=0.50,
                input_tokens=8000, output_tokens=1500)

    body = client.get("/api/tasks/usage").json()
    today_agents = {r["agent_name"] for r in body["today_by_agent"]}
    week_agents = {r["agent_name"] for r in body["last7d_by_agent"]}
    assert "codex" in today_agents
    assert "deepseek" not in today_agents       # not today
    assert "deepseek" in week_agents             # but inside the 7-day window
    assert "codex" in week_agents                # both views reach today


def test_empty_db_returns_empty_per_agent_lists(client):
    body = client.get("/api/tasks/usage").json()
    assert body["today_by_agent"] == []
    assert body["last7d_by_agent"] == []
