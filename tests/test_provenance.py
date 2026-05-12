"""Tests for source_agent provenance plumbing.

Verifies that when a task is created with a specific source_agent value, that
value round-trips through both the detail view and the list view. Also tests
the --invoked-by flag parser in the CLI client.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.database import init_database, connect, now_iso
from app.services import agent_registry
from app.utils.ids import task_id as new_task_id, result_id


@pytest.fixture
def client(tmp_path: Path):
    db_path = tmp_path / "test.db"
    init_database(str(db_path))
    agent_registry.clear()
    agent_registry.init_registry()

    from app.api import tasks as tasks_module
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(tasks_module.router)
    return TestClient(app)


def _insert_task(source: str, source_agent: str | None, status: str = "completed") -> str:
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
            VALUES (?, ?, ?, ?, ?, ?, 'conclave', 'general_consultation',
                    'Test', NULL, '["fake"]', NULL,
                    '{}', ?, ?)""",
            (tid, now, now, status, source, source_agent,
             json.dumps(perms), json.dumps(limits)),
        )
    return tid


# ---------------------------------------------------------------------------
# Detail view (GET /api/tasks/{id}) returns source + source_agent
# ---------------------------------------------------------------------------

def test_detail_view_returns_source_agent_codex(client):
    tid = _insert_task(source="cli", source_agent="codex")
    resp = client.get(f"/api/tasks/{tid}")
    assert resp.status_code == 200
    task = resp.json()["task"]
    assert task["source"] == "cli"
    assert task["source_agent"] == "codex"


def test_detail_view_returns_source_agent_gemini(client):
    tid = _insert_task(source="cli", source_agent="gemini")
    resp = client.get(f"/api/tasks/{tid}")
    task = resp.json()["task"]
    assert task["source_agent"] == "gemini"


def test_detail_view_returns_source_agent_claude_code(client):
    tid = _insert_task(source="cli", source_agent="claude-code")
    resp = client.get(f"/api/tasks/{tid}")
    task = resp.json()["task"]
    assert task["source_agent"] == "claude-code"


def test_detail_view_returns_null_source_agent(client):
    """Legacy tasks without source_agent should round-trip a None value."""
    tid = _insert_task(source="api", source_agent=None)
    resp = client.get(f"/api/tasks/{tid}")
    task = resp.json()["task"]
    assert task["source"] == "api"
    assert task["source_agent"] is None


# ---------------------------------------------------------------------------
# List view (GET /api/tasks) returns source + source_agent
# ---------------------------------------------------------------------------

def test_list_view_returns_source_agent(client):
    tid_codex = _insert_task(source="cli", source_agent="codex")
    tid_gemini = _insert_task(source="cli", source_agent="gemini")
    resp = client.get("/api/tasks?limit=10")
    by_id = {t["id"]: t for t in resp.json()["tasks"]}
    assert by_id[tid_codex]["source_agent"] == "codex"
    assert by_id[tid_gemini]["source_agent"] == "gemini"
    assert by_id[tid_codex]["source"] == "cli"


# ---------------------------------------------------------------------------
# CLI client --invoked-by flag parser
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def cli_module():
    """Load switchboard.py as a module so we can unit-test the flag parser."""
    sb_path = Path("C:/Users/gosmo/.claude/skills/switchboard-conclave/switchboard.py")
    if not sb_path.exists():
        pytest.skip(f"switchboard.py not installed at {sb_path}")
    spec = importlib.util.spec_from_file_location("switchboard_cli", sb_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pop_invoked_by_space_form(cli_module, monkeypatch):
    monkeypatch.delenv("SWITCHBOARD_INVOKED_BY", raising=False)
    argv = ["switchboard.py", "--invoked-by", "codex", "run", "conclave", "a,b", "q"]
    val = cli_module._pop_invoked_by(argv)
    assert val == "codex"
    assert argv == ["switchboard.py", "run", "conclave", "a,b", "q"]


def test_pop_invoked_by_equals_form(cli_module, monkeypatch):
    monkeypatch.delenv("SWITCHBOARD_INVOKED_BY", raising=False)
    argv = ["switchboard.py", "--invoked-by=gemini", "decide", "latest", "ratified"]
    val = cli_module._pop_invoked_by(argv)
    assert val == "gemini"
    assert argv == ["switchboard.py", "decide", "latest", "ratified"]


def test_pop_invoked_by_env_fallback(cli_module, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_INVOKED_BY", "codex")
    argv = ["switchboard.py", "run", "conclave", "a", "q"]
    val = cli_module._pop_invoked_by(argv)
    assert val == "codex"
    # argv unchanged when no flag was present
    assert argv == ["switchboard.py", "run", "conclave", "a", "q"]


def test_pop_invoked_by_default(cli_module, monkeypatch):
    monkeypatch.delenv("SWITCHBOARD_INVOKED_BY", raising=False)
    argv = ["switchboard.py", "run", "conclave", "a", "q"]
    val = cli_module._pop_invoked_by(argv)
    assert val == "claude-code"  # back-compat default


def test_pop_invoked_by_flag_wins_over_env(cli_module, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_INVOKED_BY", "gemini")
    argv = ["switchboard.py", "--invoked-by", "codex", "run", "conclave", "a", "q"]
    val = cli_module._pop_invoked_by(argv)
    assert val == "codex"
