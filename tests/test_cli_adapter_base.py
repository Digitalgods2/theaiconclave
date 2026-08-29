"""Characterization tests for the shared CLI-adapter run_* behavior.

These were written BEFORE the CliAdapterBase extraction and must pass, byte
for byte, after it. That is the whole point: prior to this file, nothing in
the suite instantiated CodexAdapter / ClaudeCodeAdapter / GeminiAdapter and
drove a run_* method at all. Every orchestrator-level flow test goes through
FakeAdapter, which shares none of this code. Consolidating four byte-identical
copies with zero coverage would have made the refactor itself the first
exercise of the merged logic.

They assert on OBSERVABLE behavior only — the prompt handed to _invoke, and
the validated model that comes back — never on which module a prompt builder
happens to live in. A test that patched `app.agents.codex_adapter.
build_primary_prompt` would have broken the moment that name moved to the
base class, which would have proved nothing about behavior.
"""

from __future__ import annotations

import json

import pytest

from app.agents.antigravity_adapter import AntigravityAdapter
from app.agents.base import AdapterContext, AdapterError
from app.agents.claude_adapter import ClaudeCodeAdapter
from app.agents.codex_adapter import CodexAdapter
from app.agents.gemini_adapter import GeminiAdapter
from app.protocol.validators import (
    ConclaveTurn,
    ConsultantCritique,
    ErrorCode,
    PrimaryResponse,
    TaskRequest,
)

CLI_ADAPTERS = [CodexAdapter, ClaudeCodeAdapter, GeminiAdapter, AntigravityAdapter]
ADAPTER_IDS = ["codex", "claude-code", "gemini", "antigravity"]


def _task(**overrides) -> TaskRequest:
    payload = {
        "protocol_version": "1.0",
        "source": "api",
        "mode": "conclave",
        "task_type": "architecture_review",
        "user_request": "MARKER_USER_REQUEST pick a database",
        "primary_agent": None,
        "consultants": ["codex", "gemini"],
        "context": {"files": [], "error": None, "git_diff": None, "extra": {}},
        "permissions": {
            "can_read_files": True, "can_write_files": False,
            "can_run_commands": False, "can_access_network": False,
            "can_install_packages": False, "can_apply_patches": False,
            "can_read_env_files": False, "can_read_secrets": False,
        },
        "limits": {"max_rounds": 3, "timeout_seconds": 60, "max_seconds": 300},
    }
    payload.update(overrides)
    return TaskRequest.model_validate(payload)


def _ctx(task: TaskRequest | None = None, **overrides) -> AdapterContext:
    task = task or _task()
    kwargs = {
        "task": task,
        "task_id": "tsk_characterization",
        "prior_messages": [],
        "permissions": task.permissions,
        "timeout_seconds": 60,
        "working_directory": ".",
    }
    kwargs.update(overrides)
    return AdapterContext(**kwargs)


def _adapter(cls, tmp_path):
    binary = tmp_path / "cli.exe"
    binary.write_text("")
    return cls(command_path=str(binary))


