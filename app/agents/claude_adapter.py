"""Claude Code CLI adapter.

Wraps `claude -p --output-format json --permission-mode plan --tools "" --no-session-persistence`.

Claude Code's headless mode returns a single JSON envelope on stdout with the
model's text in the `result` field. We default to claude-haiku-4-5 to keep
conclave costs reasonable; override with `model="..."` on construction.

Note: this adapter invokes a fresh, headless Claude session. It is NOT the
interactive Claude session the user might be running for orchestration —
they are separate inference calls with no shared context.
"""

from __future__ import annotations

import asyncio
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
    ConclaveTurn,
    ConsultantCritique,
    ErrorCode,
    MessageType,
    PeerAnswer,
    PrimaryResponse,
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


class ClaudeCodeAdapter(BaseAdapter):
    name = "claude-code"
    _command = "claude"
    _default_model = "claude-haiku-4-5"  # cheap default; override via constructor for higher quality
    # Haiku 4.5 context window is 200K tokens. Conservative char ratio: 4 chars/token.
    max_context_chars = 800_000

    def __init__(self, model: Optional[str] = None, command_path: Optional[str] = None) -> None:
        super().__init__()
        self._model = model or self._default_model
        # See CodexAdapter — absolute path override (DR0017).
        self.command_path = command_path

    def _resolve_command(self) -> Optional[str]:
        if self.command_path:
            return self.command_path if Path(self.command_path).is_file() else None
        return shutil.which(self._command)

    async def is_available(self) -> bool:
        return self._resolve_command() is not None

    async def readiness(self) -> Readiness:
        if self.command_path:
            if Path(self.command_path).is_file():
                return Readiness(available=True, reason="ok", hint="")
            return Readiness(
                available=False,
                reason="configured_path_missing",
                hint=f"agents.claude-code.command_path is set to '{self.command_path}' but no file exists there.",
            )
        if shutil.which(self._command) is None:
            return Readiness(
                available=False,
                reason="command_not_found",
                hint=(
                    "Claude Code CLI not on PATH. Install it from claude.com/code and run `claude /login`, "
                    "or set agents.claude-code.command_path to the absolute path of the binary."
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
        """Run claude in headless mode with the prompt on stdin.

        When images are attached OR a project sandbox is provided, enable the
        Read tool with --add-dir restricted to the relevant directories so
        Claude can view the files. Without either, keep tools fully disabled.
        """
        cmd_path = self._resolve_command()
        if cmd_path is None:
            raise AdapterError(
                ErrorCode.AGENT_UNAVAILABLE,
                f"{self._command} not on PATH",
            )

        image_paths = image_paths or []
        needs_read_tool = bool(image_paths) or bool(sandbox_path)
        seen_dirs: set[str] = set()
        extra_args: list[str] = []
        if needs_read_tool:
            tools_arg = "Read"
            extra_args.append("--dangerously-skip-permissions")
            for img in image_paths:
                parent = str(img.parent)
                if parent not in seen_dirs:
                    extra_args.extend(["--add-dir", parent])
                    seen_dirs.add(parent)
            if sandbox_path and sandbox_path not in seen_dirs:
                extra_args.extend(["--add-dir", sandbox_path])
                seen_dirs.add(sandbox_path)
        else:
            tools_arg = ""

        # Compose the preamble that nudges Claude to use Read on attached resources.
        preamble_parts: list[str] = []
        if image_paths:
            image_list = "\n".join(f"- {p}" for p in image_paths)
            preamble_parts.append(
                "The following image(s) have been attached. Use the Read tool "
                "on each one before reasoning about them.\n\n"
                f"Images to read:\n{image_list}"
            )
        if sandbox_path:
            preamble_parts.append(
                f"A read-only sandbox copy of the project is available at:\n"
                f"  {sandbox_path}\n\n"
                "Use the Read tool to explore the file tree and read source files "
                "as needed for your reasoning. The sandbox is a snapshot — read-only — "
                "you cannot execute code or modify anything."
            )
        if preamble_parts:
            prompt = (
                "\n\n".join(preamble_parts)
                + "\n\nThen produce your structured JSON response.\n\n"
                + prompt
            )

        args = [
            cmd_path,
            "-p",
            "--output-format", "json",
            "--tools", tools_arg,
            "--no-session-persistence",
            "--model", self._model,
            *extra_args,
        ]
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
                f"claude CLI not found: {e}",
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
                f"claude exceeded timeout of {timeout_seconds}s",
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            raise AdapterError(
                ErrorCode.AGENT_ERROR,
                f"claude exited with code {proc.returncode}",
                details={
                    "stderr": stderr_bytes.decode("utf-8", errors="replace")[-2000:],
                    "stdout_tail": stdout[-2000:],
                },
            )

        self._last_usage = _extract_usage_from_claude(stdout)
        return _extract_result_field(stdout)

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

def _extract_result_field(stdout: str) -> str:
    """Pull the .result field out of Claude's JSON envelope. Fail if is_error is true."""
    try:
        envelope = extract_json_object(stdout)
    except ValueError as e:
        raise AdapterError(
            ErrorCode.AGENT_ERROR,
            f"could not parse claude stdout as JSON: {e}",
            details={"stdout_tail": stdout[-2000:]},
        )
    if envelope.get("is_error"):
        raise AdapterError(
            ErrorCode.AGENT_ERROR,
            f"claude returned error: {envelope.get('result', '(no message)')}",
            details={"envelope": {k: v for k, v in envelope.items() if k in ("subtype", "stop_reason", "api_error_status")}},
        )
    result = envelope.get("result")
    if not isinstance(result, str):
        raise AdapterError(
            ErrorCode.AGENT_ERROR,
            "claude envelope had no .result string",
            details={"envelope_keys": list(envelope.keys())},
        )
    return result


def _extract_usage_from_claude(stdout: str) -> dict[str, Any]:
    """Pull tokens + cost from Claude's envelope."""
    try:
        envelope = extract_json_object(stdout)
    except Exception:  # noqa: BLE001
        return {}
    usage = envelope.get("usage") or {}
    out: dict[str, Any] = {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }
    cost = envelope.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        out["cost_usd"] = float(cost)
    return out


def _parse_and_coerce(
    text: str,
    task_id: str,
    agent_name: str,
    *,
    role: str,
    default_message_type: str,
) -> dict[str, Any]:
    try:
        data = extract_json_object(text)
    except ValueError as e:
        raise AdapterError(
            ErrorCode.AGENT_ERROR,
            f"could not extract JSON from claude response: {e}",
            details={"text_tail": text[-2000:]},
        )
    data["protocol_version"] = "1.0"
    data["task_id"] = task_id
    data["agent"] = agent_name
    data["role"] = role
    data.setdefault("message_type", default_message_type)
    if data.get("resolution_status") in ("null", "None", ""):
        data["resolution_status"] = None
    return data
