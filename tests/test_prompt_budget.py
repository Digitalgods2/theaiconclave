"""Tests for the centralized prompt-budget enforcement (DR0017 Batch C).

Covers:
- The helper module's own behavior (trim, marker, no-budget edge cases)
- End-to-end: every public builder honors ceiling_chars when set, drops
  oldest prior_messages first, emits the marker, and never disturbs the
  schema demand or core sections.
"""

from __future__ import annotations

import pytest

from app.protocol.validators import Limits, Permissions, TaskMode, TaskRequest, TaskSource
from app.services import prompt_budget
from app.services.prompt_builder import (
    build_conclave_prompt,
    build_consultant_prompt,
    build_final_prompt,
    build_peer_prompt,
    build_primary_prompt,
)


# ---------------------------------------------------------------------------
# prompt_budget module — unit tests
# ---------------------------------------------------------------------------

def test_prior_messages_budget_subtracts_overhead():
    """The budget is `ceiling - already_used - reserved_overhead`."""
    overhead = prompt_budget.reserved_overhead()
    assert prompt_budget.prior_messages_budget(100_000, 10_000) == 100_000 - 10_000 - overhead


def test_prior_messages_budget_floors_at_zero():
    """Never returns a negative budget."""
    assert prompt_budget.prior_messages_budget(1_000, 999) == 0
    assert prompt_budget.prior_messages_budget(0, 0) == 0


def test_trim_with_empty_list():
    out, dropped = prompt_budget.trim_prior_messages([], lambda m: "", 1_000_000)
    assert out == []
    assert dropped == 0


def test_trim_drops_oldest_first():
    """When the budget can fit some but not all messages, the newest survive.

    Budget must exceed `_MIN_INCLUDE_BUDGET` (2_000) for partial trimming to
    engage at all — below that the function returns ([], len(msgs)) since
    the marker alone is more honest than truncated turns.
    """
    msgs = [{"i": i} for i in range(5)]
    # Each formatted message: 1000 chars + 1 newline = 1001 chars.
    # Budget 3500 fits 3 (3003 ≤ 3500), rejects 4th (4004 > 3500).
    fmt = lambda m: "x" * 1000
    out, dropped = prompt_budget.trim_prior_messages(msgs, fmt, budget_chars=3_500)
    assert len(out) == 3
    assert dropped == 2


def test_trim_keeps_all_when_budget_ample():
    msgs = [{"i": i} for i in range(3)]
    fmt = lambda m: "x" * 1_000
    out, dropped = prompt_budget.trim_prior_messages(msgs, fmt, budget_chars=1_000_000)
    assert len(out) == 3
    assert dropped == 0


def test_trim_drops_everything_below_min_budget():
    """Below the floor, the marker alone is more honest than a half-truncated turn."""
    msgs = [{"i": i} for i in range(3)]
    fmt = lambda m: "x" * 1_000
    out, dropped = prompt_budget.trim_prior_messages(msgs, fmt, budget_chars=500)
    # 500 is below _MIN_INCLUDE_BUDGET=2_000 (private constant), so everything drops.
    assert out == []
    assert dropped == 3


def test_omitted_marker_singular_vs_plural():
    assert "1 earlier turn" in prompt_budget.omitted_marker(1)
    assert "7 earlier turns" in prompt_budget.omitted_marker(7)


# ---------------------------------------------------------------------------
# Integration with prompt_builder
# ---------------------------------------------------------------------------

def _make_task(mode: TaskMode = TaskMode.CONSULT) -> TaskRequest:
    perms = Permissions(
        can_read_files=True, can_write_files=False, can_run_commands=False,
        can_access_network=False, can_install_packages=False,
        can_apply_patches=False, can_read_env_files=False, can_read_secrets=False,
    )
    limits = Limits(max_rounds=5, timeout_seconds=180, max_seconds=600)
    kwargs: dict = dict(
        protocol_version="1.0",
        source=TaskSource.API,
        mode=mode,
        consultants=["codex", "claude-code", "gemini"] if mode == TaskMode.CONCLAVE else ["claude-code", "gemini"],
        user_request="What is the answer?",
        task_type="general_consultation",
        permissions=perms,
        limits=limits,
    )
    if mode != TaskMode.CONCLAVE:
        kwargs["primary_agent"] = "codex"
    return TaskRequest(**kwargs)


def _make_priors(n: int, content_chars: int = 200) -> list[dict]:
    """Generate n synthetic prior conclave_turn messages, each ~content_chars long."""
    out = []
    for i in range(n):
        out.append({
            "agent": f"agent{i}",
            "role": "participant",
            "message_type": "conclave_turn",
            "summary": f"Turn {i} summary",
            "analysis": "x" * content_chars,
            "position": "x" * content_chars,
            "convergence": "still_thinking",
        })
    return out


