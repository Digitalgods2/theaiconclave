"""Registry of agent adapters. The orchestrator looks up adapters here."""

from __future__ import annotations

from app.agents.base import BaseAdapter
from app.agents.claude_adapter import ClaudeCodeAdapter
from app.agents.codex_adapter import CodexAdapter
from app.agents.fake_adapter import FakeAdapter
from app.agents.gemini_adapter import GeminiAdapter


_registry: dict[str, BaseAdapter] = {}


def register(adapter: BaseAdapter) -> None:
    _registry[adapter.name] = adapter


def get(name: str) -> BaseAdapter:
    if name not in _registry:
        raise KeyError(f"agent_unavailable: {name}")
    return _registry[name]


def list_names() -> list[str]:
    return list(_registry.keys())


def clear() -> None:
    """Reset the registry. Used by tests."""
    _registry.clear()


def init_registry() -> None:
    """Register all enabled adapters. Called once at startup."""
    register(FakeAdapter())
    register(CodexAdapter())
    register(GeminiAdapter())
    register(ClaudeCodeAdapter())
