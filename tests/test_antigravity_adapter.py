"""Antigravity CLI (`agy`) adapter tests.

The invariants worth pinning here are the ones that cost real debugging to
find against the live binary:

- `-p` must be spelled `-p=` (bare `-p` eats the next flag as its prompt and
  exits 2), and the prompt must travel on stdin, not argv.
- `--mode plan` must always be present — it is the read-only guarantee.
- a failed run still exits 0, so `status` decides success, not the return code.
"""

from __future__ import annotations

import json

import pytest

from app.agents.antigravity_adapter import (
    AntigravityAdapter,
    _extract_result_event,
    _extract_usage,
)
from app.agents.base import AdapterError
from app.protocol.validators import ErrorCode


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _result_event(**overrides) -> dict:
    payload = {
        "conversation_id": "c1",
        "status": "SUCCESS",
        "response": "hello",
        "duration_seconds": 1.0,
        "num_turns": 1,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "thinking_tokens": 5,
            "cache_read_tokens": 0,
            "total_tokens": 120,
        },
    }
    payload.update(overrides)
    return {"event": "result", "result": payload}


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------

def test_prompt_flag_uses_attached_empty_value():
    """Bare `-p` would consume `--input-format` as its prompt (exit 2)."""
    args = AntigravityAdapter()._build_args("agy", 180, [])
    assert "-p=" in args
    assert "-p" not in args


def test_plan_mode_always_present():
    """`--mode plan` is the read-only invariant; it is not configurable away."""
    args = AntigravityAdapter(extra_args=["--sandbox"])._build_args("agy", 60, [])
    assert args[args.index("--mode") + 1] == "plan"


def test_never_passes_disable_slash_commands():
    """`--disable-slash-commands` silently voids `--mode plan` — see adapter docstring."""
    args = AntigravityAdapter()._build_args("agy", 60, [])
    assert "--disable-slash-commands" not in args


def test_stream_json_both_directions():
    args = AntigravityAdapter()._build_args("agy", 60, [])
    assert args[args.index("--input-format") + 1] == "stream-json"
    assert args[args.index("--output-format") + 1] == "stream-json"


def test_timeout_rendered_as_go_duration():
    args = AntigravityAdapter()._build_args("agy", 42, [])
    assert args[args.index("--print-timeout") + 1] == "42s"


def test_model_effort_and_workspace_dirs():
    a = AntigravityAdapter(model="gemini-3.1-pro-high", effort="high")
    args = a._build_args("agy", 60, ["C:/sandbox", "C:/images"])
    assert args[args.index("--model") + 1] == "gemini-3.1-pro-high"
    assert args[args.index("--effort") + 1] == "high"
    assert args.count("--add-dir") == 2


def test_optional_flags_omitted_when_unset():
    args = AntigravityAdapter()._build_args("agy", 60, [])
    assert "--model" not in args
    assert "--effort" not in args
    assert "--add-dir" not in args


def test_extra_args_appended():
    args = AntigravityAdapter(extra_args=["--sandbox"])._build_args("agy", 60, [])
    assert args[-1] == "--sandbox"


# ---------------------------------------------------------------------------
# NDJSON stream parsing
# ---------------------------------------------------------------------------

def test_extracts_result_from_event_stream():
    stdout = _stream(
        {"event": "init", "conversation_id": "c1", "init": {"cwd": "/tmp"}},
        {"event": "step_update", "step_update": {"step_index": 0, "state": "DONE"}},
        _result_event(),
    )
    assert _extract_result_event(stdout)["response"] == "hello"


def test_unparseable_lines_are_skipped():
    """Interleaved diagnostics must not lose a completed turn."""
    stdout = "not json\n" + _stream(_result_event()) + "trailing garbage\n"
    assert _extract_result_event(stdout)["status"] == "SUCCESS"


