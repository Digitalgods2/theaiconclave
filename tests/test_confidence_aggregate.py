"""Tests for confidence-weighted synthesis (Phase 2 of post-DR plan
tsk_01KRSW6AS3M66B4RRJE3JFAPRV).

Covers:
- _compute_confidence_aggregate basic stats (min/max/mean/count)
- All turns missing confidence → None
- Mixed missing/present → counted via missing_count
- _synthesis_directive_dict includes per-participant confidence
- _row_to_final_result roundtrips confidence_aggregate_json
- _compute_confidence_trajectory reconstructs per-agent rounds from messages
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.tasks import _compute_confidence_trajectory, _row_to_final_result
from app.database import connect, init_database
from app.services.orchestrator import (
    _compute_confidence_aggregate,
    _synthesis_directive_dict,
)


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_database(str(db_path))
        yield db_path


def _turn(agent: str, confidence, position: str = "x"):
    """Build a minimal turn-like object exposing .confidence and .position + .agent."""
    return SimpleNamespace(agent=agent, confidence=confidence, position=position)


def test_aggregate_basic_stats():
    turns = [_turn("a", 0.9), _turn("b", 0.6), _turn("c", 0.8), _turn("d", 1.0)]
    agg = _compute_confidence_aggregate(turns)
    assert agg["min"]  == 0.6
    assert agg["max"]  == 1.0
    assert agg["mean"] == 0.825
    assert agg["count"] == 4
    assert agg["missing_count"] == 0


def test_aggregate_all_missing_returns_none():
    turns = [_turn("a", None), _turn("b", None)]
    assert _compute_confidence_aggregate(turns) is None


def test_aggregate_partial_missing_counts_missing():
    turns = [_turn("a", 0.8), _turn("b", None), _turn("c", 0.9)]
    agg = _compute_confidence_aggregate(turns)
    assert agg["count"] == 2
    assert agg["missing_count"] == 1
    assert agg["mean"] == 0.85


def test_aggregate_empty_list_returns_none():
    assert _compute_confidence_aggregate([]) is None


def test_aggregate_rounds_to_three_decimals():
    turns = [_turn("a", 0.123456789), _turn("b", 0.987654321)]
    agg = _compute_confidence_aggregate(turns)
    assert agg["min"]  == 0.123
    assert agg["max"]  == 0.988
    assert agg["mean"] == 0.556


def test_synthesis_directive_includes_confidence():
    turns = [
        _turn("codex", 0.95, "Build the recovery console first."),
        _turn("gemini", 0.40, "Decision memory should come first."),
        _turn("claude-code", None, "Confidence-weighted synthesis is the win."),
    ]
    msg = _synthesis_directive_dict(turns)
    content = msg["content"]
    assert "[confidence 0.95]" in content
    assert "[confidence 0.40]" in content
    # The third participant emitted no confidence: no bracket inserted for it.
    assert "claude-code:" in content
    # And the directive instruction is still preserved.
    assert "Synthesis Round" in content


def test_synthesis_directive_handles_all_missing():
    turns = [_turn("a", None), _turn("b", None)]
    msg = _synthesis_directive_dict(turns)
    assert "confidence" not in msg["content"].lower() or "[confidence" not in msg["content"]


def test_row_to_final_result_extracts_confidence_aggregate(temp_db):
    """When the row has confidence_aggregate_json populated, the API includes it."""
    fake_row = {
        "task_id": "tsk_x",
        "final_answer": "answer",
        "agreement_level": "consensus",
        "resolution_status": None,
        "disagreements_json": "[]",
        "recommended_actions_json": "[]",
        "risks_json": "[]",
        "commands_requiring_approval_json": "[]",
        "patches_requiring_approval_json": "[]",
        "errors_json": "[]",
        "confidence_aggregate_json": json.dumps({"min": 0.6, "max": 0.9, "mean": 0.75, "count": 3, "missing_count": 0}),
        "created_at": "2026-05-16T00:00:00+00:00",
    }
    # Mimic sqlite3.Row.keys() so _column_or_none works.
    class _Row(dict):
        def keys(self):
            return list(super().keys())
        def __getitem__(self, k):
            return super().__getitem__(k)
    row = _Row(fake_row)
    out = _row_to_final_result(row)
    assert out["confidence_aggregate"]["mean"] == 0.75
    assert out["confidence_aggregate"]["count"] == 3
    assert out["action_plan"] == []


def test_row_to_final_result_handles_null_aggregate(temp_db):
    """Pre-Phase-2 rows have NULL aggregate — must not crash and must return None."""
    class _Row(dict):
        def keys(self):
            return list(super().keys())
        def __getitem__(self, k):
            return super().__getitem__(k)
    fake = _Row({
        "task_id": "tsk_x",
        "final_answer": "answer",
        "agreement_level": "consensus",
        "resolution_status": None,
        "disagreements_json": "[]",
        "recommended_actions_json": "[]",
        "risks_json": "[]",
        "commands_requiring_approval_json": "[]",
        "patches_requiring_approval_json": "[]",
        "errors_json": "[]",
        "confidence_aggregate_json": None,
        "created_at": "2026-05-16T00:00:00+00:00",
    })
    out = _row_to_final_result(fake)
    assert out["confidence_aggregate"] is None


def test_trajectory_reconstructed_from_messages():
    """Per-agent trajectory groups conclave_turn messages by agent across rounds."""
    msgs = [
        {"message_type": "conclave_turn", "agent_name": "codex",
         "structured_json": json.dumps({"confidence": 0.9, "convergence": "still_thinking"})},
        {"message_type": "conclave_turn", "agent_name": "gemini",
         "structured_json": json.dumps({"confidence": 0.5, "convergence": "still_thinking"})},
        {"message_type": "synthesis_directive", "agent_name": "orchestrator",
         "structured_json": None},
        {"message_type": "conclave_turn", "agent_name": "codex",
         "structured_json": json.dumps({"confidence": 0.95, "convergence": "i_am_done"})},
        {"message_type": "conclave_turn", "agent_name": "gemini",
         "structured_json": json.dumps({"confidence": 0.85, "convergence": "i_am_done"})},
    ]
    traj = _compute_confidence_trajectory(msgs)
    by_agent = {t["agent"]: t["rounds"] for t in traj}
    assert by_agent["codex"] == [
        {"round": 1, "confidence": 0.9,  "convergence": "still_thinking"},
        {"round": 2, "confidence": 0.95, "convergence": "i_am_done"},
    ]
    assert by_agent["gemini"][1]["confidence"] == 0.85
    # Synthesis directive is filtered out
    assert "orchestrator" not in by_agent


def test_trajectory_skips_messages_without_structured_json():
    msgs = [
        {"message_type": "conclave_turn", "agent_name": "codex", "structured_json": None},
        {"message_type": "conclave_turn", "agent_name": "codex",
         "structured_json": json.dumps({"confidence": 0.7, "convergence": "i_am_done"})},
    ]
    traj = _compute_confidence_trajectory(msgs)
    assert traj == [{"agent": "codex", "rounds": [
        {"round": 1, "confidence": 0.7, "convergence": "i_am_done"}
    ]}]


def test_trajectory_empty_when_no_conclave_turns():
    msgs = [
        {"message_type": "user_input_request", "agent_name": "codex", "structured_json": None},
    ]
    assert _compute_confidence_trajectory(msgs) == []
