"""Neutral synthesis contract tests."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from app.protocol.validators import Limits, Permissions, TaskRequest
from app.services.synthesis import synthesize_conclave


def _task() -> TaskRequest:
    return TaskRequest(
        protocol_version="1.0", source="api", mode="conclave",
        task_type="general_consultation", user_request="Choose A or B",
        consultants=["a", "b"], synthesis_agent="neutral",
        permissions=Permissions(
            can_read_files=True, can_write_files=False, can_run_commands=False,
            can_access_network=False, can_install_packages=False,
            can_apply_patches=False, can_read_env_files=False, can_read_secrets=False,
        ),
        limits=Limits(max_rounds=3),
    )


async def test_synthesis_preserves_dissent_and_citations():
    task = _task()
    task.context.extra["evidence_snapshots"] = [{"id": "evd_ONE"}]
    adapter = AsyncMock()
    adapter.name = "neutral"
    adapter._invoke = AsyncMock(return_value=json.dumps({
        "final_answer": "A is simpler; B remains preferred for scale.",
        "preserved_disagreements": ["Whether scale justifies B"],
        "recommended_actions": [], "risks": [], "citation_ids": ["evd_ONE"],
    }))
    result = await synthesize_conclave(task, [
        {"agent": "a", "position": "Choose A"},
        {"agent": "b", "position": "Choose B"},
    ], adapter)
    assert result.preserved_disagreements == ["Whether scale justifies B"]
    assert result.citation_ids == ["evd_ONE"]
    prompt = adapter._invoke.await_args.args[0]
    assert "Never manufacture consensus" in prompt


def test_neutral_seats_cannot_be_participants():
    payload = _task().model_dump(mode="json")
    payload["judge_agent"] = "a"
    try:
        TaskRequest.model_validate(payload)
    except ValueError as error:
        assert "independent" in str(error)
    else:
        raise AssertionError("participant was accepted as neutral judge")