def test_last_result_event_wins():
    stdout = _stream(_result_event(response="first"), _result_event(response="second"))
    assert _extract_result_event(stdout)["response"] == "second"


def test_missing_result_event_raises():
    stdout = _stream({"event": "init", "init": {}})
    with pytest.raises(AdapterError) as e:
        _extract_result_event(stdout)
    assert e.value.code == ErrorCode.AGENT_ERROR


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------

def test_usage_tokens_extracted():
    usage = _extract_usage(_result_event()["result"])
    assert usage == {"input_tokens": 100, "output_tokens": 20}


def test_usage_never_reports_cost():
    """agy reports no dollar figure and default auth is subscription-billed."""
    result = _result_event()["result"]
    result["usage"]["cost_usd"] = 1.23
    assert "cost_usd" not in _extract_usage(result)


def test_usage_tolerates_missing_block():
    assert _extract_usage({"status": "SUCCESS"}) == {}


# ---------------------------------------------------------------------------
# invocation: stdin payload, status handling
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self._stdout = stdout.encode("utf-8")
        self._stderr = stderr.encode("utf-8")
        self.returncode = returncode
        self.stdin_payload = None

    async def communicate(self, input=None):
        self.stdin_payload = input
        return self._stdout, self._stderr

    def kill(self):  # pragma: no cover - not reached in these tests
        pass


@pytest.fixture
def spawn(monkeypatch):
    """Patch subprocess creation; returns a dict capturing argv and the proc."""
    captured = {}

    def _install(stdout: str, returncode: int = 0, stderr: str = ""):
        proc = _FakeProc(stdout, returncode, stderr)

        async def _fake_exec(*args, **kwargs):
            captured["argv"] = list(args)
            captured["proc"] = proc
            return proc

        monkeypatch.setattr(
            "app.agents.antigravity_adapter.asyncio.create_subprocess_exec",
            _fake_exec,
        )
        return captured

    captured["install"] = _install
    return captured


@pytest.fixture
def adapter(tmp_path):
    binary = tmp_path / "agy.exe"
    binary.write_text("")
    return AntigravityAdapter(command_path=str(binary))


async def test_prompt_delivered_on_stdin_not_argv(adapter, spawn):
    """Conclave prompts exceed the Windows 32k command-line limit."""
    big_prompt = "X" * 50_000
    spawn["install"](_stream(_result_event()))
    await adapter._invoke(big_prompt, 60)

    assert not any(big_prompt in a for a in spawn["argv"])
    payload = json.loads(spawn["proc"].stdin_payload.decode("utf-8"))
    assert payload == {"event": "user", "message": {"content": big_prompt}}


async def test_returns_response_and_stashes_usage(adapter, spawn):
    spawn["install"](_stream(_result_event(response="the answer")))
    text = await adapter._invoke("q", 60)
    assert text == "the answer"
    assert adapter._last_usage == {"input_tokens": 100, "output_tokens": 20}


async def test_error_status_with_exit_zero_raises(adapter, spawn):
    """A failed agy run still exits 0 — status is the authoritative signal."""
    spawn["install"](_stream(_result_event(status="ERROR", response="", error="boom")))
    with pytest.raises(AdapterError) as e:
        await adapter._invoke("q", 60)
    assert e.value.code == ErrorCode.AGENT_ERROR
    assert "boom" in e.value.message


async def test_timeout_status_maps_to_agent_timeout(adapter, spawn):
    spawn["install"](
        _stream(
            _result_event(
                status="ERROR", response="", error="timeout waiting for response"
            )
        )
    )
    with pytest.raises(AdapterError) as e:
        await adapter._invoke("q", 60)
    assert e.value.code == ErrorCode.AGENT_TIMEOUT


async def test_nonzero_exit_raises(adapter, spawn):
    spawn["install"]("", returncode=2, stderr="flag error")
    with pytest.raises(AdapterError) as e:
        await adapter._invoke("q", 60)
    assert e.value.code == ErrorCode.AGENT_ERROR
    assert "exited with code 2" in e.value.message


