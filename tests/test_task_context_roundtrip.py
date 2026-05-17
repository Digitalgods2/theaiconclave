"""Regression test for `task.context` round-tripping through GET /api/tasks/{id}.

The dashboard's "Continue thread (new task)" button reads
`task.context.extra.include_sandbox` to inherit the sandbox-checkbox state
from the parent task. The API endpoint historically omitted `context` from
its response shape, which silently dropped the inheritance and produced
follow-up tasks with no sandbox access (no tool-loop, no project files
inlined for OpenRouter seats). This locks the round-trip in.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app.api.tasks import get_task
from app.database import connect, init_database


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_database(str(db_path))
        yield db_path


def _insert_task(context_extra: dict) -> str:
    tid = "tsk_ctx_test"
    ctx_json = json.dumps({"extra": context_extra}, sort_keys=True)
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
            (id, created_at, updated_at, status, source, mode, task_type,
             user_request, consultants, context_json, permissions_json, limits_json)
            VALUES (?, '2026-05-17T00:00:00+00:00', '2026-05-17T00:00:00+00:00',
                    'completed', 'api', 'consult', 'general_consultation',
                    'Q', '["fake"]', ?, '{}', '{}')""",
            (tid, ctx_json),
        )
    return tid


@pytest.mark.asyncio
async def test_context_present_in_response(temp_db):
    tid = _insert_task({"include_sandbox": True})
    r = await get_task(tid)
    assert "context" in r["task"], (
        "The dashboard's Continue-thread inherits sandbox state from "
        "task.context.extra — the API must return it."
    )


@pytest.mark.asyncio
async def test_include_sandbox_survives_roundtrip(temp_db):
    tid = _insert_task({"include_sandbox": True})
    r = await get_task(tid)
    assert r["task"]["context"]["extra"]["include_sandbox"] is True


@pytest.mark.asyncio
async def test_include_sandbox_false_also_survives(temp_db):
    tid = _insert_task({"include_sandbox": False})
    r = await get_task(tid)
    assert r["task"]["context"]["extra"]["include_sandbox"] is False


@pytest.mark.asyncio
async def test_extra_with_arbitrary_keys_round_trips(temp_db):
    tid = _insert_task({
        "include_sandbox": True,
        "attachments": [{"file_id": "fil_x", "filename": "y.md"}],
        "thread_ancestors": [{"id": "tsk_parent", "mode": "conclave"}],
    })
    r = await get_task(tid)
    extra = r["task"]["context"]["extra"]
    assert extra["include_sandbox"] is True
    assert extra["attachments"][0]["file_id"] == "fil_x"
    assert extra["thread_ancestors"][0]["id"] == "tsk_parent"


@pytest.mark.asyncio
async def test_malformed_context_json_does_not_500(temp_db):
    """Defensive: a row with corrupt context_json must not crash the
    endpoint — the helper returns {} instead."""
    tid = "tsk_bad_ctx"
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
            (id, created_at, updated_at, status, source, mode, task_type,
             user_request, consultants, context_json, permissions_json, limits_json)
            VALUES (?, '2026-05-17T00:00:00+00:00', '2026-05-17T00:00:00+00:00',
                    'completed', 'api', 'conclave', 'general_consultation',
                    'Q', '["fake"]', 'NOT-VALID-JSON', '{}', '{}')""",
            (tid,),
        )
    r = await get_task(tid)
    assert r["task"]["context"] == {}
