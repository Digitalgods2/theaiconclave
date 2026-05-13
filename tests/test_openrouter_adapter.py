"""Tests for the OpenRouter adapter and its config-driven registration.

All HTTP is mocked — no network calls. Covers:
- _invoke: content extraction (OpenAI-shaped choices[0].message.content),
  <think>-block stripping, usage stash (incl. cost when present), error paths,
  the nested-error-in-200 case
- is_available / constructor validation / API-key gating (env + DB fallback)
- data_collection passed through; X-Title header sent
- run_conclave_turn end-to-end against a mocked chat response
- register_openrouter_models adds adapters from config (and no-ops when off)
- _http_error_message produces useful text for 401/402/404/429
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.agents.base import AdapterContext, AdapterError
from app.agents.openrouter_adapter import OpenRouterAdapter, _parse_and_coerce, _http_error_message
from app.protocol.validators import ErrorCode, TaskRequest


# ---------------------------------------------------------------------------
# Fake httpx plumbing (records the last request payload/headers)
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
    last = {"json": None, "headers": None, "url": None}
    def __init__(self, resp=None, raise_exc=None):
        self._resp = resp
        self._raise = raise_exc
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, **kw):
        _FakeClient.last = {"json": kw.get("json"), "headers": kw.get("headers"), "url": url}
        if self._raise: raise self._raise
        return self._resp
    async def get(self, url, **kw):
        if self._raise: raise self._raise
        return self._resp


def _patch_httpx(monkeypatch, resp=None, raise_exc=None):
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _FakeClient(resp, raise_exc))


_VALID_TURN = {
    "convergence": "i_am_done",
    "summary": "Agnosticism is the defensible answer.",
    "position": "No. We cannot determine whether there is a god.",
    "analysis": "Human writings underdetermine the conclusion.",
}


def _chat_body(content: str, prompt_tok=120, completion_tok=60, cost=None):
    usage = {"prompt_tokens": prompt_tok, "completion_tokens": completion_tok}
    if cost is not None:
        usage["cost"] = cost
    return {"id": "gen-1", "choices": [{"message": {"role": "assistant", "content": content},
                                        "finish_reason": "stop"}],
            "usage": usage}


# ---------------------------------------------------------------------------
# Constructor / availability
# ---------------------------------------------------------------------------

def test_constructor_requires_name_and_slug():
    with pytest.raises(ValueError): OpenRouterAdapter(name="", model_slug="x")
    with pytest.raises(ValueError): OpenRouterAdapter(name="x", model_slug="")


def test_data_collection_normalised():
    a = OpenRouterAdapter(name="ds", model_slug="deepseek/deepseek-chat", data_collection="bogus")
    assert a.data_collection == "deny"
    b = OpenRouterAdapter(name="ds", model_slug="deepseek/deepseek-chat", data_collection="allow")
    assert b.data_collection == "allow"


@pytest.mark.asyncio
async def test_is_available_reflects_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    a = OpenRouterAdapter(name="ds", model_slug="m")
    assert await a.is_available() is False
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    assert await a.is_available() is True


@pytest.mark.asyncio
async def test_invoke_without_key_raises_unavailable(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    a = OpenRouterAdapter(name="ds", model_slug="m")
    with pytest.raises(AdapterError) as ei:
        await a._invoke("hi", timeout_seconds=10)
    assert ei.value.code == ErrorCode.AGENT_UNAVAILABLE


# ---------------------------------------------------------------------------
# _invoke happy path + quirks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_extracts_content_usage_and_cost(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _patch_httpx(monkeypatch, resp=_FakeResp(200, _chat_body(json.dumps(_VALID_TURN), 200, 90, cost=0.0031)))
    a = OpenRouterAdapter(name="ds", model_slug="deepseek/deepseek-chat", data_collection="deny")
    text = await a._invoke("prompt", timeout_seconds=30)
    assert json.loads(text) == _VALID_TURN
    assert a._last_usage["input_tokens"] == 200
    assert a._last_usage["output_tokens"] == 90
    assert a._last_usage["cost_usd"] == 0.0031
    # data_collection + X-Title were sent
    assert _FakeClient.last["json"]["provider"] == {"data_collection": "deny"}
    assert _FakeClient.last["headers"]["X-Title"]
    assert _FakeClient.last["json"]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_invoke_strips_think_block(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    content = "<think>step 1\nstep 2</think>\n" + json.dumps(_VALID_TURN)
    _patch_httpx(monkeypatch, resp=_FakeResp(200, _chat_body(content)))
    a = OpenRouterAdapter(name="ds", model_slug="m")
    text = await a._invoke("prompt", timeout_seconds=30)
    assert "<think>" not in text
    assert json.loads(text) == _VALID_TURN


@pytest.mark.asyncio
async def test_invoke_http_402_message(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _patch_httpx(monkeypatch, resp=_FakeResp(402, text='{"error":{"message":"insufficient credits"}}'))
    a = OpenRouterAdapter(name="ds", model_slug="m")
    with pytest.raises(AdapterError) as ei:
        await a._invoke("prompt", timeout_seconds=30)
    assert ei.value.code == ErrorCode.AGENT_ERROR
    assert "credit" in ei.value.message.lower()


@pytest.mark.asyncio
async def test_invoke_timeout(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _patch_httpx(monkeypatch, raise_exc=httpx.TimeoutException("slow"))
    a = OpenRouterAdapter(name="ds", model_slug="m")
    with pytest.raises(AdapterError) as ei:
        await a._invoke("prompt", timeout_seconds=5)
    assert ei.value.code == ErrorCode.AGENT_TIMEOUT


@pytest.mark.asyncio
async def test_invoke_nested_error_in_200(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _patch_httpx(monkeypatch, resp=_FakeResp(200, {"error": {"message": "model is overloaded"}}))
    a = OpenRouterAdapter(name="ds", model_slug="m")
    with pytest.raises(AdapterError) as ei:
        await a._invoke("prompt", timeout_seconds=30)
    assert "overloaded" in ei.value.message


@pytest.mark.asyncio
async def test_invoke_empty_content(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _patch_httpx(monkeypatch, resp=_FakeResp(200, {"choices": [{"message": {"content": "  "}, "finish_reason": "length"}], "usage": {}}))
    a = OpenRouterAdapter(name="ds", model_slug="m")
    with pytest.raises(AdapterError) as ei:
        await a._invoke("prompt", timeout_seconds=30)
    assert ei.value.code == ErrorCode.AGENT_ERROR


# ---------------------------------------------------------------------------
# _http_error_message
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,body,needle", [
    (401, "", "unauthorized"),
    (402, "", "credit"),
    (404, "no endpoints found for model", "model not found"),
    (429, "", "rate limited"),
    (500, "", "HTTP 500"),
])
def test_http_error_message(status, body, needle):
    msg = _http_error_message("deepseek/deepseek-chat", status, body)
    assert needle in msg


def test_http_error_message_context_overflow_extracts_limits():
    body = ('{"error":{"message":"This endpoint\'s maximum context length is 163840 '
            'tokens. However, you requested about 212253 tokens (212253 of text input)."}}')
    msg = _http_error_message("deepseek/deepseek-chat", 400, body)
    assert "163840" in msg and "212253" in msg
    assert "max_context_chars" in msg
    assert "config.yaml" in msg
    # The recommended ceiling should be sensible (well under the real limit).
    assert "417,792" in msg or "417792" in msg.replace(",", "")  # 163840 * 3 * 0.85 = 417,792


def test_http_error_message_context_overflow_without_numbers():
    body = '{"error":{"message":"This endpoint\'s maximum context length is exceeded"}}'
    msg = _http_error_message("deepseek/deepseek-chat", 400, body)
    assert "overflowed" in msg
    assert "max_context_chars" in msg


# ---------------------------------------------------------------------------
# _parse_and_coerce
# ---------------------------------------------------------------------------

def test_parse_and_coerce_fills_envelope():
    data = _parse_and_coerce(json.dumps(_VALID_TURN), "tsk_1", "deepseek",
                             role="participant", default_message_type="conclave_turn")
    assert data["protocol_version"] == "1.0"
    assert data["task_id"] == "tsk_1" and data["agent"] == "deepseek"
    assert data["message_type"] == "conclave_turn"


# ---------------------------------------------------------------------------
# run_conclave_turn end-to-end
# ---------------------------------------------------------------------------

def _minimal_conclave_task(consultants):
    return TaskRequest.model_validate({
        "protocol_version": "1.0", "source": "cli", "mode": "conclave",
        "task_type": "general_consultation", "user_request": "Q?",
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
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _patch_httpx(monkeypatch, resp=_FakeResp(200, _chat_body(json.dumps(_VALID_TURN))))
    a = OpenRouterAdapter(name="deepseek", model_slug="deepseek/deepseek-chat")
    task = _minimal_conclave_task(["deepseek", "codex", "gemini"])
    ctx = AdapterContext(task=task, task_id="tsk_x", prior_messages=[],
                         permissions=task.permissions, timeout_seconds=30, working_directory=".")
    turn = await a.run_conclave_turn(ctx)
    assert turn.agent == "deepseek"
    assert turn.convergence.value == "i_am_done"
    # No sandbox on the task -> the request prompt has no PROJECT FILES section.
    assert "PROJECT FILES" not in _FakeClient.last["json"]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_run_conclave_turn_inlines_sandbox(monkeypatch, tmp_path):
    """When the task carries a project sandbox, the OpenRouter prompt gets the
    read-only file tree + contents appended (this adapter can't browse files)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    sandbox = tmp_path / "proj"
    sandbox.mkdir()
    (sandbox / "main.py").write_text("print('SANDBOX_MARKER_CONTENT')\n", encoding="utf-8")
    _patch_httpx(monkeypatch, resp=_FakeResp(200, _chat_body(json.dumps(_VALID_TURN))))
    a = OpenRouterAdapter(name="deepseek", model_slug="deepseek/deepseek-chat", max_context_chars=400_000)
    task = TaskRequest.model_validate({
        "protocol_version": "1.0", "source": "cli", "mode": "conclave",
        "task_type": "code_review", "user_request": "Examine this project.",
        "primary_agent": None, "consultants": ["deepseek", "codex"],
        "context": {"files": [], "error": None, "git_diff": None, "extra": {"sandbox_path": str(sandbox)}},
        "permissions": {"can_read_files": True, "can_write_files": False, "can_run_commands": False,
                        "can_access_network": False, "can_install_packages": False, "can_apply_patches": False,
                        "can_read_env_files": False, "can_read_secrets": False},
        "limits": {"max_rounds": 5, "timeout_seconds": 180, "max_seconds": 600,
                   "max_context_tokens": None, "convergence_threshold": 1.0},
    })
    ctx = AdapterContext(task=task, task_id="tsk_y", prior_messages=[],
                         permissions=task.permissions, timeout_seconds=30, working_directory=".")
    await a.run_conclave_turn(ctx)
    sent = _FakeClient.last["json"]["messages"][0]["content"]
    assert "PROJECT FILES" in sent
    assert "main.py" in sent
    assert "SANDBOX_MARKER_CONTENT" in sent


# ---------------------------------------------------------------------------
# register_openrouter_models
# ---------------------------------------------------------------------------

class _FakeModel:
    def __init__(self, name, model_slug, max_context_chars=400_000):
        self.name = name; self.model_slug = model_slug; self.max_context_chars = max_context_chars

class _FakeOR:
    def __init__(self, enabled, models, endpoint="https://openrouter.ai/api/v1", data_collection="deny"):
        self.enabled = enabled; self.models = models; self.endpoint = endpoint; self.data_collection = data_collection

class _FakeConfig:
    def __init__(self, orc): self.openrouter = orc


def test_register_openrouter_models_adds_seats():
    from app.services import agent_registry
    agent_registry.clear(); agent_registry.init_registry()
    cfg = _FakeConfig(_FakeOR(True, [
        _FakeModel("deepseek", "deepseek/deepseek-chat", 800_000),
        _FakeModel("glm", "z-ai/glm-4.6"),
    ], data_collection="allow"))
    agent_registry.register_openrouter_models(cfg)
    names = agent_registry.list_names()
    assert "deepseek" in names and "glm" in names
    ds = agent_registry.get("deepseek")
    assert ds.model_slug == "deepseek/deepseek-chat"
    assert ds.max_context_chars == 800_000
    assert ds.data_collection == "allow"


def test_register_openrouter_models_noop_when_disabled():
    from app.services import agent_registry
    agent_registry.clear(); agent_registry.init_registry()
    before = set(agent_registry.list_names())
    agent_registry.register_openrouter_models(_FakeConfig(_FakeOR(False, [_FakeModel("x", "y")])))
    assert set(agent_registry.list_names()) == before
