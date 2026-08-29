"""Antigravity CLI adapter (`agy`).

Wraps:

    agy -p= --input-format stream-json --output-format stream-json --mode plan

Google retired the Gemini CLI on 2026-06-18 for AI Pro / Ultra / free tiers
(Gemini Code Assist Standard and Enterprise licences keep it), and replaced it
with the closed-source Go binary `agy`. This adapter is the successor seat to
`gemini_adapter.py`, which stays in-tree for licence holders.

Four things about `agy` drive the shape of this file:

1. **The prompt goes over stdin, not argv.** `-p "<prompt>"` puts the whole
   prompt on the command line, and conclave prompts (charter + role behavior +
   transcript) routinely exceed the Windows 32,767-character command-line
   limit. `--input-format stream-json` reads one NDJSON message per line from
   stdin instead, which has no such ceiling. It requires
   `--output-format stream-json`, so we parse the NDJSON event stream and take
   the terminal `result` event — the same envelope `--output-format json`
   would have produced in one shot.

2. **`-p` is a Go flag that consumes the next token.** Written bare, it eats
   `--input-format` as its value and the run dies with exit 2. The value has to
   be attached, so the flag is spelled `-p=` (deliberately empty: the prompt
   arrives on stdin).

3. **A failed run still exits 0.** A timeout or model error comes back as
   `status: "ERROR"` inside the result envelope with exit code 0, so the status
   field — not the return code — is the authoritative success signal. Checking
   only `returncode` would silently record an empty deliberation turn.

4. **`--mode plan` is the read-only guarantee**, the analogue of the Gemini
   CLI's `--approval-mode plan`. It is mutually exclusive with
   `--disable-slash-commands`: passing both makes `agy` warn
   "--mode plan has no effect while slash command expansion is disabled" and
   silently drop plan mode. The charter's read-only invariant outranks
   slash-command hardening, so we pass `--mode plan` and never the other.

Cost: the envelope reports tokens but no dollar figure, and the default
Google-account auth draws on an AI Pro/Ultra subscription rather than metered
API billing. We therefore record token counts and never a `cost_usd` — the
same rule `claude_adapter.py` applies in subscription mode, for the same
reason: a list-price estimate in the spend view is worse than no number.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from app.agents._spawn import SPAWN_KWARGS
from app.agents.cli_adapter_base import CliAdapterBase
from app.agents.base import (
    AdapterError,
    AdapterTestResult,
    Readiness,
)
from app.protocol.validators import (
    ErrorCode,
)

# Grace added to our own asyncio timeout so `agy`'s `--print-timeout` fires
# first and we get its structured ERROR envelope (with usage) instead of
# killing the process and losing the token counts.
_TIMEOUT_GRACE_SECONDS = 20


class AntigravityAdapter(CliAdapterBase):
    name = "antigravity"
    _command = "agy"
    # Gemini 3.1 Pro context is ~1M tokens; conservative cap below that,
    # matching the ceiling the Gemini CLI seat used.
    max_context_chars = 2_000_000

    def __init__(
        self,
        command_path: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        extra_args: Optional[list[str]] = None,
    ) -> None:
        super().__init__()
        # Absolute path override (DR0017). `agy` installs to
        # %LOCALAPPDATA%\agy\bin on Windows and ~/.local/bin on Unix; both are
        # on PATH after `agy install`, but a GUI launch may not inherit it.
        self.command_path = command_path
        self.model = model
        self.effort = effort
        self.extra_args = list(extra_args or [])

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
                hint=(
                    f"agents.antigravity.command_path is set to '{self.command_path}' "
                    "but no file exists there."
                ),
            )
        if shutil.which(self._command) is None:
            return Readiness(
                available=False,
                reason="command_not_found",
                hint=(
                    "Antigravity CLI (`agy`) not on PATH. Install it with "
                    "`irm https://antigravity.google/cli/install.ps1 | iex` (Windows) or "
                    "`curl -fsSL https://antigravity.google/cli/install.sh | sh` (Unix), "
                    "run `agy` once interactively to authenticate, or set "
                    "agents.antigravity.command_path to the absolute path of the binary."
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

    def _build_args(
        self,
        cmd_path: str,
        timeout_seconds: int,
        workspace_dirs: list[str],
    ) -> list[str]:
        """Assemble the argv for one print-mode run.

        `-p=` is intentionally an attached empty value — see the module
        docstring. `--mode plan` is what keeps the seat read-only, so it is not
        configurable away from here.
        """
        args = [
            cmd_path,
            "-p=",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--mode", "plan",
            "--print-timeout", f"{timeout_seconds}s",
        ]
        if self.model:
            args.extend(["--model", self.model])
        if self.effort:
            args.extend(["--effort", self.effort])
        for d in workspace_dirs:
            args.extend(["--add-dir", d])
        args.extend(self.extra_args)
        return args

    async def _invoke(
        self,
        prompt: str,
        timeout_seconds: int,
        image_paths: list = None,
        sandbox_path: str = None,
    ) -> str:
        """Run one `agy` turn and return the agent's response text.

        `sandbox_path` (the per-task read-only copy) and any image parent
        directories are added to the workspace with `--add-dir`; `agy` does not
        treat the process cwd as the workspace on its own.
        """
        image_paths = image_paths or []
        cmd_path = self._resolve_command()
        if cmd_path is None:
            raise AdapterError(
                ErrorCode.AGENT_UNAVAILABLE,
                f"{self._command} not on PATH",
            )

        workspace_dirs: list[str] = []
        seen_dirs: set[str] = set()
        if sandbox_path:
            sandbox_abs = str(Path(sandbox_path).resolve())
            workspace_dirs.append(sandbox_abs)
            seen_dirs.add(sandbox_abs)

        if image_paths:
            refs = "\n".join(str(p.resolve()) for p in image_paths)
            prompt = (
                "The following image(s) are attached for your analysis. "
                "Read each one from disk and examine its actual visual content "
                "before reasoning.\n"
                + refs
                + "\n\n"
                + prompt
            )
            for p in image_paths:
                parent = str(p.parent.resolve())
                if parent not in seen_dirs:
                    workspace_dirs.append(parent)
                    seen_dirs.add(parent)

        args = self._build_args(cmd_path, timeout_seconds, workspace_dirs)
        stdin_payload = json.dumps(
            {"event": "user", "message": {"content": prompt}},
            ensure_ascii=False,
        ) + "\n"

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
                f"Antigravity CLI not found: {e}",
            )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=stdin_payload.encode("utf-8")),
                timeout=timeout_seconds + _TIMEOUT_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise AdapterError(
                ErrorCode.AGENT_TIMEOUT,
                f"agy exceeded timeout of {timeout_seconds}s",
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            raise AdapterError(
                ErrorCode.AGENT_ERROR,
                f"agy exited with code {proc.returncode}",
                details={"stderr": stderr[-2000:], "stdout_tail": stdout[-2000:]},
            )

        result = _extract_result_event(stdout)
        self._last_usage = _extract_usage(result)

        # Exit code 0 is not success on its own — see module docstring.
        status = result.get("status")
        if status != "SUCCESS":
            message = result.get("error") or f"agy returned status {status}"
            code = (
                ErrorCode.AGENT_TIMEOUT
                if isinstance(message, str) and "timeout" in message.lower()
                else ErrorCode.AGENT_ERROR
            )
            raise AdapterError(
                code,
                f"agy run failed: {message}",
                details={"status": status, "stderr": stderr[-2000:]},
            )

        response = result.get("response")
        if not isinstance(response, str) or not response.strip():
            raise AdapterError(
                ErrorCode.AGENT_ERROR,
                "agy result event had no .response text",
                details={"result_keys": list(result.keys())},
            )
        return response

    # ------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Output parsing helpers
# ---------------------------------------------------------------------------

def _extract_result_event(stdout: str) -> dict[str, Any]:
    """Return the payload of the terminal `result` event from the NDJSON stream.

    The stream also carries `init` and per-step `step_update` events; only
    `result` is terminal and only it carries the final response and cumulative
    usage. Unparseable lines are skipped rather than fatal — `agy` may
    interleave diagnostics, and one bad line should not lose a completed turn.
    """
    result: Optional[dict[str, Any]] = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event") == "result":
            payload = event.get("result")
            if isinstance(payload, dict):
                result = payload
    if result is None:
        raise AdapterError(
            ErrorCode.AGENT_ERROR,
            "agy produced no result event",
            details={"stdout_tail": stdout[-2000:]},
        )
    return result


def _extract_usage(result: dict[str, Any]) -> dict[str, Any]:
    """Pull token counts from the result envelope.

    No `cost_usd` is ever recorded: `agy` reports no dollar figure, and the
    default Google-account auth bills against an AI Pro/Ultra subscription
    rather than per token. See the module docstring.
    """
    usage = result.get("usage") or {}
    if not isinstance(usage, dict):
        return {}
    out: dict[str, Any] = {}
    if isinstance(usage.get("input_tokens"), int):
        out["input_tokens"] = usage["input_tokens"]
    if isinstance(usage.get("output_tokens"), int):
        out["output_tokens"] = usage["output_tokens"]
    return out


