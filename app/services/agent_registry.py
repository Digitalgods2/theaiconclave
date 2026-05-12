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
    """Register the static CLI adapters. Called once at startup.

    Ollama-Cloud-backed seats are config-driven and registered separately via
    register_ollama_cloud_models() so that tests (which call init_registry()
    with no config) don't pull in network-backed adapters.
    """
    register(FakeAdapter())
    register(CodexAdapter())
    register(GeminiAdapter())
    register(ClaudeCodeAdapter())


def register_ollama_cloud_models(config) -> None:
    """Register one OllamaCloudAdapter per enabled model in config.ollama_cloud.

    No-op if the section is disabled or empty. Idempotent enough for repeated
    calls (re-registers under the same name). Imported lazily so the adapter
    module (and httpx) isn't a hard import for code paths that don't use it.
    """
    oc = getattr(config, "ollama_cloud", None)
    if oc is None or not getattr(oc, "enabled", False):
        return
    models = getattr(oc, "models", None) or []
    if not models:
        return
    from app.agents.ollama_adapter import OllamaCloudAdapter
    endpoint = getattr(oc, "endpoint", "https://ollama.com")
    for m in models:
        register(OllamaCloudAdapter(
            name=m.name,
            model_id=m.model_id,
            max_context_chars=getattr(m, "max_context_chars", 400_000),
            endpoint=endpoint,
        ))


def register_openrouter_models(config) -> None:
    """Register one OpenRouterAdapter per enabled model in config.openrouter.

    No-op if the section is disabled or empty. Imported lazily so the adapter
    module (and httpx) isn't a hard import for code paths that don't use it.
    """
    orc = getattr(config, "openrouter", None)
    if orc is None or not getattr(orc, "enabled", False):
        return
    models = getattr(orc, "models", None) or []
    if not models:
        return
    from app.agents.openrouter_adapter import OpenRouterAdapter
    endpoint = getattr(orc, "endpoint", "https://openrouter.ai/api/v1")
    data_collection = getattr(orc, "data_collection", "deny")
    for m in models:
        register(OpenRouterAdapter(
            name=m.name,
            model_slug=m.model_slug,
            max_context_chars=getattr(m, "max_context_chars", 400_000),
            endpoint=endpoint,
            data_collection=data_collection,
        ))
