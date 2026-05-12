"""Ollama Cloud adapter — a pluggable seat for open-weight frontier-class models.

Unlike the CLI adapters (codex / claude-code / gemini), this one talks to a
hosted HTTP API: `POST https://ollama.com/api/chat` with a Bearer token. One
adapter class, instantiated once per enabled model id, so the council can carry
several Ollama-backed seats (DeepSeek, GLM, Qwen, …) selected via the same
checkbox UI as the CLI agents.

Auth: `OLLAMA_API_KEY` env var (created at ollama.com settings). If unset, the
adapter registers but reports unavailable — same pattern as a CLI adapter whose
binary isn't on PATH.

Notes / known v1 limitations:
- Image attachments are NOT forwarded. These are text-reasoning models; an
  image-heavy task should rely on the frontier participants for the visual part.
- Reasoning models (DeepSeek-R1-style) emit a `<think>…</think>` block before
  the answer. We request `think: false` and also strip any such block before
  parsing, so the JSON survives.
- Structured output uses Ollama's `format: "json"` mode plus the prompt
  builders' existing "produce structured JSON" instructions; we then parse with
  the same tolerant extractor the other adapters use.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Optional

import httpx

from app.agents.base import (
    AdapterContext,
    AdapterError,
    AdapterTestResult,
    BaseAdapter,
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
from app.utils.json_tools import extract_json_object
from app.utils.sandbox_inline import build_sandbox_section


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_API_KEY_ENV = "OLLAMA_API_KEY"
_DEFAULT_ENDPOINT = "https://ollama.com"
# Reserve this many chars below max_context_chars for the response + tokenizer slop
# when sizing an inlined sandbox.
_SANDBOX_HEADROOM = 16_000


class OllamaCloudAdapter(BaseAdapter):
    """One Ollama-Cloud-hosted model, exposed under a friendly council name.

    Args:
        name:               council/checkbox name, e.g. "deepseek". MUST match
                            what the orchestrator puts in task.consultants.
        model_id:           Ollama Cloud model id, e.g. "deepseek-v3.1:671b-cloud".
        max_context_chars:  declared context budget (informational, same as the
                            other adapters — not enforced in v1).
        endpoint:           API base, defaults to https://ollama.com.
    """

    internal = False

    def __init__(
        self,
        name: str,
        model_id: str,
        max_context_chars: int = 400_000,
        endpoint: str = _DEFAULT_ENDPOINT,
    ) -> None:
        super().__init__()
        if not name:
            raise ValueError("OllamaCloudAdapter requires a non-empty name")
        if not model_id:
            raise ValueError("OllamaCloudAdapter requires a non-empty model_id")
        self.name = name
        self.model_id = model_id
        self.max_context_chars = max_context_chars
        self.endpoint = endpoint.rstrip("/")

    # ------------------------------------------------------------------

    def _api_key(self) -> Optional[str]:
        """Resolve the Ollama Cloud API key. Precedence: OLLAMA_API_KEY env var,
        then the database-stored key (set via the dashboard's Settings panel).
        Returns None if neither is present.
        """
        env_key = os.environ.get(_API_KEY_ENV)
        if env_key and env_key.strip():
            return env_key.strip()
        # Lazy import: keeps the DB out of this module's hard dependencies, so
        # unit tests that construct the adapter directly don't need a DB.
        from app.services.settings_store import get_secret
        db_key = get_secret("ollama_api_key")
        return db_key.strip() if db_key and db_key.strip() else None

    def _append_sandbox(self, prompt: str, ctx: AdapterContext) -> str:
        """If the task has a project sandbox, inline a read-only file tree +
        file contents into the prompt (this adapter can't browse files)."""
        try:
            sandbox_path = ctx.task.context.extra.get("sandbox_path")
        except Exception:  # noqa: BLE001
            sandbox_path = None
        if not sandbox_path:
            return prompt
        budget = max(0, self.max_context_chars - len(prompt) - _SANDBOX_HEADROOM)
        if budget < 2000:
            return prompt
        section = build_sandbox_section(str(sandbox_path), budget)
        if not section:
            return prompt
        return prompt + "\n\n" + section

    async def is_available(self) -> bool:
        return self._api_key() is not None

    async def test_connection(self) -> AdapterTestResult:
        start = time.perf_counter()
        key = self._api_key()
        if key is None:
            return AdapterTestResult(
                available=False,
                error=f"{_API_KEY_ENV} not set",
                elapsed_ms=int((time.perf_counter() - start) * 1000),
            )
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{self.endpoint}/api/tags",
                    headers={"Authorization": f"Bearer {key}"},
                )
            ok = resp.status_code == 200
            return AdapterTestResult(
                available=ok,
                version=f"ollama-cloud:{self.model_id}" if ok else None,
                error=None if ok else f"HTTP {resp.status_code}: {resp.text[:200]}",
                elapsed_ms=int((time.perf_counter() - start) * 1000),
            )
        except Exception as e:  # noqa: BLE001
            return AdapterTestResult(
                available=False,
                error=str(e),
                elapsed_ms=int((time.perf_counter() - start) * 1000),
            )

    # ------------------------------------------------------------------

    async def _invoke(self, prompt: str, timeout_seconds: int) -> str:
        key = self._api_key()
        if key is None:
            raise AdapterError(
                ErrorCode.AGENT_UNAVAILABLE,
                f"{_API_KEY_ENV} not set; configure an Ollama Cloud API key",
            )

        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",   # constrain output to valid JSON
            "think": False,     # ask reasoning models to skip the <think> block (ignored if unsupported)
        }
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                resp = await client.post(
                    f"{self.endpoint}/api/chat",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json=payload,
                )
        except httpx.TimeoutException:
            raise AdapterError(
                ErrorCode.AGENT_TIMEOUT,
                f"ollama-cloud[{self.model_id}] exceeded timeout of {timeout_seconds}s",
            )
        except httpx.HTTPError as e:
            raise AdapterError(
                ErrorCode.AGENT_ERROR,
                f"ollama-cloud[{self.model_id}] HTTP error: {e}",
            )

        if resp.status_code != 200:
            body_low = (resp.text or "").lower()
            if resp.status_code == 403 and "subscription" in body_low:
                msg = (f"ollama-cloud[{self.model_id}] requires an Ollama Cloud paid plan "
                       f"(see https://ollama.com/upgrade). Tip: OpenRouter carries the same "
                       f"models pay-per-token with no subscription.")
            elif resp.status_code == 401:
                msg = f"ollama-cloud[{self.model_id}]: unauthorized (bad or missing API key)"
            else:
                msg = f"ollama-cloud[{self.model_id}] returned HTTP {resp.status_code}"
            raise AdapterError(
                ErrorCode.AGENT_ERROR, msg,
                details={"status_code": resp.status_code, "body_tail": resp.text[-2000:]},
            )

        try:
            body = resp.json()
        except ValueError as e:
            raise AdapterError(
                ErrorCode.AGENT_ERROR,
                f"ollama-cloud[{self.model_id}] response was not JSON: {e}",
                details={"body_tail": resp.text[-2000:]},
            )

        # Usage accounting (no cost field from Ollama Cloud; that's fine — costs
        # are intentionally out of scope for this seat).
        self._last_usage = {
            "input_tokens": body.get("prompt_eval_count"),
            "output_tokens": body.get("eval_count"),
        }

        message = body.get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise AdapterError(
                ErrorCode.AGENT_ERROR,
                f"ollama-cloud[{self.model_id}] returned no message content",
                details={"body_keys": list(body.keys())},
            )
        # Strip any reasoning block the model emitted despite think:false.
        return _THINK_BLOCK_RE.sub("", content).strip()

    # ------------------------------------------------------------------

    async def run_primary(self, ctx: AdapterContext) -> PrimaryResponse:
        prompt = build_primary_prompt(
            task=ctx.task, task_id=ctx.task_id, agent_name=self.name,
            prior_messages=ctx.prior_messages,
        )
        text = await self._invoke(self._append_sandbox(prompt, ctx), ctx.timeout_seconds)
        data = _parse_and_coerce(text, ctx.task_id, self.name,
                                 role="primary",
                                 default_message_type=MessageType.PRIMARY_PROPOSAL.value)
        return PrimaryResponse.model_validate(data)

    async def run_consultant(self, ctx: AdapterContext) -> ConsultantCritique:
        prompt = build_consultant_prompt(
            task=ctx.task, task_id=ctx.task_id, agent_name=self.name,
            prior_messages=ctx.prior_messages,
        )
        text = await self._invoke(self._append_sandbox(prompt, ctx), ctx.timeout_seconds)
        data = _parse_and_coerce(text, ctx.task_id, self.name,
                                 role="consultant",
                                 default_message_type=MessageType.CONSULTANT_CRITIQUE.value)
        return ConsultantCritique.model_validate(data)

    async def run_final(self, ctx: AdapterContext) -> PrimaryResponse:
        prompt = build_final_prompt(
            task=ctx.task, task_id=ctx.task_id, agent_name=self.name,
            prior_messages=ctx.prior_messages,
        )
        text = await self._invoke(self._append_sandbox(prompt, ctx), ctx.timeout_seconds)
        data = _parse_and_coerce(text, ctx.task_id, self.name,
                                 role="primary",
                                 default_message_type=MessageType.PRIMARY_FINAL.value)
        return PrimaryResponse.model_validate(data)

    async def run_peer(self, ctx: AdapterContext) -> PeerAnswer:
        prompt = build_peer_prompt(ctx.task, ctx.task_id, self.name)
        text = await self._invoke(self._append_sandbox(prompt, ctx), ctx.timeout_seconds)
        data = _parse_and_coerce(text, ctx.task_id, self.name,
                                 role="peer",
                                 default_message_type=MessageType.PEER_ANSWER.value)
        return PeerAnswer.model_validate(data)

    async def run_conclave_turn(self, ctx: AdapterContext) -> ConclaveTurn:
        others = [c for c in ctx.task.consultants if c != self.name]
        prompt = build_conclave_prompt(
            task=ctx.task, task_id=ctx.task_id, agent_name=self.name,
            prior_messages=ctx.prior_messages, other_participants=others,
        )
        text = await self._invoke(self._append_sandbox(prompt, ctx), ctx.timeout_seconds)
        data = _parse_and_coerce(text, ctx.task_id, self.name,
                                 role="participant",
                                 default_message_type=MessageType.CONCLAVE_TURN.value)
        return ConclaveTurn.model_validate(data)


# ---------------------------------------------------------------------------
# Output parsing (mirrors the CLI adapters' coercion shape)
# ---------------------------------------------------------------------------

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
            f"could not extract JSON from ollama-cloud[{agent_name}] response: {e}",
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


__all__ = ["OllamaCloudAdapter"]