def test_primary_prompt_no_ceiling_keeps_all_priors():
    """Backward-compat: ceiling_chars=None means no enforcement."""
    task = _make_task()
    priors = _make_priors(5, content_chars=200)
    prompt = build_primary_prompt(task, "tsk_test", "codex", priors, ceiling_chars=None)
    # All 5 turn summaries should appear.
    for i in range(5):
        assert f"Turn {i} summary" in prompt
    assert "earlier turn" not in prompt  # no marker


def test_primary_prompt_tiny_ceiling_drops_everything_with_marker():
    """A tiny ceiling should drop all priors and insert the marker."""
    task = _make_task()
    priors = _make_priors(5, content_chars=200)
    # Ceiling below overhead means budget=0 for priors → drop all
    tiny = prompt_budget.reserved_overhead() + 1_000
    prompt = build_primary_prompt(task, "tsk_test", "codex", priors, ceiling_chars=tiny)
    assert "5 earlier turns omitted to fit the prompt budget" in prompt
    # No prior turn content survives
    for i in range(5):
        assert f"Turn {i} summary" not in prompt
    # Schema demand still present
    assert "Required Output" in prompt


def test_primary_prompt_partial_trim_keeps_newest():
    """Mid-range ceiling keeps the newest turns and drops the oldest."""
    task = _make_task()
    # Each prior is ~15K formatted (analysis+position dominate); use a budget
    # tight enough that not all 10 fit.
    priors = _make_priors(10, content_chars=15_000)
    ceiling = prompt_budget.reserved_overhead() + 60_000  # only the newest few should survive
    prompt = build_primary_prompt(task, "tsk_test", "codex", priors, ceiling_chars=ceiling)
    # Newer turns should be present, oldest not.
    assert "Turn 9 summary" in prompt
    assert "Turn 0 summary" not in prompt
    # Marker present
    assert "earlier turns omitted" in prompt
    # Schema demand survives
    assert "Required Output" in prompt


def test_consultant_prompt_honors_ceiling():
    task = _make_task()
    priors = _make_priors(8, content_chars=2_500)
    ceiling = prompt_budget.reserved_overhead() + 30_000
    prompt = build_consultant_prompt(task, "tsk_test", "claude-code", priors, ceiling_chars=ceiling)
    assert "earlier turns omitted" in prompt
    # The newest turn must always survive partial trimming
    assert "Turn 7 summary" in prompt


def test_final_prompt_inherits_primary_budget_behavior():
    task = _make_task()
    priors = _make_priors(5, content_chars=200)
    tiny = prompt_budget.reserved_overhead() + 1_000
    prompt = build_final_prompt(task, "tsk_test", "codex", priors, ceiling_chars=tiny)
    # build_final wraps build_primary; same trim should fire.
    assert "5 earlier turns omitted" in prompt
    assert "primary_final" in prompt  # final wrap intact


def test_conclave_prompt_honors_ceiling():
    task = _make_task(mode=TaskMode.CONCLAVE)
    priors = _make_priors(8, content_chars=2_500)
    ceiling = prompt_budget.reserved_overhead() + 30_000
    prompt = build_conclave_prompt(
        task, "tsk_test", "claude-code",
        prior_messages=priors,
        other_participants=["codex", "gemini"],
        ceiling_chars=ceiling,
    )
    assert "earlier turns omitted" in prompt
    assert "Turn 7 summary" in prompt
    assert "Other Participants in This Conclave" in prompt  # structure intact


def test_conclave_prompt_round_one_unaffected():
    """When there are no prior_messages, ceiling enforcement is a no-op and the
    'this is round 1' message still appears."""
    task = _make_task(mode=TaskMode.CONCLAVE)
    prompt = build_conclave_prompt(
        task, "tsk_test", "codex",
        prior_messages=[],
        other_participants=["claude-code"],
        ceiling_chars=10_000,
    )
    assert "this is round 1" in prompt
    assert "earlier turns omitted" not in prompt


def test_peer_prompt_with_ceiling_passes_through():
    """build_peer_prompt has no priors of its own; ceiling is forwarded but has
    nothing to trim."""
    task = _make_task()
    prompt = build_peer_prompt(task, "tsk_test", "codex", ceiling_chars=50_000)
    assert "Required Output" in prompt
    assert "earlier turn" not in prompt


def test_budget_does_not_eat_into_schema_demand():
    """Reserved overhead protects the schema demand block. Even with absurdly
    long priors, the schema demand block survives intact."""
    task = _make_task()
    # 50 priors of 5_000 chars each = 250_000 chars of priors
    priors = _make_priors(50, content_chars=5_000)
    # Ceiling barely above overhead — almost everything should drop
    ceiling = prompt_budget.reserved_overhead() + 5_000
    prompt = build_primary_prompt(task, "tsk_test", "codex", priors, ceiling_chars=ceiling)
    # Schema demand intact
    assert '"protocol_version": "1.0"' in prompt
    assert '"role": "primary"' in prompt
    # The marker is there
    assert "earlier turns omitted" in prompt
