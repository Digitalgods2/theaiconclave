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

from app.agents.base import (
    AdapterContext,
    AdapterError,
    AdapterTestResult,
    BaseAdapter,
    Readiness,
)
from app.protocol.validators import (
    AgentRole,
    ConclaveTurn,
    ConsultantCritique,
    ErrorCode,
    MessageType,
    PeerAnswer,
    PrimaryResponse,
    ResolutionStatus,
)
from app.services.prompt_builder import (
    build_conclave_prompt,
    build_consultant_prompt,
    build_final_prompt,
    build_peer_prompt,
    build_primary_prompt,
)
from app.utils.attachments import image_attachment_paths
from app.utils.json_tools import extract_json_object


class CodexAdapter(BaseAdapter):
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
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
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

    async def _invoke(self, prompt: str, timeout_seconds: int, image_paths: list = None, sandbox_path: str = None) -> str:
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
            )
        except FileNotFoundError as e:
            raise AdapterError(
                ErrorCode.AGENT_UNAVAILABLE,
                f"codex CLI not found: {e}",
            )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
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

    async def run_primary(self, ctx: AdapterContext) -> PrimaryResponse:
        prompt = build_primary_prompt(
            task=ctx.task,
            task_id=ctx.task_id,
            agent_name=self.name,
            prior_messages=ctx.prior_messages,
            ceiling_chars=self.max_context_chars,
            include_sandbox_manifest=False,
        )
        text = await self._invoke(
            prompt, ctx.timeout_seconds,
            image_attachment_paths(ctx.task),
            ctx.task.context.extra.get("sandbox_path"),
        )
        data = _parse_and_coerce(
            text, ctx.task_id, self.name,
            role="primary", default_message_type=MessageType.PRIMARY_PROPOSAL.value,
        )
        return PrimaryResponse.model_validate(data)

    async def run_consultant(self, ctx: AdapterContext) -> ConsultantCritique:
        prompt = build_consultant_prompt(
            task=ctx.task,
            task_id=ctx.task_id,
            agent_name=self.name,
            prior_messages=ctx.prior_messages,
            ceiling_chars=self.max_context_chars,
            include_sandbox_manifest=False,
        )
        text = await self._invoke(
            prompt, ctx.timeout_seconds,
            image_attachment_paths(ctx.task),
            ctx.task.context.extra.get("sandbox_path"),
        )
        data = _parse_and_coerce(
            text, ctx.task_id, self.name,
            role="consultant", default_message_type=MessageType.CONSULTANT_CRITIQUE.value,
        )
        return ConsultantCritique.model_validate(data)

    async def run_final(self, ctx: AdapterContext) -> PrimaryResponse:
        prompt = build_final_prompt(
            task=ctx.task,
            task_id=ctx.task_id,
            agent_name=self.name,
            prior_messages=ctx.prior_messages,
            ceiling_chars=self.max_context_chars,
            include_sandbox_manifest=False,
        )
        text = await self._invoke(
            prompt, ctx.timeout_seconds,
            image_attachment_paths(ctx.task),
            ctx.task.context.extra.get("sandbox_path"),
        )
        data = _parse_and_coerce(
            text, ctx.task_id, self.name,
            role="primary", default_message_type=MessageType.PRIMARY_FINAL.value,
        )
        return PrimaryResponse.model_validate(data)

    async def run_peer(self, ctx: AdapterContext) -> PeerAnswer:
        prompt = build_peer_prompt(ctx.task, ctx.task_id, self.name, ceiling_chars=self.max_context_chars, include_sandbox_manifest=False)
        text = await self._invoke(
            prompt, ctx.timeout_seconds,
            image_attachment_paths(ctx.task),
            ctx.task.context.extra.get("sandbox_path"),
        )
        data = _parse_and_coerce(
            text, ctx.task_id, self.name,
            role="peer", default_message_type=MessageType.PEER_ANSWER.value,
        )
        return PeerAnswer.model_validate(data)

    async def run_conclave_turn(self, ctx: AdapterContext) -> ConclaveTurn:
        others = [c for c in ctx.task.consultants if c != self.name]
        prompt = build_conclave_prompt(
            task=ctx.task,
            task_id=ctx.task_id,
            agent_name=self.name,
            prior_messages=ctx.prior_messages,
            other_participants=others,
        )
        text = await self._invoke(
            prompt, ctx.timeout_seconds,
            image_attachment_paths(ctx.task),
            ctx.task.context.extra.get("sandbox_path"),
        )
        data = _parse_and_coerce(
            text, ctx.task_id, self.name,
            role="participant", default_message_type=MessageType.CONCLAVE_TURN.value,
        )
        return ConclaveTurn.model_validate(data)


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


def _parse_and_coerce(
    text: str,
    task_id: str,
    agent_name: str,
    *,
    role: str,
    default_message_type: str,
) -> dict[str, Any]:
    """Extract the JSON object and overwrite identity fields the model may have garbled."""
    try:
        data = extract_json_object(text)
    except ValueError as e:
        raise AdapterError(
            ErrorCode.AGENT_ERROR,
            f"could not extract JSON from codex response: {e}",
            details={"text_tail": text[-2000:]},
        )
    data["protocol_version"] = "1.0"
    data["task_id"] = task_id
    data["agent"] = agent_name
    data["role"] = role
    data.setdefault("message_type", default_message_type)
    # Normalize resolution_status if the model returned a stringified null
    if data.get("resolution_status") in ("null", "None", ""):
        data["resolution_status"] = None
    return data
