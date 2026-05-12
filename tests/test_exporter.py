"""Tests for app.services.exporter.export_to_markdown.

These tests cover:
  * Every required section appears in the output for a fully-populated task.
  * Missing fields (no decision, no final_result, no disagreements, no runs)
    still produce a clean, readable document - no empty headers, no crashes.
  * Special characters in user_request / agent prose (backticks, fences,
    pipes, ``` , markdown markup) are escaped or fenced so the markdown
    renders correctly without breaking out of code blocks or tables.
"""

from __future__ import annotations

import re

import pytest

from app.services.exporter import export_to_markdown


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _full_task() -> dict:
    return {
        "id": "task_abc123",
        "status": "completed",
        "mode": "conclave",
        "task_type": "general_consultation",
        "user_request": "Should we adopt Rust for the new service?",
        "primary_agent": "codex",
        "consultants": ["claude-code", "gemini"],
        "project_path": "C:/work/svc",
        "permissions": {"can_read_files": True},
        "limits": {"max_rounds": 5},
        "created_at": "2026-05-01T10:00:00+00:00",
        "updated_at": "2026-05-01T10:30:00+00:00",
        "error_message": None,
        "user_decision": "Adopt Rust for net-new services only.\nKeep Go for existing services.",
        "user_decided_at": "2026-05-02T09:00:00+00:00",
        "parent_task_id": None,
    }


def _full_messages() -> list[dict]:
    return [
        {
            "id": "m1",
            "agent_name": "codex",
            "role": "primary",
            "message_type": "primary_response",
            "direction": "from_agent",
            "structured": {
                "round": 1,
                "summary": "Rust offers strong safety guarantees.",
                "position": "Adopt for new services.",
                "analysis": "Memory-safety wins outweigh ramp-up cost over 6 months.",
            },
            "created_at": "2026-05-01T10:05:00+00:00",
        },
        {
            "id": "m2",
            "agent_name": "claude-code",
            "role": "consultant",
            "message_type": "consultant_critique",
            "direction": "from_agent",
            "structured": {
                "round": 1,
                "critique": "Watch hiring funnel - smaller pool than Go.",
                "agreement": "partial",
            },
            "created_at": "2026-05-01T10:06:00+00:00",
        },
        {
            "id": "m3",
            "agent_name": "codex",
            "role": "primary",
            "message_type": "primary_response",
            "direction": "from_agent",
            "structured": {
                "round": 2,
                "convergence": "agreed to scope to net-new services",
            },
            "created_at": "2026-05-01T10:10:00+00:00",
        },
    ]


def _full_final_result() -> dict:
    return {
        "task_id": "task_abc123",
        "final_answer": "Adopt Rust for net-new services with a 6-month ramp.",
        "agreement_level": "high",
        "resolution_status": "resolved",
        "disagreements": [
            {
                "topic": "Hiring impact",
                "primary_position": "Manageable with bootcamps.",
                "consultant_position": "Significant near-term cost.",
                "severity": "medium",
            }
        ],
        "recommended_actions": ["Pilot project Q3", "Hire two Rust seniors"],
        "risks": ["Hiring funnel"],
        "commands_requiring_approval": [],
        "patches_requiring_approval": [],
        "errors": [],
        "created_at": "2026-05-01T10:30:00+00:00",
    }


def _full_runs() -> list[dict]:
    return [
        {
            "id": "r1", "agent_name": "codex", "role": "primary", "round_number": 1,
            "started_at": "2026-05-01T10:05:00+00:00", "finished_at": "2026-05-01T10:05:30+00:00",
            "status": "ok", "duration_ms": 30000, "error_code": None, "error_message": None,
            "input_tokens": 1500, "output_tokens": 800, "cost_usd": 0.025,
        },
        {
            "id": "r2", "agent_name": "claude-code", "role": "consultant", "round_number": 1,
            "started_at": "2026-05-01T10:06:00+00:00", "finished_at": "2026-05-01T10:06:20+00:00",
            "status": "ok", "duration_ms": 20000, "error_code": None, "error_message": None,
            "input_tokens": 1200, "output_tokens": 400, "cost_usd": 0.012,
        },
    ]


# ---------------------------------------------------------------------------
# Section presence
# ---------------------------------------------------------------------------

