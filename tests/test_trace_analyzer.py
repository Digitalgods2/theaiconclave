"""Unit tests for `services.trace_analyzer.classify_failure_causes`.

Rule-based classifier; deterministic. One test per rule path, plus defensive
tests covering missing fields, mixed shapes (dict vs sqlite3.Row-like), and
the contract that an unexpected exception returns [] rather than raising.
"""

from __future__ import annotations

import json

import pytest

from app.protocol.validators import FailureCause
from app.services.trace_analyzer import classify_failure_causes


# ---------------------------------------------------------------------------
# Fixture helpers — keep the test cases short and readable.
# ---------------------------------------------------------------------------

def _task(status: str = "completed", **overrides) -> dict:
    base = {"status": status, "user_decision": None}
    base.update(overrides)
    return base


def _final(
    *,
    agreement_level: str = "consensus",
    resolution_status: str | None = None,
    errors: list[dict] | None = None,
) -> dict:
    return {
        "agreement_level":   agreement_level,
        "resolution_status": resolution_status,
        # Trace analyzer reads `errors` directly when present.
        "errors":            errors or [],
    }


def _msg(message_type: str, structured: dict | None = None, **extra) -> dict:
    out = {"message_type": message_type, "agent_name": "alpha", "role": "participant"}
    if structured is not None:
        out["structured"] = structured
    out.update(extra)
    return out


def _run(status: str = "completed", error_code: str | None = None) -> dict:
    return {"status": status, "error_code": error_code}


# ---------------------------------------------------------------------------
# Rule: unresolved_dissent
# ---------------------------------------------------------------------------

def test_unresolved_dissent_when_agreement_unresolved():
    causes = classify_failure_causes(
        task_row=_task(),
        messages=[],
        runs=[],
        final_result=_final(agreement_level="unresolved"),
    )
    assert FailureCause.UNRESOLVED_DISSENT in causes


def test_unresolved_dissent_when_agreement_major_disagreement():
    causes = classify_failure_causes(
        task_row=_task(),
        messages=[],
        runs=[],
        final_result=_final(agreement_level="major_disagreement"),
    )
    assert FailureCause.UNRESOLVED_DISSENT in causes


def test_unresolved_dissent_when_cannot_resolve_without_decision():
    causes = classify_failure_causes(
        task_row=_task(status="completed", user_decision=None),
        messages=[],
        runs=[],
        final_result=_final(agreement_level="consensus", resolution_status="cannot_resolve"),
    )
    assert FailureCause.UNRESOLVED_DISSENT in causes


def test_no_dissent_on_clean_consensus():
    causes = classify_failure_causes(
        task_row=_task(status="completed"),
        messages=[],
        runs=[],
        final_result=_final(agreement_level="consensus"),
    )
    assert FailureCause.UNRESOLVED_DISSENT not in causes


# ---------------------------------------------------------------------------
# Rule: bad_json_output
# ---------------------------------------------------------------------------

def test_bad_json_output_detected_by_error_code():
    causes = classify_failure_causes(
        task_row=_task(),
        messages=[],
        runs=[],
        final_result=_final(errors=[{"code": "agent_error", "message": "could not extract JSON from output"}]),
    )
    assert FailureCause.BAD_JSON_OUTPUT in causes


def test_bad_json_output_detected_by_explicit_parse_code():
    causes = classify_failure_causes(
        task_row=_task(),
        messages=[],
        runs=[],
        final_result=_final(errors=[{"code": "json_parse_error", "message": "x"}]),
    )
    assert FailureCause.BAD_JSON_OUTPUT in causes


# ---------------------------------------------------------------------------
# Rule: tool_timeout
# ---------------------------------------------------------------------------

def test_tool_timeout_from_run_error_code():
    causes = classify_failure_causes(
        task_row=_task(),
        messages=[],
        runs=[_run(status="failed", error_code="agent_timeout")],
        final_result=_final(),
    )
    assert FailureCause.TOOL_TIMEOUT in causes


def test_tool_timeout_from_run_status():
    causes = classify_failure_causes(
        task_row=_task(),
        messages=[],
        runs=[_run(status="timeout")],
        final_result=_final(),
    )
    assert FailureCause.TOOL_TIMEOUT in causes


def test_tool_timeout_from_final_resolve_timeout():
    causes = classify_failure_causes(
        task_row=_task(),
        messages=[],
        runs=[],
        final_result=_final(errors=[{"code": "resolve_timeout", "message": "max_seconds hit"}]),
    )
    assert FailureCause.TOOL_TIMEOUT in causes


# ---------------------------------------------------------------------------
# Rule: multimodal_perception_split
# ---------------------------------------------------------------------------

def test_multimodal_split_detected_when_visual_word_in_question():
    msgs = [
        _msg(
            "conclave_turn",
            structured={
                "convergence": "need_user_input",
                "user_input_question": "Which CHART are you referring to?",
            },
        )
    ]
    causes = classify_failure_causes(
        task_row=_task(status="awaiting_user_input"),
        messages=msgs,
        runs=[],
        final_result=_final(),
    )
    assert FailureCause.MULTIMODAL_PERCEPTION_SPLIT in causes


def test_multimodal_split_not_fired_when_question_is_text_only():
    msgs = [
        _msg(
            "conclave_turn",
            structured={
                "convergence": "need_user_input",
                "user_input_question": "Which Python version should be assumed?",
            },
        )
    ]
    causes = classify_failure_causes(
        task_row=_task(),
        messages=msgs,
        runs=[],
        final_result=_final(),
    )
    assert FailureCause.MULTIMODAL_PERCEPTION_SPLIT not in causes