async def test_empty_response_raises(adapter, spawn):
    spawn["install"](_stream(_result_event(response="   ")))
    with pytest.raises(AdapterError) as e:
        await adapter._invoke("q", 60)
    assert "no .response text" in e.value.message


async def test_sandbox_path_added_to_workspace(adapter, spawn, tmp_path):
    sandbox = tmp_path / "sbx"
    sandbox.mkdir()
    spawn["install"](_stream(_result_event()))
    await adapter._invoke("q", 60, sandbox_path=str(sandbox))
    argv = spawn["argv"]
    assert argv[argv.index("--add-dir") + 1] == str(sandbox.resolve())


async def test_unavailable_when_command_path_missing(tmp_path):
    a = AntigravityAdapter(command_path=str(tmp_path / "nope.exe"))
    with pytest.raises(AdapterError) as e:
        await a._invoke("q", 60)
    assert e.value.code == ErrorCode.AGENT_UNAVAILABLE


# ---------------------------------------------------------------------------
# readiness
# ---------------------------------------------------------------------------

async def test_readiness_ok_with_configured_path(tmp_path):
    binary = tmp_path / "agy.exe"
    binary.write_text("")
    r = await AntigravityAdapter(command_path=str(binary)).readiness()
    assert r.available and r.reason == "ok"


async def test_readiness_configured_path_missing(tmp_path):
    r = await AntigravityAdapter(command_path=str(tmp_path / "missing")).readiness()
    assert not r.available
    assert r.reason == "configured_path_missing"


async def test_readiness_command_not_on_path(monkeypatch):
    monkeypatch.setattr("app.agents.antigravity_adapter.shutil.which", lambda _: None)
    r = await AntigravityAdapter().readiness()
    assert not r.available
    assert r.reason == "command_not_found"
    assert "install.ps1" in r.hint


async def test_registered_in_registry():
    from app.services import agent_registry

    agent_registry.clear()
    agent_registry.init_registry()
    assert isinstance(agent_registry.get("antigravity"), AntigravityAdapter)


# ---------------------------------------------------------------------------
# Pricing-view integration (app/api/agents.py)
# ---------------------------------------------------------------------------

def test_model_slug_strips_effort_suffix():
    """`agy models` folds effort into the slug; OpenRouter prices the model."""
    from app.api.agents import _normalize_cli_model_to_slug

    for raw in ("gemini-3.1-pro-high", "gemini-3.1-pro-low", "gemini-3.5-flash-medium"):
        assert not _normalize_cli_model_to_slug("antigravity", raw).endswith(
            ("-high", "-low", "-medium")
        )
    assert (
        _normalize_cli_model_to_slug("antigravity", "gemini-3.1-pro-high")
        == "google/gemini-3.1-pro"
    )


def test_auth_mode_defaults_to_subscription(monkeypatch, tmp_path):
    """Google-account auth lives in the OS keyring; absence of the API-key
    setting means the subscription is paying, so no spend is recorded."""
    from pathlib import Path

    import app.api.agents as agents_api

    settings = tmp_path / ".gemini" / "antigravity-cli"
    settings.mkdir(parents=True)
    (settings / "settings.json").write_text('{"colorScheme": "dark"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    mode, _ = agents_api._detect_cli_auth_mode("antigravity")
    assert mode == "subscription"


def test_auth_mode_api_when_model_provider_set(monkeypatch, tmp_path):
    from pathlib import Path

    import app.api.agents as agents_api

    settings = tmp_path / ".gemini" / "antigravity-cli"
    settings.mkdir(parents=True)
    (settings / "settings.json").write_text(
        '{"modelProvider": "gemini"}', encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    mode, source = agents_api._detect_cli_auth_mode("antigravity")
    assert mode == "api"
    assert "modelProvider=gemini" in source
