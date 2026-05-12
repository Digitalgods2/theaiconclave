"""
Protocol validator tests.

Loads every example JSON in examples/ through the matching Pydantic model,
asserts the model accepts it, and checks key invariants from the protocol
doc and safety model. This test must pass before Milestone 1 is complete.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.protocol.validators import (
    Approval,
    ConsultantCritique,
    FinalResult,
    MessageType,
    PeerAnswer,
    PrimaryResponse,
    TaskRequest,
)

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def _load(name: str) -> dict:
    return json.loads((EXAMPLES_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Round-trip: every example loads, validates, and re-serializes equivalently.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "filename, model",
    [
        ("task_request_debug.json", TaskRequest),
        ("task_request_code_review.json", TaskRequest),
        ("agent_response_primary.json", PrimaryResponse),
        ("agent_response_consultant.json", ConsultantCritique),
        ("peer_answer_poll.json", PeerAnswer),
        ("final_result.json", FinalResult),
        ("approval_required.json", Approval),
    ],
)
def test_example_validates(filename: str, model) -> None:
    raw = _load(filename)
    instance = model.model_validate(raw)
    re_dumped = instance.model_dump(mode="json")
    re_loaded = model.model_validate(re_dumped)
    assert re_loaded.model_dump(mode="json") == re_dumped


# ---------------------------------------------------------------------------
# Mode invariants — SWITCHBOARD_PROTOCOL.md §4 + role_disambiguation.md.
# ---------------------------------------------------------------------------

def test_consult_requires_primary_agent() -> None:
    raw = _load("task_request_debug.json")
    raw["primary_agent"] = None
    with pytest.raises(ValueError, match="primary_agent is required"):
        TaskRequest.model_validate(raw)


def test_consult_requires_at_least_one_consultant() -> None:
    raw = _load("task_request_debug.json")
    raw["consultants"] = []
    with pytest.raises(ValueError, match="consultants must be non-empty"):
        TaskRequest.model_validate(raw)


def test_handoff_requires_primary() -> None:
    raw = _load("task_request_debug.json")
    raw["mode"] = "handoff"
    raw["primary_agent"] = None
    with pytest.raises(ValueError, match="primary_agent is required"):
        TaskRequest.model_validate(raw)


def test_poll_requires_at_least_two_consultants() -> None:
    raw = _load("task_request_debug.json")
    raw["mode"] = "poll"
    raw["primary_agent"] = None
    raw["consultants"] = ["claude-code"]
    with pytest.raises(ValueError, match="poll mode requires at least 2"):
        TaskRequest.model_validate(raw)


def test_poll_rejects_primary_agent() -> None:
    raw = _load("task_request_debug.json")
    raw["mode"] = "poll"
    raw["consultants"] = ["claude-code", "gemini"]
    raw["primary_agent"] = "codex"
    with pytest.raises(ValueError, match="primary_agent must be omitted"):
        TaskRequest.model_validate(raw)


# ---------------------------------------------------------------------------
# Permission implication — SAFETY_MODEL.md §1.
# ---------------------------------------------------------------------------

def test_install_packages_requires_run_and_network() -> None:
    raw = _load("task_request_debug.json")
    raw["permissions"]["can_install_packages"] = True
    raw["permissions"]["can_run_commands"] = False
    raw["permissions"]["can_access_network"] = False
    with pytest.raises(ValueError, match="can_install_packages requires"):
        TaskRequest.model_validate(raw)


def test_install_packages_succeeds_when_implications_satisfied() -> None:
    raw = _load("task_request_debug.json")
    raw["permissions"]["can_install_packages"] = True
    raw["permissions"]["can_run_commands"] = True
    raw["permissions"]["can_access_network"] = True
    TaskRequest.model_validate(raw)


# ---------------------------------------------------------------------------
# Versioning — SWITCHBOARD_PROTOCOL.md §2.
# ---------------------------------------------------------------------------

def test_minor_version_bump_accepted() -> None:
    raw = _load("task_request_debug.json")
    raw["protocol_version"] = "1.99"
    TaskRequest.model_validate(raw)


def test_major_version_bump_rejected() -> None:
    raw = _load("task_request_debug.json")
    raw["protocol_version"] = "2.0"
    with pytest.raises(ValueError, match="protocol_version_mismatch"):
        TaskRequest.model_validate(raw)


# ---------------------------------------------------------------------------
# Message-type invariants on responses.
# ---------------------------------------------------------------------------

def test_primary_response_has_primary_message_type() -> None:
    raw = _load("agent_response_primary.json")
    instance = PrimaryResponse.model_validate(raw)
    assert instance.message_type in (
        MessageType.PRIMARY_PROPOSAL,
        MessageType.PRIMARY_FINAL,
    )


def test_consultant_critique_carries_agreement() -> None:
    raw = _load("agent_response_consultant.json")
    instance = ConsultantCritique.model_validate(raw)
    assert instance.agreement.value in {"agree", "partial", "disagree"}


# ---------------------------------------------------------------------------
# Final result must preserve disagreements verbatim — design rule, not flatten.
# ---------------------------------------------------------------------------

def test_final_result_disagreements_have_full_structure() -> None:
    raw = _load("final_result.json")
    instance = FinalResult.model_validate(raw)
    for d in instance.disagreements:
        assert d.topic, "disagreement.topic must be non-empty"
        assert d.primary_position, "disagreement.primary_position must be non-empty"
        assert d.consultant_position, "disagreement.consultant_position must be non-empty"


def test_final_result_in_example_has_at_least_one_disagreement() -> None:
    """The canonical example shows the design intent: surface disagreement."""
    raw = _load("final_result.json")
    instance = FinalResult.model_validate(raw)
    assert len(instance.disagreements) >= 1
