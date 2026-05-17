"""Regression test for DR0015 tool-event persistence (the AttributeError fix).

The original implementation passed the raw string message_type ("tool_call" /
"tool_result") from adapter._last_tool_events directly to _record_message,
which calls `.value` on it. That crashed deepseek's turn on
tsk_01KRVDYBG6A6MSBX84D1KNJCQ6 with `'str' object has no attribute 'value'`,
which the orchestrator surfaced as `agent_error: Unexpected adapter error`,
and discarded deepseek's actual critique output.

Catches the regression by driving _call_adapter_method with a stub adapter
that populates _last_tool_events and asserting the rows land in the DB.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.database import connect, init_database
from app.protocol.validators import (
    AgentRole,
    ConclaveConvergence,
    ConclaveTurn,
    MessageType,
)
from app.services.orchestrator import _call_adapter_method


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_database(str(db_path))
        # Need at least one tasks row for the FK constraint on agent_messages.
        with connect() as conn:
            conn.execute(
                """INSERT INTO tasks
                (id, created_at, updated_at, status, source, mode, task_type,
                 user_request, consultants, context_json, permissions_json, limits_json)
                VALUES ('tsk_test', '2026-05-17T00:00:00+00:00',
                        '2026-05-17T00:00:00+00:00', 'running', 'api',
                        'conclave', 'general_consultation',
                        'Q', '["stub"]', '{}', '{}', '{}')""",
            )
        yield db_path


class _StubAdapter:
    """Pretends to be an adapter — emits tool events + a structured turn
    without doing any actual HTTP work."""
    name = "stub"

    def __init__(self, tool_events: list[dict]):
        self._scripted_events = tool_events
        self._last_tool_events: list[dict] = []
        self._last_usage: dict = {}

    async def run_conclave_turn(self, ctx) -> ConclaveTurn:
        # Mimic what the OpenRouterAdapter does: accumulate events during
        # the call, then return the structured turn.
        self._last_tool_events = list(self._scripted_events)
        self._last_usage = {"input_tokens": 100, "output_tokens": 50}
        return ConclaveTurn(
            protocol_version="1.0",
            task_id=ctx.task_id,
            agent="stub",
            role="participant",
            message_type=MessageType.CONCLAVE_TURN,
            position="p",
            summary="s",
            analysis="a",
            convergence=ConclaveConvergence.I_AM_DONE,
            confidence=0.9,
        )


def _make_ctx():
    return SimpleNamespace(task_id="tsk_test", task=SimpleNamespace())


@pytest.mark.asyncio
async def test_tool_events_persist_without_crashing(temp_db):
    """The original AttributeError: persisting a tool_event whose
    message_type is a raw string instead of a MessageType enum must not
    crash _record_message."""
    events = [
        {"message_type": "tool_call", "direction": "from_agent",
         "content": None, "structured": {"function": "read_file", "arguments": '{"path":"x"}'}},
        {"message_type": "tool_result", "direction": "to_agent",
         "content": None, "structured": {"ok": True, "bytes": 42}},
    ]
    adapter = _StubAdapter(events)
    result, err = await _call_adapter_method(
        adapter, "run_conclave_turn", _make_ctx(), "tsk_test",
        AgentRole.PARTICIPANT, round_number=1,
    )
    # No exception → no AttributeError
    assert err is None, f"orchestrator raised: {err}"
    assert result is not None, "adapter result was dropped"

    # Both tool rows landed in the DB with the right message_type strings.
    with connect() as conn:
        rows = conn.execute(
            "SELECT message_type, direction FROM agent_messages WHERE task_id=? ORDER BY created_at",
            ("tsk_test",),
        ).fetchall()
    types = [r["message_type"] for r in rows]
    assert "tool_call" in types
    assert "tool_result" in types
    assert "conclave_turn" in types
    # The structured turn comes AFTER its tool events.
    assert types.index("conclave_turn") > types.index("tool_call")


@pytest.mark.asyncio
async def test_no_tool_events_still_works(temp_db):
    """The non-tool-loop path (empty _last_tool_events) must still record
    just the structured turn — no regression on the common case."""
    adapter = _StubAdapter(tool_events=[])
    result, err = await _call_adapter_method(
        adapter, "run_conclave_turn", _make_ctx(), "tsk_test",
        AgentRole.PARTICIPANT, round_number=1,
    )
    assert err is None
    with connect() as conn:
        rows = conn.execute(
            "SELECT message_type FROM agent_messages WHERE task_id=?",
            ("tsk_test",),
        ).fetchall()
    types = [r["message_type"] for r in rows]
    assert types == ["conclave_turn"]  # exactly one — no tool rows


@pytest.mark.asyncio
async def test_tool_events_linked_to_agent_run(temp_db):
    """Tool rows must reference the same agent_run_id as the structured turn
    they belong to — that linkage is what lets the dashboard group them
    correctly under their agent in the transcript."""
    events = [
        {"message_type": "tool_call", "direction": "from_agent",
         "content": None, "structured": {"function": "list_dir"}},
    ]
    adapter = _StubAdapter(events)
    await _call_adapter_method(
        adapter, "run_conclave_turn", _make_ctx(), "tsk_test",
        AgentRole.PARTICIPANT, round_number=1,
    )
    with connect() as conn:
        rows = conn.execute(
            "SELECT agent_run_id FROM agent_messages WHERE task_id=?",
            ("tsk_test",),
        ).fetchall()
    run_ids = {r["agent_run_id"] for r in rows}
    assert len(run_ids) == 1, "tool events should share the structured turn's agent_run_id"
