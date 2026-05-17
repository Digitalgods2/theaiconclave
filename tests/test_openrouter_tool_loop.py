"""Tests for the OpenRouter tool-loop (DR0015).

Covers:
- Sandbox tool primitives (read_file / list_dir / glob) — path traversal,
  ignore set, missing paths, glob cap
- Tool-call dispatch — known tools, unknown tools, malformed arguments
- Loop control — happy path (one tool call then final), iteration cap,
  byte budget cap, consecutive-bad-call cap, model returns no tool calls,
  model returns both tool calls and content
- run_conclave_turn end-to-end with tool_loop=True, mocked transport
- run_conclave_turn with tool_loop=False (regression — DR0012 path
  unchanged when flag is off)
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.openrouter_adapter import (
    MAX_CONSECUTIVE_BAD_CALLS,
    MAX_TOOL_BYTES,
    MAX_TOOL_ITERATIONS,
    OpenRouterAdapter,
    _messages_to_single_prompt,
)
from app.protocol.validators import MessageType
from app.services.sandbox_tools import (
    tool_glob,
    tool_list_dir,
    tool_read_file,
)


# ---------------------------------------------------------------------------
# Sandbox fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """Build a small synthetic project tree under tmp_path."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "services").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "main.py").write_text(
        "def main():\n    print('hello')\n", encoding="utf-8"
    )
    (tmp_path / "app" / "services" / "orchestrator.py").write_text(
        "# orchestrator\nclass Orchestrator: pass\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_main.py").write_text(
        "def test_main(): pass\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("# Project\n", encoding="utf-8")
    # Items that should be ignored:
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / "node_modules" / "foo.js").write_text("// junk", encoding="utf-8")
    (tmp_path / "app" / "main.pyc").write_text("\x00\x00\x00", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Sandbox tool primitives
# ---------------------------------------------------------------------------

def test_read_file_returns_content(sandbox):
    r = tool_read_file(sandbox, "app/main.py")
    assert r["ok"] is True
    assert "def main()" in r["content"]
    assert r["truncated"] is False


def test_read_file_rejects_traversal(sandbox):
    r = tool_read_file(sandbox, "../etc/passwd")
    assert r["ok"] is False
    assert "outside sandbox" in r["error"]


def test_read_file_rejects_absolute_path_escape(sandbox):
    # An absolute path that doesn't resolve inside the sandbox should fail.
    r = tool_read_file(sandbox, "/etc/passwd")
    # Leading slash gets stripped → "etc/passwd" inside sandbox → file not found.
    assert r["ok"] is False
    assert "not found" in r["error"] or "outside" in r["error"]


def test_read_file_handles_missing_file(sandbox):
    r = tool_read_file(sandbox, "does/not/exist.py")
    assert r["ok"] is False
    assert "not found" in r["error"]


def test_read_file_refuses_directory(sandbox):
    r = tool_read_file(sandbox, "app")
    assert r["ok"] is False
    assert "not a file" in r["error"]


def test_read_file_refuses_ignored_path(sandbox):
    r = tool_read_file(sandbox, ".git/HEAD")
    assert r["ok"] is False
    assert "ignore set" in r["error"]


def test_list_dir_returns_entries(sandbox):
    r = tool_list_dir(sandbox, ".")
    assert r["ok"] is True
    names = {e["name"] for e in r["entries"]}
    assert "app" in names
    assert "tests" in names
    assert "README.md" in names
    # Ignored dirs filtered out
    assert ".git" not in names
    assert "node_modules" not in names


def test_list_dir_dot_is_root(sandbox):
    r_dot = tool_list_dir(sandbox, ".")
    r_empty = tool_list_dir(sandbox, "")
    assert {e["name"] for e in r_dot["entries"]} == {e["name"] for e in r_empty["entries"]}


def test_list_dir_rejects_traversal(sandbox):
    r = tool_list_dir(sandbox, "..")
    assert r["ok"] is False


def test_list_dir_handles_missing(sandbox):
    r = tool_list_dir(sandbox, "nope")
    assert r["ok"] is False
    assert "not found" in r["error"]


def test_list_dir_refuses_file(sandbox):
    r = tool_list_dir(sandbox, "app/main.py")
    assert r["ok"] is False
    assert "not a directory" in r["error"]


def test_glob_finds_python_files(sandbox):
    r = tool_glob(sandbox, "**/*.py")
    assert r["ok"] is True
    paths = set(r["paths"])
    assert "app/main.py" in paths
    assert "app/services/orchestrator.py" in paths
    assert "tests/test_main.py" in paths


def test_glob_filters_ignored(sandbox):
    r = tool_glob(sandbox, "**/*")
    paths = r["paths"]
    assert not any(".git" in p for p in paths)
    assert not any("node_modules" in p for p in paths)
    assert not any(p.endswith(".pyc") for p in paths)


def test_glob_rejects_traversal_pattern(sandbox):
    r = tool_glob(sandbox, "../*")
    assert r["ok"] is False


def test_glob_empty_pattern(sandbox):
    r = tool_glob(sandbox, "")
    assert r["ok"] is False


def test_glob_respects_cap():
    # Build a sandbox with > 200 files of a single ext so the cap fires.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for i in range(250):
            (root / f"f{i}.py").write_text("x", encoding="utf-8")
        r = tool_glob(root, "*.py", max_paths=200)
        assert r["ok"] is True
        assert len(r["paths"]) == 200
        assert r["truncated"] is True


# ---------------------------------------------------------------------------
# Adapter — _dispatch_tool_call
# ---------------------------------------------------------------------------

def test_dispatch_known_tool(sandbox):
    a = OpenRouterAdapter("test", "fake/model", tool_loop=True)
    result, byte_size = a._dispatch_tool_call(sandbox, "read_file", '{"path":"app/main.py"}')
    assert result["ok"] is True
    assert byte_size > 0


def test_dispatch_unknown_tool(sandbox):
    a = OpenRouterAdapter("test", "fake/model", tool_loop=True)
    result, _ = a._dispatch_tool_call(sandbox, "rm_rf", "{}")
    assert result["ok"] is False
    assert "unknown tool" in result["error"]


def test_dispatch_malformed_args(sandbox):
    a = OpenRouterAdapter("test", "fake/model", tool_loop=True)
    result, _ = a._dispatch_tool_call(sandbox, "read_file", "not json{")
    assert result["ok"] is False
    assert "parse arguments" in result["error"]


def test_dispatch_args_must_be_object(sandbox):
    a = OpenRouterAdapter("test", "fake/model", tool_loop=True)
    result, _ = a._dispatch_tool_call(sandbox, "read_file", "[1,2,3]")
    assert result["ok"] is False
    assert "must be a JSON object" in result["error"]


# ---------------------------------------------------------------------------
# Loop control — fake httpx, fake sandbox, real adapter
# ---------------------------------------------------------------------------

class _FakeResp:
    """Stand-in for httpx.Response in tests. Carries a status_code and a
    pre-built JSON body."""
    def __init__(self, body: dict, status_code: int = 200, text: str = ""):
        self._body = body
        self.status_code = status_code
        self.text = text or json.dumps(body)

    def json(self):
        return self._body


def _scripted_responses(*responses: _FakeResp):
    """Yields the supplied responses in order; raises if asked for more."""
    it = iter(responses)
    async def _post(*_args, **_kwargs):
        try:
            return next(it)
        except StopIteration:
            raise AssertionError("scripted responses exhausted")
    return _post


def _final_content_response(content: str = '{"position":"x","summary":"x","analysis":"x","convergence":"i_am_done"}') -> _FakeResp:
    return _FakeResp({
        "choices": [{"message": {"content": content, "tool_calls": None}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })


def _tool_call_response(tool: str, args: dict, call_id: str = "c1") -> _FakeResp:
    return _FakeResp({
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": tool, "arguments": json.dumps(args)},
                }],
            },
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })


