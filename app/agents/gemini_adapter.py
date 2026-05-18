"""Gemini CLI adapter.

Wraps `gemini -p <PROMPT> -o json --approval-mode plan`.

Gemini's `-o json` returns a single JSON object on stdout with the model's
text in the `response` field plus stats. The approval mode `plan` keeps the
agent in read-only behavior — it cannot run tools or modify files.

Note: Gemini's stderr emits warnings (terminal capabilities, MCP status,
etc.) which we ignore.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any, Optional

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


class GeminiAdapter(BaseAdapter):
    name = "gemini"
    _command = "gemini"
    # Gemini 3.1 Pro context is ~1M tokens; conservative cap below that for safety.
    max_context_chars = 2_000_000

    def __init__(self, command_path: Optional[str] = None) -> None:
        super().__init__()
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
                hint=f"agents.gemini.command_path is set to '{self.command_path}' but no file exists there.",
            )
        if shutil.which(self._command) is None:
            return Readiness(
                available=False,
                reason="command_not_found",
                hint=(
                    "Gemini CLI not on PATH. Install it (`npm install -g @google/gemini-cli`) and "
                    "run `gemini /auth`, or set agents.gemini.command_path to the absolute path "
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
        """Run gemini with the prompt on stdin. Images are referenced as @<path>
        in a preamble so Gemini's parser loads them into context.

        On Windows, Gemini's `@<path>` parser needs forward-slash POSIX paths,
        not native backslashes, to reliably load the image. We also add each
        image's parent directory to the trusted workspace via
        --include-directories + --skip-trust so the sandbox does not refuse.

        When `sandbox_path` is set, that directory is added to Gemini's
        workspace via `--include-directories` so the agent can browse and
        read project files in plan (read-only) mode.
        """
        image_paths = image_paths or []
        cmd_path = self._resolve_command()
        if cmd_path is None:
            raise AdapterError(
                ErrorCode.AGENT_UNAVAILABLE,
                f"{self._command} not on PATH",
            )

        extra_args: list[str] = []
        seen_dirs: set[str] = set()
        if image_paths:
            refs = "\n".join(f"@{p.as_posix()}" for p in image_paths)
            prompt = (
                "The following image(s) are attached for your analysis. "
                "Examine each one's actual visual content before reasoning.\n"
                + refs
                + "\n\n"
                + prompt
            )
            extra_args.append("--skip-trust")
            for p in image_paths:
                parent = p.parent.as_posix()
                if parent not in seen_dirs:
                    extra_args.extend(["--include-directories", parent])
                    seen_dirs.add(parent)

        if sandbox_path:
            sandbox_posix = Path(sandbox_path).as_posix()
            if sandbox_posix not in seen_dirs:
                if "--skip-trust" not in extra_args:
                    extra_args.append("--skip-trust")
                extra_args.extend(["--include-directories", sandbox_posix])
                seen_dirs.add(sandbox_posix)

        args = [
            cmd_path,
            "-p", "",
            "-o", "json",
            "--approval-mode", "plan",
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
                f"gemini CLI not found: {e}",
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
                f"gemini exceeded timeout of {timeout_seconds}s",
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            raise AdapterError(
                ErrorCode.AGENT_ERROR,
                f"gemini exited with code {proc.returncode}",
                details={
                    "stderr": stderr_bytes.decode("utf-8", errors="replace")[-2000:],
                    "stdout_tail": stdout[-2000:],
                },
            )

        self._last_usage = _extract_usage_from_gemini(stdout)
        return _extract_response_field(stdout)

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

def _extract_response_field(stdout: str) -> str:
    """Pull the .response field out of Gemini's JSON envelope."""
    try:
        envelope = extract_json_object(stdout)
    except ValueError as e:
        raise AdapterError(
            ErrorCode.AGENT_ERROR,
            f"could not parse gemini stdout as JSON: {e}",
            details={"stdout_tail": stdout[-2000:]},
        )
    response = envelope.get("response")
    if not isinstance(response, str):
        raise AdapterError(
            ErrorCode.AGENT_ERROR,
            "gemini envelope had no .response string",
            details={"envelope_keys": list(envelope.keys())},
        )
    return response


def _extract_usage_from_gemini(stdout: str) -> dict[str, Any]:
    """Pull total token counts from Gemini's stats block."""
    try:
        envelope = extract_json_object(stdout)
    except Exception:  # noqa: BLE001
        return {}
    stats = envelope.get("stats") or {}
    models = stats.get("models") or {}
    total_in = 0
    total_out = 0
    for model_stats in models.values():
        tokens = (model_stats or {}).get("tokens") or {}
        if isinstance(tokens.get("prompt"), int):
            total_in += tokens["prompt"]
        if isinstance(tokens.get("candidates"), int):
            total_out += tokens["candidates"]
    return {"input_tokens": total_in or None, "output_tokens": total_out or None}


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
            f"could not extract JSON from gemini response: {e}",
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