# ---------------------------------------------------------------------------
# Rule: clarification_unanswered
# ---------------------------------------------------------------------------

def test_clarification_unanswered_when_request_without_response_on_terminal_task():
    msgs = [_msg("user_input_request", content="?")]
    causes = classify_failure_causes(
        task_row=_task(status="cancelled"),
        messages=msgs,
        runs=[],
        final_result=_final(agreement_level="unresolved"),
    )
    assert FailureCause.CLARIFICATION_UNANSWERED in causes


def test_clarification_not_fired_when_response_present():
    msgs = [
        _msg("user_input_request", content="?"),
        _msg("user_input_response", content="here you go"),
    ]
    causes = classify_failure_causes(
        task_row=_task(status="completed"),
        messages=msgs,
        runs=[],
        final_result=_final(),
    )
    assert FailureCause.CLARIFICATION_UNANSWERED not in causes


# ---------------------------------------------------------------------------
# Rule: permission_denied
# ---------------------------------------------------------------------------

def test_permission_denied_from_error_code():
    causes = classify_failure_causes(
        task_row=_task(),
        messages=[],
        runs=[],
        final_result=_final(errors=[{"code": "permission_denied", "message": "no can_write_files"}]),
    )
    assert FailureCause.PERMISSION_DENIED in causes


def test_permission_denied_from_error_message_keyword():
    causes = classify_failure_causes(
        task_row=_task(),
        messages=[],
        runs=[],
        final_result=_final(errors=[{"code": "agent_error", "message": "operation denied by policy"}]),
    )
    assert FailureCause.PERMISSION_DENIED in causes


# ---------------------------------------------------------------------------
# Rule: repetition_loop_backstop
# ---------------------------------------------------------------------------

def test_repetition_loop_from_loop_detected_code():
    causes = classify_failure_causes(
        task_row=_task(),
        messages=[],
        runs=[],
        final_result=_final(errors=[{"code": "loop_detected", "message": "too similar to prior round"}]),
    )
    assert FailureCause.REPETITION_LOOP_BACKSTOP in causes


def test_repetition_loop_from_message_keyword():
    causes = classify_failure_causes(
        task_row=_task(),
        messages=[],
        runs=[],
        final_result=_final(errors=[{"code": "agent_error", "message": "repetition guard fired"}]),
    )
    assert FailureCause.REPETITION_LOOP_BACKSTOP in causes


# ---------------------------------------------------------------------------
# De-dup + ordering
# ---------------------------------------------------------------------------

def test_results_are_deduped_and_stable():
    """Same condition triggered multiple ways must appear only once."""
    causes = classify_failure_causes(
        task_row=_task(),
        messages=[],
        runs=[_run(status="timeout"), _run(status="timeout")],
        final_result=_final(errors=[{"code": "resolve_timeout", "message": "x"}]),
    )
    # Both run + final report a timeout — still exactly one tag.
    assert causes.count(FailureCause.TOOL_TIMEOUT) == 1


def test_multiple_distinct_rules_all_recorded():
    causes = classify_failure_causes(
        task_row=_task(),
        messages=[],
        runs=[_run(status="timeout")],
        final_result=_final(
            agreement_level="unresolved",
            errors=[
                {"code": "json_parse_error", "message": "x"},
                {"code": "loop_detected",    "message": "y"},
            ],
        ),
    )
    assert set(causes) == {
        FailureCause.UNRESOLVED_DISSENT,
        FailureCause.BAD_JSON_OUTPUT,
        FailureCause.TOOL_TIMEOUT,
        FailureCause.REPETITION_LOOP_BACKSTOP,
    }


# ---------------------------------------------------------------------------
# Defensive: contract is "never raise; return [] on unexpected failure"
# ---------------------------------------------------------------------------

def test_returns_empty_on_missing_inputs():
    """All-None inputs must not crash — older / partial data exists."""
    assert classify_failure_causes(None, None, None, None) == []


def test_returns_empty_on_bad_error_shape():
    """`errors` not a list of dicts must be silently ignored."""
    # Garbage in errors_json shape — should be tolerated as empty.
    assert classify_failure_causes(
        task_row=_task(),
        messages=[],
        runs=[],
        final_result={"agreement_level": "consensus", "errors_json": "not-json{"},
    ) == []


def test_accepts_serialized_errors_json_column():
    """The orchestrator hook passes the raw DB row, so the analyzer also has to
    decode `errors_json` (string) when there's no decoded `errors` key."""
    fr = {
        "agreement_level": "consensus",
        "errors_json": json.dumps([{"code": "resolve_timeout", "message": "x"}]),
    }
    causes = classify_failure_causes(_task(), [], [], fr)
    assert FailureCause.TOOL_TIMEOUT in causes


def test_quick_consensus_yields_no_tags():
    """The happy path: 3-way agreement, no errors, no clarification, returns []."""
    msgs = [
        _msg("conclave_turn", structured={"convergence": "i_am_done", "confidence": 0.9}),
        _msg("conclave_turn", structured={"convergence": "i_am_done", "confidence": 0.9}),
        _msg("conclave_turn", structured={"convergence": "i_am_done", "confidence": 0.9}),
    ]
    assert classify_failure_causes(
        task_row=_task(status="completed"),
        messages=msgs,
        runs=[_run(status="completed"), _run(status="completed"), _run(status="completed")],
        final_result=_final(agreement_level="consensus"),
    ) == []