def test_all_required_sections_present_for_full_task():
    md = export_to_markdown(_full_task(), _full_messages(), _full_final_result(), _full_runs())

    # Header
    assert "# Task task_abc123" in md
    assert "**Status:** completed" in md
    assert "**Mode:** conclave" in md
    assert "codex (primary)" in md
    assert "claude-code" in md and "gemini" in md
    assert "**Created at:** 2026-05-01T10:00:00+00:00" in md

    # All required H2 section headers
    for section in ("## Question", "## Decision", "## Final result",
                    "## Transcript", "## Usage"):
        assert section in md, f"missing section: {section}"

    # Question content
    assert "Should we adopt Rust for the new service?" in md

    # Decision content (multi-line decision must survive)
    assert "Adopt Rust for net-new services only." in md
    assert "Keep Go for existing services." in md

    # Final result content
    assert "Adopt Rust for net-new services with a 6-month ramp." in md
    assert "**Agreement level:**" in md and "high" in md
    assert "### Disagreements" in md
    assert "Hiring impact" in md
    assert "Pilot project Q3" in md  # recommended action

    # Transcript - conclave mode groups by round
    assert "### Round 1" in md
    assert "### Round 2" in md
    assert "#### codex" in md
    assert "#### claude-code" in md
    assert "Memory-safety wins outweigh ramp-up cost" in md  # analysis preserved verbatim
    assert "Watch hiring funnel" in md
    assert "agreed to scope to net-new services" in md

    # Usage aggregation
    assert "Total input tokens:**" in md
    assert "2700" in md  # 1500 + 1200
    assert "1200" in md  # 800 + 400
    assert "Total cost (USD)" in md
    # 0.025 + 0.012 = 0.037
    assert "0.0370" in md

    # Footer
    assert re.search(r"_Exported on .+ by AI Switchboard\._", md)


def test_section_order_is_header_question_decision_final_transcript_usage_footer():
    md = export_to_markdown(_full_task(), _full_messages(), _full_final_result(), _full_runs())

    def pos(needle: str) -> int:
        idx = md.find(needle)
        assert idx >= 0, f"missing: {needle}"
        return idx

    order = [
        pos("# Task task_abc123"),
        pos("## Question"),
        pos("## Decision"),
        pos("## Final result"),
        pos("## Transcript"),
        pos("## Usage"),
        pos("by AI Switchboard."),
    ]
    assert order == sorted(order), f"sections out of order: {order}"


def test_full_analysis_and_critique_preserved_verbatim_no_truncation():
    # Build a deliberately long analysis to confirm the exporter does not truncate.
    long_analysis = "Detail line. " * 500
    messages = [{
        "id": "m1",
        "agent_name": "codex",
        "role": "primary",
        "message_type": "primary_response",
        "direction": "from_agent",
        "structured": {"round": 1, "analysis": long_analysis, "critique": long_analysis},
        "created_at": "2026-05-01T10:05:00+00:00",
    }]
    md = export_to_markdown(_full_task(), messages, None, [])
    # Both copies of the long string must be present in full.
    assert md.count("Detail line.") >= 1000


# ---------------------------------------------------------------------------
# Missing-field cases
# ---------------------------------------------------------------------------

def test_no_user_decision_renders_none_recorded():
    task = _full_task()
    task["user_decision"] = None
    task["user_decided_at"] = None
    md = export_to_markdown(task, _full_messages(), _full_final_result(), _full_runs())
    assert "## Decision" in md
    assert "(none recorded)" in md


def test_no_final_result_renders_placeholder_without_crashing():
    md = export_to_markdown(_full_task(), _full_messages(), None, _full_runs())
    assert "## Final result" in md
    assert "(no final result was produced)" in md
    # No disagreements / recommended_actions sub-headers should appear.
    assert "### Disagreements" not in md
    assert "### Recommended actions" not in md


def test_final_result_without_disagreements_omits_subsection():
    fr = _full_final_result()
    fr["disagreements"] = []
    fr["recommended_actions"] = []
    fr["risks"] = []
    md = export_to_markdown(_full_task(), _full_messages(), fr, _full_runs())
    assert "## Final result" in md
    assert "### Disagreements" not in md
    assert "### Recommended actions" not in md
    assert "### Risks" not in md
    # Final answer still appears.
    assert "Adopt Rust for net-new services with a 6-month ramp." in md


def test_no_messages_renders_placeholder():
    md = export_to_markdown(_full_task(), [], None, [])
    assert "## Transcript" in md
    assert "(no messages recorded)" in md


def test_no_runs_renders_placeholder_in_usage():
    md = export_to_markdown(_full_task(), _full_messages(), _full_final_result(), [])
    assert "## Usage" in md
    assert "(no run accounting recorded)" in md