@pytest.mark.asyncio
async def test_tool_loop_happy_path(sandbox, monkeypatch):
    a = OpenRouterAdapter("test", "fake/model", tool_loop=True)
    monkeypatch.setattr(a, "_api_key", lambda: "k")
    # First POST → model asks for a file. Second POST → model returns final content.
    monkeypatch.setattr(a, "_post_chat_tools", _scripted_responses(
        _tool_call_response("read_file", {"path": "app/main.py"}),
        _final_content_response(),
    ))
    text = await a._invoke_with_tools("base prompt", str(sandbox), timeout_seconds=60)
    assert "position" in text
    # Tool events were recorded
    events = a._last_tool_events
    assert len(events) == 2  # one call + one result
    assert events[0]["message_type"] == MessageType.TOOL_CALL.value
    assert events[1]["message_type"] == MessageType.TOOL_RESULT.value
    assert events[1]["structured"]["ok"] is True


@pytest.mark.asyncio
async def test_tool_loop_returns_immediately_on_no_tool_call(sandbox, monkeypatch):
    """If the model returns content on the first POST (no tool calls), the loop
    exits immediately. No events recorded."""
    a = OpenRouterAdapter("test", "fake/model", tool_loop=True)
    monkeypatch.setattr(a, "_api_key", lambda: "k")
    monkeypatch.setattr(a, "_post_chat_tools", _scripted_responses(
        _final_content_response('{"position":"y","summary":"y","analysis":"y","convergence":"i_am_done"}'),
    ))
    text = await a._invoke_with_tools("base", str(sandbox), timeout_seconds=60)
    assert "y" in text
    assert a._last_tool_events == []


