"""Tests for the Ollama Cloud adapter and its config-driven registration.

All HTTP is mocked — no network calls. Covers:
- _invoke: content extraction, <think>-block stripping, usage stash, error paths
- is_available / constructor validation / API-key gating
- run_conclave_turn end-to-end against a mocked chat response
- register_ollama_cloud_models adds adapters from config (and no-ops when off)
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.agents.base import AdapterContext, AdapterError
from app.agents.ollama_adapter import OllamaCloudAdapter, _parse_and_coerce
from app.protocol.validators import ErrorCode, Permissions, TaskRequest


# ---------------------------------------------------------------------------
# Fake httpx plumbing
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body
        self.text = text if text else (json.dumps(json_body) if json_body is not None else "")

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class _FakeClient:
    """Stands in for httpx.AsyncClient as an async context manager."""
    def __init__(self, resp=None, raise_exc=None):
        self._resp = resp
        self._raise = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        if self._raise:
            raise self._raise
        return self._resp

    async def get(self, url, **kw):
        if self._raise:
            raise self._raise
        return self._resp


def _patch_httpx(monkeypatch, resp=None, raise_exc=None):
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _FakeClient(resp, raise_exc))


_VALID_TURN = {
    "convergence": "i_am_done",
    "summary": "Agnosticism is the defensible answer.",
    "position": "No. We cannot determine whether there is a god.",
    "analysis": "Human writings underdetermine the conclusion.",
}


def _chat_body(content: str, prompt_tok=120, eval_tok=60):
    return {"message": {"role": "assistant", "content": content},
            "prompt_eval_count": prompt_tok, "eval_count": eval_tok}


# ---------------------------------------------------------------------------
# Constructor / availability
# ---------------------------------------------------------------------------

def test_constructor_requires_name_and_model_id():
    with pytest.raises(ValueError):
        OllamaCloudAdapter(name="", model_id="x")
    with pytest.raises(ValueError):
        OllamaCloudAdapter(name="x", model_id="")


def test_name_and_model_id_are_per_instance():
    a = OllamaCloudAdapter(name="deepseek", model_id="deepseek-v3.1:671b-cloud")
    b = OllamaCloudAdapter(name="glm", model_id="glm-5:cloud")
    assert a.name == "deepseek" and a.model_id == "deepseek-v3.1:671b-cloud"
    assert b.name == "glm" and b.model_id == "glm-5:cloud"
    assert not a.internal


@pytest.mark.asyncio
async def test_is_available_reflects_api_key(monkeypatch):
    a = OllamaCloudAdapter(name="deepseek", model_id="m")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    assert await a.is_available() is False
    monkeypatch.setenv("OLLAMA_API_KEY", "  ")  # whitespace-only counts as unset
    assert await a.is_available() is False
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-test")
    assert await a.is_available() is True


@pytest.mark.asyncio
async def test_invoke_without_key_raises_unavailable(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    a = OllamaCloudAdapter(name="deepseek", model_id="m")
    with pytest.raises(AdapterError) as ei:
        await a._invoke("hello", timeout_seconds=10)
    assert ei.value.code == ErrorCode.AGENT_UNAVAILABLE


# ---------------------------------------------------------------------------
# _invoke happy path + parsing quirks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_extracts_content_and_stashes_usage(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-test")
    content = json.dumps(_VALID_TURN)
    _patch_httpx(monkeypatch, resp=_FakeResp(200, _chat_body(content, 200, 99)))
    a = OllamaCloudAdapter(name="deepseek", model_id="m")
    text = await a._invoke("prompt", timeout_seconds=30)
    assert json.loads(text) == _VALID_TURN
    assert a._last_usage == {"input_tokens": 200, "output_tokens": 99}


@pytest.mark.asyncio
async def test_invoke_strips_think_block(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-test")
    content = "<think>Let me reason about this carefully...\nmany lines\n</think>\n" + json.dumps(_VALID_TURN)
    _patch_httpx(monkeypatch, resp=_FakeResp(200, _chat_body(content)))
    a = OllamaCloudAdapter(name="glm", model_id="m")
    text = await a._invoke("prompt", timeout_seconds=30)
    assert "<think>" not in text
    assert json.loads(text) == _VALID_TURN


@pytest.mark.asyncio
async def test_invoke_http_error_raises_agent_error(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-test")
    _patch_httpx(monkeypatch, resp=_FakeResp(500, text="upstream boom"))
    a = OllamaCloudAdapter(name="deepseek", model_id="m")
    with pytest.raises(AdapterError) as ei:
        await a._invoke("prompt", timeout_seconds=30)
    assert ei.value.code == ErrorCode.AGENT_ERROR


@pytest.mark.asyncio
async def test_invoke_timeout_raises_agent_timeout(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-test")
    _patch_httpx(monkeypatch, raise_exc=httpx.TimeoutException("slow"))
    a = OllamaCloudAdapter(name="deepseek", model_id="m")
    with pytest.raises(AdapterError) as ei:
        await a._invoke("prompt", timeout_seconds=5)
    assert ei.value.code == ErrorCode.AGENT_TIMEOUT


@pytest.mark.asyncio
async def test_invoke_empty_content_raises(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-test")
    _patch_httpx(monkeypatch, resp=_FakeResp(200, {"message": {"content": "  "}}))
    a = OllamaCloudAdapter(name="deepseek", model_id="m")
    with pytest.raises(AdapterError) as ei:
        await a._invoke("prompt", timeout_seconds=30)
    assert ei.value.code == ErrorCode.AGENT_ERROR


# ---------------------------------------------------------------------------
# _parse_and_coerce
# ---------------------------------------------------------------------------

def test_parse_and_coerce_fills_envelope_fields():
    data = _parse_and_coerce(json.dumps(_VALID_TURN), "tsk_1", "deepseek",
                             role="participant", default_message_type="conclave_turn")
    assert data["protocol_version"] == "1.0"
    assert data["task_id"] == "tsk_1"
    assert data["agent"] == "deepseek"
    assert data["role"] == "participant"
    assert data["message_type"] == "conclave_turn"


def test_parse_and_coerce_normalizes_string_null_resolution_status():
    body = dict(_VALID_TURN, resolution_status="null")
    data = _parse_and_coerce(json.dumps(body), "tsk_1", "glm",
                             role="participant", default_message_type="conclave_turn")
    assert data["resolution_status"] is None


def test_parse_and_coerce_raises_on_garbage():
    with pytest.raises(AdapterError):
        _parse_and_coerce("not json at all", "tsk_1", "qwen",
                          role="participant", default_message_type="conclave_turn")


# ---------------------------------------------------------------------------
# run_conclave_turn end-to-end (mocked HTTP)
# ---------------------------------------------------------------------------

def _minimal_conclave_task(consultants):
    return TaskRequest.model_validate({
        "protocol_version": "1.0", "source": "cli", "mode": "conclave",
        "task_type": "general_consultation", "user_request": "Is there a god?",
        "primary_agent": None, "consultants": consultants,
        "context": {"files": [], "error": None, "git_diff": None, "extra": {}},
        "permissions": {"can_read_files": True, "can_write_files": False, "can_run_commands": False,
                        "can_access_network": False, "can_install_packages": False, "can_apply_patches": False,
                        "can_read_env_files": False, "can_read_secrets": False},
        "limits": {"max_rounds": 5, "timeout_seconds": 180, "max_seconds": 600,
                   "max_context_tokens": None, "convergence_threshold": 1.0},
    })


@pytest.mark.asyncio
async def test_run_conclave_turn_parses_mocked_response(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-test")
    _patch_httpx(monkeypatch, resp=_FakeResp(200, _chat_body(json.dumps(_VALID_TURN))))
    a = OllamaCloudAdapter(name="deepseek", model_id="deepseek-v3.1:671b-cloud")
    task = _minimal_conclave_task(["deepseek", "codex", "gemini"])
    ctx = AdapterContext(task=task, task_id="tsk_x", prior_messages=[],
                         permissions=task.permissions, timeout_seconds=30,
                         working_directory=".")
    turn = await a.run_conclave_turn(ctx)
    assert turn.agent == "deepseek"
    assert turn.convergence.value == "i_am_done"
    assert "cannot determine" in turn.position.lower()


# ---------------------------------------------------------------------------
# register_ollama_cloud_models
# ---------------------------------------------------------------------------

class _FakeModel:
    def __init__(self, name, model_id, max_context_chars=400_000):
        self.name = name
        self.model_id = model_id
        self.max_context_chars = max_context_chars


class _FakeOC:
    def __init__(self, enabled, models, endpoint="https://ollama.com"):
        self.enabled = enabled
        self.models = models
        self.endpoint = endpoint


class _FakeConfig:
    def __init__(self, oc):
        self.ollama_cloud = oc


def test_register_ollama_cloud_models_adds_seats():
    from app.services import agent_registry
    agent_registry.clear()
    agent_registry.init_registry()
    cfg = _FakeConfig(_FakeOC(True, [
        _FakeModel("deepseek", "deepseek-v3.1:671b-cloud", 800_000),
        _FakeModel("glm", "glm-5:cloud"),
    ]))
    agent_registry.register_ollama_cloud_models(cfg)
    names = agent_registry.list_names()
    assert "deepseek" in names and "glm" in names
    assert agent_registry.get("deepseek").max_context_chars == 800_000
    assert agent_registry.get("deepseek").model_id == "deepseek-v3.1:671b-cloud"


def test_register_ollama_cloud_models_noop_when_disabled():
    from app.services import agent_registry
    agent_registry.clear()
    agent_registry.init_registry()
    before = set(agent_registry.list_names())
    agent_registry.register_ollama_cloud_models(_FakeConfig(_FakeOC(False, [_FakeModel("x", "y")])))
    assert set(agent_registry.list_names()) == before


def test_register_ollama_cloud_models_noop_when_empty():
    from app.services import agent_registry
    agent_registry.clear()
    agent_registry.init_registry()
    before = set(agent_registry.list_names())
    agent_registry.register_ollama_cloud_models(_FakeConfig(_FakeOC(True, [])))
    assert set(agent_registry.list_names()) == before