class _Capture:
    """Stands in for _invoke: records what it was handed, returns canned JSON."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.prompt = None
        self.timeout_seconds = None
        self.image_paths = None
        self.sandbox_path = None

    async def __call__(self, prompt, timeout_seconds, image_paths=None, sandbox_path=None):
        self.prompt = prompt
        self.timeout_seconds = timeout_seconds
        self.image_paths = image_paths
        self.sandbox_path = sandbox_path
        return json.dumps(self.payload)


PRIMARY_PAYLOAD = {"summary": "s", "analysis": "a"}
CRITIQUE_PAYLOAD = {"agreement": "agree", "critique": "c"}
CONCLAVE_PAYLOAD = {
    "summary": "s", "analysis": "a", "position": "p", "convergence": "i_am_done",
}


# ---------------------------------------------------------------------------
# Each run_* builds the right prompt and returns the right model
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls", CLI_ADAPTERS, ids=ADAPTER_IDS)
async def test_run_primary(cls, tmp_path, monkeypatch):
    a = _adapter(cls, tmp_path)
    cap = _Capture(PRIMARY_PAYLOAD)
    monkeypatch.setattr(a, "_invoke", cap)

    result = await a.run_primary(_ctx())

    assert isinstance(result, PrimaryResponse)
    assert result.agent == a.name
    assert result.task_id == "tsk_characterization"
    assert result.role.value == "primary"
    assert result.message_type.value == "primary_proposal"
    assert result.protocol_version == "1.0"
    # the prompt demanded exactly that shape
    assert '"message_type": "primary_proposal"' in cap.prompt
    assert "MARKER_USER_REQUEST" in cap.prompt


@pytest.mark.parametrize("cls", CLI_ADAPTERS, ids=ADAPTER_IDS)
async def test_run_consultant(cls, tmp_path, monkeypatch):
    a = _adapter(cls, tmp_path)
    cap = _Capture(CRITIQUE_PAYLOAD)
    monkeypatch.setattr(a, "_invoke", cap)

    result = await a.run_consultant(_ctx())

    assert isinstance(result, ConsultantCritique)
    assert result.agent == a.name
    assert result.role.value == "consultant"
    assert result.message_type.value == "consultant_critique"
    assert '"role": "consultant"' in cap.prompt


@pytest.mark.parametrize("cls", CLI_ADAPTERS, ids=ADAPTER_IDS)
async def test_run_final(cls, tmp_path, monkeypatch):
    a = _adapter(cls, tmp_path)
    cap = _Capture(PRIMARY_PAYLOAD)
    monkeypatch.setattr(a, "_invoke", cap)

    result = await a.run_final(_ctx())

    assert isinstance(result, PrimaryResponse)
    assert result.message_type.value == "primary_final"
    assert result.role.value == "primary"


@pytest.mark.parametrize("cls", CLI_ADAPTERS, ids=ADAPTER_IDS)
async def test_run_conclave_turn(cls, tmp_path, monkeypatch):
    a = _adapter(cls, tmp_path)
    cap = _Capture(CONCLAVE_PAYLOAD)
    monkeypatch.setattr(a, "_invoke", cap)

    task = _task(consultants=[a.name, "OTHER_PARTICIPANT"])
    result = await a.run_conclave_turn(_ctx(task))

    assert isinstance(result, ConclaveTurn)
    assert result.role.value == "participant"
    assert result.message_type.value == "conclave_turn"
    # other participants are named in the prompt, self is excluded from that list
    assert "OTHER_PARTICIPANT" in cap.prompt


# ---------------------------------------------------------------------------
# Identity coercion — the model's own claims about who it is are overwritten
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls", CLI_ADAPTERS, ids=ADAPTER_IDS)
async def test_identity_fields_are_overwritten(cls, tmp_path, monkeypatch):
    """A model that misreports its own agent/task_id/role must not corrupt the
    transcript — the adapter overwrites those fields unconditionally."""
    a = _adapter(cls, tmp_path)
    lying = dict(
        PRIMARY_PAYLOAD,
        protocol_version="0.1",
        task_id="tsk_WRONG",
        agent="SOME_OTHER_AGENT",
        role="consultant",
    )
    monkeypatch.setattr(a, "_invoke", _Capture(lying))

    result = await a.run_primary(_ctx())

    assert result.agent == a.name
    assert result.task_id == "tsk_characterization"
    assert result.role.value == "primary"
    assert result.protocol_version == "1.0"


@pytest.mark.parametrize("cls", CLI_ADAPTERS, ids=ADAPTER_IDS)
async def test_stringified_null_resolution_status_is_normalized(cls, tmp_path, monkeypatch):
    """Models routinely emit the string "null" rather than JSON null."""
    a = _adapter(cls, tmp_path)
    monkeypatch.setattr(a, "_invoke", _Capture(dict(PRIMARY_PAYLOAD, resolution_status="null")))

    result = await a.run_primary(_ctx())

    assert result.resolution_status is None


@pytest.mark.parametrize("cls", CLI_ADAPTERS, ids=ADAPTER_IDS)
async def test_message_type_defaults_when_model_omits_it(cls, tmp_path, monkeypatch):
    a = _adapter(cls, tmp_path)
    monkeypatch.setattr(a, "_invoke", _Capture(PRIMARY_PAYLOAD))

    result = await a.run_final(_ctx())

    assert result.message_type.value == "primary_final"


# ---------------------------------------------------------------------------
# Context plumbing and failure
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls", CLI_ADAPTERS, ids=ADAPTER_IDS)
async def test_sandbox_path_and_timeout_reach_invoke(cls, tmp_path, monkeypatch):
    a = _adapter(cls, tmp_path)
    cap = _Capture(PRIMARY_PAYLOAD)
    monkeypatch.setattr(a, "_invoke", cap)

    task = _task(context={
        "files": [], "error": None, "git_diff": None,
        "extra": {"sandbox_path": "C:/sbx"},
    })
    await a.run_primary(_ctx(task, timeout_seconds=99))

    assert cap.sandbox_path == "C:/sbx"
    assert cap.timeout_seconds == 99
    assert cap.image_paths == []


@pytest.mark.parametrize("cls", CLI_ADAPTERS, ids=ADAPTER_IDS)
async def test_unparseable_output_raises_agent_error(cls, tmp_path, monkeypatch):
    a = _adapter(cls, tmp_path)

    async def _prose(*args, **kwargs):
        return "I am afraid I cannot comply, here is some prose instead."

    monkeypatch.setattr(a, "_invoke", _prose)

    with pytest.raises(AdapterError) as e:
        await a.run_primary(_ctx())
    assert e.value.code == ErrorCode.AGENT_ERROR


@pytest.mark.parametrize("cls", CLI_ADAPTERS, ids=ADAPTER_IDS)
async def test_adapter_error_from_invoke_propagates(cls, tmp_path, monkeypatch):
    """A transport failure must surface as-is, not be reshaped into a parse error."""
    a = _adapter(cls, tmp_path)

    async def _boom(*args, **kwargs):
        raise AdapterError(ErrorCode.AGENT_TIMEOUT, "timed out")

    monkeypatch.setattr(a, "_invoke", _boom)

    with pytest.raises(AdapterError) as e:
        await a.run_conclave_turn(_ctx())
    assert e.value.code == ErrorCode.AGENT_TIMEOUT
