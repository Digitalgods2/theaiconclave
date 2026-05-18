"""Tests for per-adapter readiness reporting (DR0017).

Each CLI adapter reports a structured `Readiness` covering three cases:
- `ok` — command resolves
- `command_not_found` — fell back to PATH and nothing was there
- `configured_path_missing` — `command_path` was set but points to nothing
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.agents.base import Readiness
from app.agents.claude_adapter import ClaudeCodeAdapter
from app.agents.codex_adapter import CodexAdapter
from app.agents.gemini_adapter import GeminiAdapter


@pytest.fixture
def real_binary(tmp_path):
    """Create a file we can point command_path at."""
    binary = tmp_path / "fake-bin"
    binary.write_text("#!/bin/sh\necho 0.0\n", encoding="utf-8")
    return binary


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

async def test_codex_readiness_ok_with_configured_path(real_binary):
    a = CodexAdapter(command_path=str(real_binary))
    r = await a.readiness()
    assert r.available is True
    assert r.reason == "ok"


async def test_codex_readiness_configured_path_missing(tmp_path):
    a = CodexAdapter(command_path=str(tmp_path / "does-not-exist"))
    r = await a.readiness()
    assert r.available is False
    assert r.reason == "configured_path_missing"
    assert "does-not-exist" in r.hint


async def test_codex_readiness_command_not_on_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    a = CodexAdapter()
    r = await a.readiness()
    assert r.available is False
    assert r.reason == "command_not_found"
    assert "Codex" in r.hint
    assert "command_path" in r.hint


async def test_codex_readiness_command_on_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/codex")
    a = CodexAdapter()
    r = await a.readiness()
    assert r.available is True
    assert r.reason == "ok"


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

async def test_claude_readiness_ok_with_configured_path(real_binary):
    a = ClaudeCodeAdapter(command_path=str(real_binary))
    r = await a.readiness()
    assert r.available is True
    assert r.reason == "ok"


async def test_claude_readiness_configured_path_missing(tmp_path):
    a = ClaudeCodeAdapter(command_path=str(tmp_path / "ghost"))
    r = await a.readiness()
    assert r.available is False
    assert r.reason == "configured_path_missing"


async def test_claude_readiness_command_not_on_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    a = ClaudeCodeAdapter()
    r = await a.readiness()
    assert r.available is False
    assert r.reason == "command_not_found"
    assert "Claude" in r.hint


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

async def test_gemini_readiness_ok_with_configured_path(real_binary):
    a = GeminiAdapter(command_path=str(real_binary))
    r = await a.readiness()
    assert r.available is True
    assert r.reason == "ok"


async def test_gemini_readiness_configured_path_missing(tmp_path):
    a = GeminiAdapter(command_path=str(tmp_path / "missing"))
    r = await a.readiness()
    assert r.available is False
    assert r.reason == "configured_path_missing"


async def test_gemini_readiness_command_not_on_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    a = GeminiAdapter()
    r = await a.readiness()
    assert r.available is False
    assert r.reason == "command_not_found"


# ---------------------------------------------------------------------------
# Resolve precedence: command_path wins over PATH
# ---------------------------------------------------------------------------

async def test_resolve_command_prefers_command_path_over_path(monkeypatch, real_binary):
    """If both command_path is valid AND shutil.which returns a path, the
    configured command_path takes precedence."""
    monkeypatch.setattr(shutil, "which", lambda _: "/should/not/be/used")
    a = CodexAdapter(command_path=str(real_binary))
    assert a._resolve_command() == str(real_binary)


async def test_resolve_command_invalid_path_does_not_fall_back_to_path(monkeypatch, tmp_path):
    """If command_path is set but invalid, the adapter does NOT silently fall
    back to PATH — that would mask a configuration mistake."""
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/codex")
    a = CodexAdapter(command_path=str(tmp_path / "missing"))
    assert a._resolve_command() is None


# ---------------------------------------------------------------------------
# BaseAdapter default readiness — wraps is_available
# ---------------------------------------------------------------------------

async def test_default_readiness_wraps_is_available():
    """The default readiness() impl in BaseAdapter wraps is_available with
    generic hint text. Adapters that don't override should still get a usable
    Readiness back."""
    from app.agents.fake_adapter import FakeAdapter
    a = FakeAdapter()
    r = await a.readiness()
    assert isinstance(r, Readiness)
    # Fake is always available; reason should be "ok".
    assert r.available is True
    assert r.reason == "ok"
