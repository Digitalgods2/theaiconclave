"""Codex CLI adapter.

Wraps `codex exec --json --skip-git-repo-check --ephemeral -s read-only`.
Reads the JSONL event stream from stdout, finds the agent_message item, and
parses it as an AI Conclave Switchboard Protocol message.

Codex's `--output-schema <FILE>` could enforce JSON shape natively; we use
prompt-based instruction + defensive parsing here for portability across
Codex versions. Adding schema enforcement is a future enhancement.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from typing import Any, Optional

from pathlib import Path

from app.agents._spawn import SPAWN_KWARGS, communicate_with_cleanup
from app.agents.cli_adapter_base import CliAdapterBase
from app.agents.base import (
    AdapterError,
    AdapterTestResult,
    Readiness,
)
from app.protocol.validators import (
    ErrorCode,
)


class CodexAdapter(CliAdapterBase):
    name = "codex"
    _command = "codex"
    # GPT-5-class context window. Conservative; actual is larger but token-to-char
    # ratio varies. Used as a soft warning, not a hard refusal.
    max_context_chars = 800_000

    # Rough rate ($/1M tokens) for retail-equivalent cost estimation when user is
    # actually on ChatGPT subscription. Honest values:
    # Codex CLI uses GPT-5 via ChatGPT auth; we report tokens, not dollars.
    _cost_per_input_token = 0.0
    _cost_per_output_token = 0.0

    def __init__(self, command_path: Optional[str] = None) -> None:
        super().__init__()
        # Absolute path override (DR0017). When set, used in preference to
        # shutil.which(self._command). Lets a packaged GUI app find the CLI
        # without depending on shell PATH inheritance.
        self.command_path = command_path

    async def is_available(self) -> bool:
        return self._resolve_command() is not None

    def _resolve_command(self) -> Optional[str]:
        """Resolve the CLI to a full path. Required on Windows where npm shims are .cmd files."""
        if self.command_path:
            return self.command_path if Path(self.command_path).is_file() else None
        return shutil.which(self._command)

    async def readiness(self) -> Readiness:
        if self.command_path:
            if Path(self.command_path).is_file():
                return Readiness(available=True, reason="ok", hint="")
            return Readiness(
                available=False,
                reason="configured_path_missing",
                hint=f"agents.codex.command_path is set to '{self.command_path}' but no file exists there.",
            )
        if shutil.which(self._command) is None:
            return Readiness(
                available=False,
                reason="command_not_found",
                hint=(
                    "Codex CLI not on PATH. Install it (`npm install -g @openai/codex-cli`) and "
                    "run `codex login`, or set agents.codex.command_path to the absolute path "
                    "of the binary."
                ),
            )
        return Readiness(available=True, reason="ok", hint="")

    async def test_connection(self) -> AdapterTestResult:
        start = time.perf_counter()
        cmd_path = self._resolve_command()
        if cmd_path is None:
            return AdapterTestResult(
                available=False,
                error=f"{self._command} not on PATH",
                elapsed_ms=int((time.perf_counter() - start) * 1000),
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                cmd_path, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **SPAWN_KWARGS,
            )
            stdout, _ = await communicate_with_cleanup(proc, timeout=15)
            return AdapterTestResult(
                available=True,
                version=stdout.decode("utf-8", errors="replace").strip(),
                elapsed_ms=int((time.perf_counter() - start) * 1000),
            )
        except Exception as e:  # noqa: BLE001
            return AdapterTestResult(
                available=False,
                error=str(e),
                elapsed_ms=int((time.perf_counter() - start) * 1000),
            )

    # ------------------------------------------------------------------

    async def _invoke(self, prompt: str, timeout_seconds: Optional[int], image_paths: list = None, sandbox_path: str = None) -> str:
        """Run `codex exec` with the prompt on stdin and return the agent's text.
        Image attachments are passed via Codex's `-i` flag (repeatable).
        When `sandbox_path` is set, Codex operates inside it via `-C <sandbox>`
        and can use its read-only shell sandbox (ls, cat, find) to enumerate
        and read source files."""
        cmd_path = self._resolve_command()
        if cmd_path is None:
            raise AdapterError(
                ErrorCode.AGENT_UNAVAILABLE,
                f"{self._command} not on PATH",
            )
        args = [
            cmd_path, "exec",
            "--json",
            "--skip-git-repo-check",
            "--ephemeral",
            "-s", "read-only",
        ]
        if sandbox_path:
            args.extend(["-C", sandbox_path])
        for img in (image_paths or []):
            args.extend(["-i", str(img)])
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **SPAWN_KWARGS,
            )
        except FileNotFoundError as e:
            raise AdapterError(
                ErrorCode.AGENT_UNAVAILABLE,
                f"codex CLI not found: {e}",
            )

        try:
            stdout_bytes, stderr_bytes = await communicate_with_cleanup(
                proc,
                input=prompt.encode("utf-8"),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise AdapterError(
                ErrorCode.AGENT_TIMEOUT,
                f"codex exceeded timeout of {timeout_seconds}s",
            )

        if proc.returncode != 0:
            raise AdapterError(
                ErrorCode.AGENT_ERROR,
                f"codex exited with code {proc.returncode}",
                details={
                    "stderr": stderr_bytes.decode("utf-8", errors="replace")[-2000:],
                },
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        self._last_usage = _extract_usage_from_codex(stdout)
        return _extract_agent_message(stdout)

    # ------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Output parsing helpers
# ---------------------------------------------------------------------------

def _extract_agent_message(stdout: str) -> str:
    """Find the last `item.completed` event of type `agent_message` in the JSONL stream."""
    last_text: Optional[str] = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "item.completed":
            item = evt.get("item", {})
            if item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    last_text = text

    if last_text is None:
        raise AdapterError(
            ErrorCode.AGENT_ERROR,
            "codex output contained no agent_message event",
            details={"stdout_tail": stdout[-2000:]},
        )
    return last_text


def _extract_usage_from_codex(stdout: str) -> dict[str, Any]:
    """Pull token counts from the turn.completed event."""
    usage: dict[str, Any] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "turn.completed":
            u = evt.get("usage") or {}
            usage["input_tokens"] = u.get("input_tokens")
            usage["output_tokens"] = u.get("output_tokens")
    return usage