def test_non_conclave_mode_uses_flat_transcript_order():
    task = _full_task()
    task["mode"] = "consult"
    md = export_to_markdown(task, _full_messages(), _full_final_result(), _full_runs())
    # No "Round X" sub-headers in flat mode.
    assert "### Round 1" not in md
    assert "### Round 2" not in md
    # Messages still appear in order.
    assert md.find("#### codex") < md.find("#### claude-code")


def test_minimal_task_does_not_crash():
    """Smoke test for the truly-empty case: only an id and a question."""
    minimal = {"id": "t1", "user_request": "hi"}
    md = export_to_markdown(minimal, [], None, [])
    assert "# Task t1" in md
    assert "## Question" in md
    assert "hi" in md
    assert "## Decision" in md
    assert "## Transcript" in md
    assert "## Usage" in md


# ---------------------------------------------------------------------------
# Escaping / special characters
# ---------------------------------------------------------------------------

def test_user_request_with_backtick_fences_does_not_break_code_block():
    task = _full_task()
    task["user_request"] = (
        "Here's a snippet:\n"
        "```python\n"
        "print('hi')\n"
        "```\n"
        "What do you think?"
    )
    md = export_to_markdown(task, [], None, [])
    # The exporter must wrap the question in a fence longer than any embedded fence,
    # so the embedded ``` survives intact and the outer block is not broken.
    assert "```python" in md  # inner fence preserved
    assert "print('hi')" in md  # full content preserved
    # Find the section that wraps the question - it must use a fence of >= 4 backticks.
    question_idx = md.find("## Question")
    assert question_idx >= 0
    snippet = md[question_idx:question_idx + 2000]
    # At least one fence with 4+ backticks (outer fence) must exist.
    assert re.search(r"````+", snippet), "outer fence must be longer than embedded fence"


def test_agent_prose_with_pipes_and_newlines_does_not_break_table():
    runs = _full_runs()
    runs[0]["agent_name"] = "weird|name"
    runs[0]["error_message"] = "line1\nline2"
    md = export_to_markdown(_full_task(), _full_messages(), _full_final_result(), runs)
    # Pipes in cells must be escaped so they don't split table columns.
    assert "weird\\|name" in md
    # Each table row should still contain exactly 9 column separators (| ... |).
    table_lines = [
        ln for ln in md.splitlines()
        if ln.startswith("|") and "weird" in ln
    ]
    assert table_lines, "expected the row with the weird agent to appear"
    for ln in table_lines:
        # 8 columns -> 9 unescaped pipes per row; the escaped \| must not count.
        unescaped = re.findall(r"(?<!\\)\|", ln)
        assert len(unescaped) == 9, f"row malformed: {ln!r}"


def test_special_chars_in_final_answer_are_fenced():
    fr = _full_final_result()
    fr["final_answer"] = "Use **bold** and `code` and # not-a-header here."
    md = export_to_markdown(_full_task(), [], fr, [])
    # Because the final answer is inside a fenced code block, the markdown special
    # characters are rendered literally, not interpreted.
    assert "Use **bold** and `code` and # not-a-header here." in md
    # Verify the answer sits inside a fenced block (look for a fence right before it).
    answer_idx = md.find("Use **bold** and `code`")
    assert answer_idx >= 0
    # The 200 chars preceding the answer should contain an opening fence.
    preamble = md[max(0, answer_idx - 200):answer_idx]
    assert "```" in preamble


def test_structured_message_with_multiline_string_uses_fenced_block():
    msg = {
        "id": "m1",
        "agent_name": "codex",
        "role": "primary",
        "message_type": "primary_response",
        "direction": "from_agent",
        "structured": {
            "round": 1,
            "analysis": "Step 1: do X\nStep 2: do Y\n```inline\nfoo\n```\nStep 3.",
        },
        "created_at": "2026-05-01T10:05:00+00:00",
    }
    md = export_to_markdown(_full_task(), [msg], None, [])
    # All three steps survive verbatim including the embedded fence.
    assert "Step 1: do X" in md
    assert "Step 2: do Y" in md
    assert "```inline" in md
    assert "Step 3." in md


def test_output_ends_with_single_trailing_newline():
    md = export_to_markdown(_full_task(), _full_messages(), _full_final_result(), _full_runs())
    assert md.endswith("\n")
    assert not md.endswith("\n\n\n")


def test_unicode_content_round_trips():
    task = _full_task()
    task["user_request"] = "Should we 採用 Rust? Pros & cons — emoji ok"
    md = export_to_markdown(task, [], None, [])
    assert "採用" in md
    assert "—" in md