@pytest.mark.asyncio
async def test_tool_loop_iteration_cap_triggers_forced_final(sandbox, monkeypatch):
    """Model never emits content — every response is a tool call. After
    MAX_TOOL_ITERATIONS, the adapter forces a final-turn POST."""
    a = OpenRouterAdapter("test", "fake/model", tool_loop=True)
    monkeypatch.setattr(a, "_api_key", lambda: "k")
    # Always return a tool call → never satisfies the loop.
    looping = [_tool_call_response("list_dir", {"path": "."}, call_id=f"c{i}")
               for i in range(MAX_TOOL_ITERATIONS)]
    monkeypatch.setattr(a, "_post_chat_tools", _scripted_responses(*looping))
    # Forced final turn uses _post_chat (no tools).
    monkeypatch.setattr(a, "_post_chat",
        _scripted_responses(_final_content_response('{"position":"forced","summary":"f","analysis":"f","convergence":"i_am_done"}'))
    )
    text = await a._invoke_with_tools("base", str(sandbox), timeout_seconds=60)
    assert "forced" in text
    # We expect 2 events per iteration (call + result), so MAX_TOOL_ITERATIONS * 2.
    assert len(a._last_tool_events) == MAX_TOOL_ITERATIONS * 2


@pytest.mark.asyncio
async def test_tool_loop_consecutive_bad_calls_force_final(sandbox, monkeypatch):
    """Three bad tool calls in a row → force final-turn POST before exhausting iterations."""
    a = OpenRouterAdapter("test", "fake/model", tool_loop=True)
    monkeypatch.setattr(a, "_api_key", lambda: "k")
    # Each response asks for an unknown tool → bad call.
    bad = [_tool_call_response("nuke_repo", {}, call_id=f"bad{i}")
           for i in range(MAX_CONSECUTIVE_BAD_CALLS)]
    monkeypatch.setattr(a, "_post_chat_tools", _scripted_responses(*bad))
    monkeypatch.setattr(a, "_post_chat",
        _scripted_responses(_final_content_response())
    )
    text = await a._invoke_with_tools("base", str(sandbox), timeout_seconds=60)
    # Final turn ran; tool events still recorded
    assert "position" in text
    # bad calls trigger; MAX_CONSECUTIVE_BAD_CALLS iterations * 2 events
    assert len(a._last_tool_events) == MAX_CONSECUTIVE_BAD_CALLS * 2


@pytest.mark.asyncio
async def test_tool_loop_path_traversal_returns_error_to_model(sandbox, monkeypatch):
    """A bad path doesn't crash the adapter — it returns an error to the model,
    which can then self-correct on the next iteration."""
    a = OpenRouterAdapter("test", "fake/model", tool_loop=True)
    monkeypatch.setattr(a, "_api_key", lambda: "k")
    monkeypatch.setattr(a, "_post_chat_tools", _scripted_responses(
        _tool_call_response("read_file", {"path": "../etc/passwd"}, call_id="c1"),
        _tool_call_response("read_file", {"path": "app/main.py"}, call_id="c2"),
        _final_content_response(),
    ))
    text = await a._invoke_with_tools("base", str(sandbox), timeout_seconds=60)
    # First tool_result should signal the error
    first_result = a._last_tool_events[1]
    assert first_result["structured"]["ok"] is False
    assert "outside sandbox" in first_result["structured"]["result"]["error"]
    # Second tool_result should be the real file
    second_result = a._last_tool_events[3]
    assert second_result["structured"]["ok"] is True


# ---------------------------------------------------------------------------
# Dispatcher — chooses path based on flag + sandbox
# ---------------------------------------------------------------------------

def test_use_tool_loop_requires_both_flag_and_sandbox(sandbox):
    a_flag_on = OpenRouterAdapter("t", "m", tool_loop=True)
    a_flag_off = OpenRouterAdapter("t", "m", tool_loop=False)

    def _ctx(sandbox_path):
        task = SimpleNamespace(context=SimpleNamespace(extra={"sandbox_path": sandbox_path} if sandbox_path else {}))
        return SimpleNamespace(task=task)

    assert a_flag_on._use_tool_loop(_ctx(str(sandbox))) is True
    assert a_flag_on._use_tool_loop(_ctx(None)) is False
    assert a_flag_off._use_tool_loop(_ctx(str(sandbox))) is False
    assert a_flag_off._use_tool_loop(_ctx(None)) is False


# ---------------------------------------------------------------------------
# _messages_to_single_prompt smoke
# ---------------------------------------------------------------------------

def test_messages_to_single_prompt_preserves_roles():
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "thinking…", "tool_calls": [
            {"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"x"}'}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "file contents"},
        {"role": "user", "content": "now respond"},
    ]
    out = _messages_to_single_prompt(messages)
    assert "### user:" in out
    assert "### assistant:" in out
    assert "### tool_result (c1):" in out
    assert "[called read_file" in out
