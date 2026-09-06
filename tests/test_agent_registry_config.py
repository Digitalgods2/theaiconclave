"""Configuration-driven static seat registration."""

from app.config import AgentConfig, Config
from app.services import agent_registry


def test_disabled_static_seats_are_not_registered_when_configured():
    agent_registry.clear()
    config = Config(agents={
        "codex": AgentConfig(enabled=False),
        "gemini": AgentConfig(enabled=True),
    })

    agent_registry.init_registry(config)

    assert agent_registry.list_names() == ["fake", "gemini"]


def test_configured_claude_model_is_wired_to_adapter():
    agent_registry.clear()
    config = Config(agents={
        "claude-code": AgentConfig(enabled=True, model="claude-sonnet-4-6"),
    })

    agent_registry.init_registry(config)

    assert agent_registry.get("claude-code")._model == "claude-sonnet-4-6"


def test_no_config_preserves_all_static_seats():
    agent_registry.clear()
    agent_registry.init_registry()

    assert {"fake", "codex", "gemini", "claude-code", "antigravity"} <= set(
        agent_registry.list_names()
    )
