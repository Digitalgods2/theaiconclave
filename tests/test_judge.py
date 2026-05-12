"""Tests for the convergence judge (semantic-equivalence pass)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.protocol.validators import Limits, Permissions, TaskRequest
from app.services.judge import _format_positions_for_judge, judge_convergence


def _make_task() -> TaskRequest:
    return TaskRequest(
        protocol_version="1.0",
        source="api",
        mode="conclave",
        task_type="general_consultation",
        user_request="Should we use Postgres or MongoDB?",
        primary_agent=None,
        consultants=["codex", "gemini"],
        permissions=Permissions(
            can_read_files=True, can_write_files=False, can_run_commands=False,
            can_access_network=False, can_install_packages=False,
            can_apply_patches=False, can_read_env_files=False, can_read_secrets=False,
        ),
        limits=Limits(max_rounds=5, timeout_seconds=180),
    )


def _make_fake_adapter(response_text: str):
    """Build a minimal mock adapter whose _invoke returns the given text."""
    adapter = AsyncMock()
    adapter.name = "test-judge"
    adapter._invoke = AsyncMock(return_value=response_text)
    return adapter


async def test_judge_returns_true_when_response_says_equivalent():
    task = _make_task()
    positions = [
        {"agent": "codex", "position": "Use PostgreSQL for v1."},
        {"agent": "gemini", "position": "PostgreSQL is the right choice."},
    ]
    adapter = _make_fake_adapter(json.dumps({
        "equivalent": True,
        "reasoning": "Both recommend PostgreSQL.",
    }))
    verdict = await judge_convergence(positions, task, "tsk_TEST", adapter)
    assert verdict["equivalent"] is True
    assert "PostgreSQL" in verdict["reasoning"]


async def test_judge_returns_false_when_response_says_not_equivalent():
    task = _make_task()
    positions = [
        {"agent": "codex", "position": "Use PostgreSQL."},
        {"agent": "gemini", "position": "Use MongoDB."},
    ]
    adapter = _make_fake_adapter(json.dumps({
        "equivalent": False,
        "reasoning": "These recommend different databases.",
    }))
    verdict = await judge_convergence(positions, task, "tsk_TEST", adapter)
    assert verdict["equivalent"] is False


async def test_judge_returns_none_on_adapter_failure():
    task = _make_task()
    positions = [
        {"agent": "codex", "position": "Use PostgreSQL."},
        {"agent": "gemini", "position": "Use PostgreSQL."},
    ]
    adapter = AsyncMock()
    adapter.name = "test-judge"
    adapter._invoke = AsyncMock(side_effect=RuntimeError("simulated failure"))
    verdict = await judge_convergence(positions, task, "tsk_TEST", adapter)
    assert verdict["equivalent"] is None
    assert "simulated failure" in verdict["reasoning"]


async def test_judge_returns_none_on_unparseable_json():
    task = _make_task()
    positions = [{"agent": "a", "position": "x"}, {"agent": "b", "position": "y"}]
    adapter = _make_fake_adapter("not valid json at all")
    verdict = await judge_convergence(positions, task, "tsk_TEST", adapter)
    assert verdict["equivalent"] is None


async def test_judge_handles_fenced_json():
    """Real CLI agents sometimes wrap output in ```json fences."""
    task = _make_task()
    positions = [{"agent": "a", "position": "x"}, {"agent": "b", "position": "x"}]
    fenced = "```json\n" + json.dumps({"equivalent": True, "reasoning": "same"}) + "\n```"
    adapter = _make_fake_adapter(fenced)
    verdict = await judge_convergence(positions, task, "tsk_TEST", adapter)
    assert verdict["equivalent"] is True


async def test_single_position_is_trivially_equivalent():
    """Edge case: only one position to judge."""
    task = _make_task()
    positions = [{"agent": "solo", "position": "x"}]
    adapter = _make_fake_adapter("")  # not called
    verdict = await judge_convergence(positions, task, "tsk_TEST", adapter)
    assert verdict["equivalent"] is True
    # Adapter should not have been called
    adapter._invoke.assert_not_called()


def test_format_positions_includes_all_agents_and_text():
    positions = [
        {"agent": "codex", "position": "Use PostgreSQL"},
        {"agent": "gemini", "position": "Use MongoDB"},
        {"agent": "claude-code", "position": "Use SQLite"},
    ]
    formatted = _format_positions_for_judge(positions)
    assert "codex" in formatted
    assert "gemini" in formatted
    assert "claude-code" in formatted
    assert "PostgreSQL" in formatted
    assert "MongoDB" in formatted
    assert "SQLite" in formatted
