"""Regression tests for the claude-code subscription-cost recording bug.

Before the fix, `claude_adapter._extract_usage_from_claude` unconditionally
recorded the CLI's `total_cost_usd` envelope field as `cost_usd` on
`agent_runs`. In subscription mode that field is a list-price *estimate* the
Pro/Max plan absorbs, not an actual bill — recording it polluted the
dashboard's Usage view with phantom spend.

These tests pin the fix: cost is recorded only when the CLI is in API mode.
"""

from __future__ import annotations

import json

from app.agents import claude_adapter


def _envelope(input_tokens: int, output_tokens: int, total_cost_usd) -> str:
    body: dict = {
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }
    if total_cost_usd is not None:
        body["total_cost_usd"] = total_cost_usd
    return json.dumps(body)


def test_subscription_mode_skips_cost_usd(monkeypatch):
    """The CLI in subscription mode reports a list-price estimate that the
    subscription absorbs. We must NOT record it as `cost_usd`."""
    monkeypatch.setattr(claude_adapter, "_claude_in_subscription_mode", lambda: True)
    out = claude_adapter._extract_usage_from_claude(
        _envelope(input_tokens=100, output_tokens=500, total_cost_usd=0.42)
    )
    assert out["input_tokens"] == 100
    assert out["output_tokens"] == 500
    assert "cost_usd" not in out, (
        "subscription-mode runs must not contribute to spend aggregates"
    )


def test_api_mode_records_cost_usd(monkeypatch):
    """The CLI in API mode IS billed per-token, so the cost figure is the
    bill the user will see. Record it."""
    monkeypatch.setattr(claude_adapter, "_claude_in_subscription_mode", lambda: False)
    out = claude_adapter._extract_usage_from_claude(
        _envelope(input_tokens=100, output_tokens=500, total_cost_usd=0.42)
    )
    assert out["cost_usd"] == 0.42


def test_subscription_mode_without_cost_field_still_records_tokens(monkeypatch):
    """The cost-skip path must not drop the token counts."""
    monkeypatch.setattr(claude_adapter, "_claude_in_subscription_mode", lambda: True)
    out = claude_adapter._extract_usage_from_claude(
        _envelope(input_tokens=100, output_tokens=500, total_cost_usd=None)
    )
    assert out["input_tokens"] == 100
    assert out["output_tokens"] == 500
    assert "cost_usd" not in out


def test_api_mode_without_cost_field_records_tokens_only(monkeypatch):
    """When the envelope lacks `total_cost_usd`, we record tokens but no cost."""
    monkeypatch.setattr(claude_adapter, "_claude_in_subscription_mode", lambda: False)
    out = claude_adapter._extract_usage_from_claude(
        _envelope(input_tokens=100, output_tokens=500, total_cost_usd=None)
    )
    assert out["input_tokens"] == 100
    assert out["output_tokens"] == 500
    assert "cost_usd" not in out


def test_subscription_detector_treats_missing_credentials_as_api(tmp_path, monkeypatch):
    """The detector returns False (API mode) when ~/.claude/.credentials.json
    is absent — i.e. cost would be recorded. Pin this so the default for users
    with no credentials file remains 'record cost'."""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))      # Windows
    monkeypatch.setenv("HOME", str(tmp_path))             # POSIX
    # No .claude/.credentials.json created → subscription mode = False.
    assert claude_adapter._claude_in_subscription_mode() is False


def test_subscription_detector_finds_credentials_file(tmp_path, monkeypatch):
    """And the inverse: when the credentials file is present and non-empty,
    the detector returns True (subscription mode)."""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text(
        '{"some": "oauth-blob"}', encoding="utf-8"
    )
    assert claude_adapter._claude_in_subscription_mode() is True
