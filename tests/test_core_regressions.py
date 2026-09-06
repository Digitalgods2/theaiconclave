"""Focused regressions for task validation, cancellation, and adapter audit state."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.base import AdapterContext, AdapterTestResult, BaseAdapter
from app.database import connect, init_database, now_iso
from app.protocol.validators import AgentRole, PrimaryResponse, TaskRequest
from app.services import task_control
from app.services.orchestrator import _call_adapter_method, _load_prior_messages, run_task
from app.utils.ids import task_id as new_task_id


def _request(**overrides) -> TaskRequest:
    payload = {
        "protocol_version": "1.0", "source": "api", "mode": "consult",
        "task_type": "general_consultation", "user_request": "test",
        "primary_agent": "primary", "consultants": ["consultant"],
        "permissions": {
            "can_read_files": True, "can_write_files": False,
            "can_run_commands": False, "can_access_network": False,
            "can_install_packages": False, "can_apply_patches": False,
            "can_read_env_files": False, "can_read_secrets": False,
        },
        "limits": {"max_rounds": 3},
    }
    payload.update(overrides)
    return TaskRequest.model_validate(payload)


@pytest.mark.parametrize("overrides, message", [
    ({"consultants": ["consultant", "consultant"]}, "unique agent names"),
    ({"primary_agent": "consultant"}, "must not also appear"),
])
def test_task_request_rejects_duplicate_or_primary_consultant(overrides, message):
    with pytest.raises(ValueError, match=message):
        _request(**overrides)


class _TelemetryAdapter(BaseAdapter):
    name = "telemetry-test"

    async def is_available(self):
        return True

    async def test_connection(self):
        return AdapterTestResult(available=True, elapsed_ms=0)

    async def run_primary(self, ctx):
        return None

    async def run_consultant(self, ctx):
        return None

    async def run_final(self, ctx):
        return None

    async def run_conclave_turn(self, ctx):
        return None


@pytest.mark.asyncio
async def test_base_adapter_telemetry_is_context_local():
    adapter = _TelemetryAdapter()
    barrier = asyncio.Barrier(2)

    async def one(label):
        adapter._last_usage = {"label": label}
        adapter._last_tool_events = [{"label": label}]
        adapter._last_prompt = f"prompt-{label}"
        adapter._last_raw_response = f"raw-{label}"
        await barrier.wait()
        return (adapter._last_usage, adapter._last_tool_events,
                adapter._last_prompt, adapter._last_raw_response)

    first, second = await asyncio.gather(one("a"), one("b"))
    assert first == ({"label": "a"}, [{"label": "a"}], "prompt-a", "raw-a")
    assert second == ({"label": "b"}, [{"label": "b"}], "prompt-b", "raw-b")


@pytest.mark.asyncio
async def test_task_control_register_cancel_and_unregister_are_identity_safe():
    task_control.clear()
    old = asyncio.create_task(asyncio.sleep(10))
    new = asyncio.create_task(asyncio.sleep(10))
    try:
        task_control.register("same", old)
        assert task_control.is_active("same")
        assert task_control.cancel("same") is True
        await asyncio.sleep(0)
        assert old.cancelled()
        task_control.register("same", new)
        task_control.unregister("same", old)
        assert task_control.is_active("same")
        task_control.unregister("same", new)
        assert not task_control.is_active("same")
        assert task_control.cancel("missing") is False
    finally:
        for task in (old, new):
            if not task.done():
                task.cancel()
        await asyncio.gather(old, new, return_exceptions=True)
        task_control.clear()


def _insert_task() -> str:
    tid = new_task_id()
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
            (id, created_at, updated_at, status, source, mode, task_type,
             user_request, primary_agent, consultants, context_json,
             permissions_json, limits_json)
            VALUES (?, ?, ?, 'running', 'api', 'consult', 'general_consultation',
                    'test', 'primary', '[\"consultant\"]', '{}', ?, ?)""",
            (tid, now, now, json.dumps(_request().permissions.model_dump()),
             json.dumps(_request().limits.model_dump())),
        )
    return tid


class _AuditAdapter(_TelemetryAdapter):
    name = "audit-test"

    async def run_primary(self, ctx):
        self._last_prompt = "exact outbound prompt"
        self._last_raw_response = "exact raw agent response"
        return PrimaryResponse(
            protocol_version="1.0", task_id=ctx.task_id, agent=self.name,
            role="primary", message_type="primary_proposal", summary="s",
            analysis="a",
        )


def _ctx(tid: str) -> AdapterContext:
    task = _request()
    return AdapterContext(task=task, task_id=tid, prior_messages=[],
                          permissions=task.permissions, working_directory=".")


@pytest.mark.asyncio
async def test_orchestrator_persists_adapter_prompt_and_raw_and_does_not_reload_them(tmp_path):
    init_database(tmp_path / "test.db")
    tid = _insert_task()
    result, error = await _call_adapter_method(
        _AuditAdapter(), "run_primary", _ctx(tid), tid, AgentRole.PRIMARY, 1,
    )
    assert error is None and result is not None
    with connect() as conn:
        rows = conn.execute(
            "SELECT message_type, content FROM agent_messages WHERE task_id = ?",
            (tid,),
        ).fetchall()
    by_type = {row["message_type"]: row["content"] for row in rows}
    assert by_type["agent_prompt"] == "exact outbound prompt"
    assert by_type["agent_raw_response"] == "exact raw agent response"
    assert all(m["message_type"] not in {"agent_prompt", "agent_raw_response"}
               for m in _load_prior_messages(tid))


@pytest.mark.asyncio
async def test_cancelled_adapter_call_records_cancelled_run(tmp_path):
    init_database(tmp_path / "test.db")
    tid = _insert_task()

    class Waiting(_TelemetryAdapter):
        name = "waiting-test"

        async def run_primary(self, ctx):
            await asyncio.Future()

    call = asyncio.create_task(_call_adapter_method(
        Waiting(), "run_primary", _ctx(tid), tid, AgentRole.PRIMARY, 1,
    ))
    await asyncio.sleep(0)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    with connect() as conn:
        row = conn.execute("SELECT status FROM agent_runs WHERE task_id = ?", (tid,)).fetchone()
    assert row["status"] == "cancelled"


def test_cancel_endpoint_reports_whether_active_call_was_interrupted(tmp_path, monkeypatch):
    init_database(tmp_path / "test.db")
    tid = _insert_task()
    from app.api import tasks as tasks_api
    app = FastAPI()
    app.include_router(tasks_api.router)
    monkeypatch.setattr(task_control, "cancel", lambda task_id: True)
    with TestClient(app) as client:
        response = client.post(f"/api/tasks/{tid}/cancel")
    assert response.status_code == 200
    assert response.json()["interrupted_active_call"] is True


@pytest.mark.asyncio
async def test_legacy_invalid_task_row_is_marked_failed_not_stranded(tmp_path):
    init_database(tmp_path / "test.db")
    tid = new_task_id()
    now = now_iso()
    request = _request()
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
               (id, created_at, updated_at, status, source, mode, task_type,
                user_request, primary_agent, consultants, context_json,
                permissions_json, limits_json)
               VALUES (?, ?, ?, 'running', 'api', 'resolve', 'general_consultation',
                       'legacy invalid row', 'same', '["same"]', '{}', ?, ?)""",
            (tid, now, now, json.dumps(request.permissions.model_dump()),
             json.dumps(request.limits.model_dump())),
        )
    with pytest.raises(ValueError):
        await run_task(tid)
    with connect() as conn:
        row = conn.execute("SELECT status, error_message FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "failed"
    assert "must not also appear" in row["error_message"]
