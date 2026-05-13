"""OpenRouter adapter — pay-per-token access to open-weight / non-frontier models.

OpenRouter is a unified OpenAI-compatible gateway to many model providers
(DeepSeek, Qwen, GLM/Z.ai, Kimi/Moonshot, MiniMax, …). Unlike Ollama Cloud's
big models, the frontier-class open-weight models here are pay-per-token (no
subscription) and cheap — a conclave turn costs cents at most.

One adapter class, instantiated once per enabled model slug, so the council can
carry several OpenRouter-backed seats selected via the dashboard checkbox list.

Auth: `OPENROUTER_API_KEY` env var, else the database-stored key (Settings →
API Keys). If neither, the seats register but report unavailable.

Privacy: by default we send `provider.data_collection: "deny"` so OpenRouter
won't route through providers that retain/train on the prompt — relevant since
the conclave's main use is code review. Set `data_collection: "allow"` in the
`openrouter:` config section to opt back in (sometimes unlocks cheaper routing).

Reasoning models (DeepSeek-R1-style) may emit a `<think>…</think>` block before
the answer; we strip it before parsing so the JSON survives.
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


# Reserve this many chars below max_context_chars for the model's response +
# tokenizer slop when sizing an inlined sandbox.
_SANDBOX_HEADROOM = 16_000


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_API_KEY_ENV = "OPENROUTER_API_KEY"
_DB_KEY = "openrouter_api_key"
_DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1"
_APP_TITLE = "AI Switchboard Conclave"


class OpenRouterAdapter(BaseAdapter):
    """One OpenRouter-hosted model, exposed under a friendly council name.

    Args:
        name:               council/checkbox name, e.g. "deepseek". MUST match
                            what the orchestrator puts in task.consultants.
        model_slug:         OpenRouter model id, e.g. "deepseek/deepseek-chat".
        max_context_chars:  declared context budget (informational — not enforced).
        endpoint:           API base, defaults to https://openrouter.ai/api/v1.
        data_collection:    "deny" (default) or "allow" — passed as
                            provider.data_collection on every request.
    """

    internal = False

    def __init__(
        self,
        name: str,
        model_slug: str,
        max_context_chars: int = 400_000,
        endpoint: str = _DEFAULT_ENDPOINT,
        data_collection: str = "deny",
    ) -> None:
        super().__init__()
        if not name:
            raise ValueError("OpenRouterAdapter requires a non-empty name")
        if not model_slug:
            raise ValueError("OpenRouterAdapter requires a non-empty model_slug")
        self.name = name
        self.model_slug = model_slug
        self.max_context_chars = max_context_chars
        self.endpoint = endpoint.rstrip("/")
        self.data_collection = data_collection if data_collection in ("deny", "allow") else "deny"

    # ------------------------------------------------------------------

    def _api_key(self) -> Optional[str]:
        """Resolve the OpenRouter API key. Precedence: OPENROUTER_API_KEY env var,
        then the database-stored key (Settings → API Keys)."""
        env_key = os.environ.get(_API_KEY_ENV)
        if env_key and env_key.strip():
            return env_key.strip()
        from app.services.settings_store import get_secret
        db_key = get_secret(_DB_KEY)
        return db_key.strip() if db_key and db_key.strip() else None

    def _headers(self, key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Title": _APP_TITLE,
        }

    def _append_sandbox(self, prompt: str, ctx: AdapterContext) -> str:
        """If the task has a project sandbox, inline a read-only file tree +
        file contents into the prompt (this adapter can't browse files). Sized
        to fit within max_context_chars with headroom for the response."""
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
                error=f"{_API_KEY_ENV} not set (and no stored key)",
                elapsed_ms=int((time.perf_counter() - start) * 1000),
            )
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{self.endpoint}/models", headers=self._headers(key))
            ok = resp.status_code == 200
            return AdapterTestResult(
                available=ok,
                version=f"openrouter:{self.model_slug}" if ok else None,
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
                f"{_API_KEY_ENV} not set; configure an OpenRouter API key (env var or Settings → API Keys)",
            )

        payload: dict[str, Any] = {
            "model": self.model_slug,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "response_format": {"type": "json_object"},
            "provider": {"data_collection": self.data_collection},
        }
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                resp = await client.post(
                    f"{self.endpoint}/chat/completions",
                    headers=self._headers(key),
                    json=payload,
                )
        except httpx.TimeoutException:
            raise AdapterError(
                ErrorCode.AGENT_TIMEOUT,
                f"openrouter[{self.model_slug}] exceeded timeout of {timeout_seconds}s",
            )
        except httpx.HTTPError as e:
            raise AdapterError(
                ErrorCode.AGENT_ERROR,
                f"openrouter[{self.model_slug}] HTTP error: {e}",
            )

        if resp.status_code != 200:
            raise AdapterError(
                ErrorCode.AGENT_ERROR,
                _http_error_message(self.model_slug, resp.status_code, resp.text),
                details={"status_code": resp.status_code, "body_tail": resp.text[-2000:]},
            )

        try:
            body = resp.json()
        except ValueError as e:
            raise AdapterError(
                ErrorCode.AGENT_ERROR,
                f"openrouter[{self.model_slug}] response was not JSON: {e}",
                details={"body_tail": resp.text[-2000:]},
            )

        # OpenRouter sometimes nests an error in a 200 body.
        if isinstance(body.get("error"), dict):
            err = body["error"]
            raise AdapterError(
                ErrorCode.AGENT_ERROR,
                f"openrouter[{self.model_slug}] error: {err.get('message') or err}",
                details={"error": err},
            )

        usage = body.get("usage") or {}
        self._last_usage = {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
        }
        # OpenRouter returns cost (in USD) in usage on newer API versions; capture if present.
        cost = usage.get("cost")
        if isinstance(cost, (int, float)):
            self._last_usage["cost_usd"] = float(cost)

        choices = body.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise AdapterError(
                ErrorCode.AGENT_ERROR,
                f"openrouter[{self.model_slug}] returned no choices",
                details={"body_keys": list(body.keys())},
            )
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise AdapterError(
                ErrorCode.AGENT_ERROR,
                f"openrouter[{self.model_slug}] returned empty message content",
                details={"finish_reason": choices[0].get("finish_reason")},
            )
        return _THINK_BLOCK_RE.sub("", content).strip()

    # ------------------------------------------------------------------

    async def run_primary(self, ctx: AdapterContext) -> PrimaryResponse:
        prompt = build_primary_prompt(task=ctx.task, task_id=ctx.task_id, agent_name=self.name,
                                      prior_messages=ctx.prior_messages)
        text = await self._invoke(self._append_sandbox(prompt, ctx), ctx.timeout_seconds)
        data = _parse_and_coerce(text, ctx.task_id, self.name, role="primary",
                                 default_message_type=MessageType.PRIMARY_PROPOSAL.value)
        return PrimaryResponse.model_validate(data)

    async def run_consultant(self, ctx: AdapterContext) -> ConsultantCritique:
        prompt = build_consultant_prompt(task=ctx.task, task_id=ctx.task_id, agent_name=self.name,
                                         prior_messages=ctx.prior_messages)
        text = await self._invoke(self._append_sandbox(prompt, ctx), ctx.timeout_seconds)
        data = _parse_and_coerce(text, ctx.task_id, self.name, role="consultant",
                                 default_message_type=MessageType.CONSULTANT_CRITIQUE.value)
        return ConsultantCritique.model_validate(data)

    async def run_final(self, ctx: AdapterContext) -> PrimaryResponse:
        prompt = build_final_prompt(task=ctx.task, task_id=ctx.task_id, agent_name=self.name,
                                    prior_messages=ctx.prior_messages)
        text = await self._invoke(self._append_sandbox(prompt, ctx), ctx.timeout_seconds)
        data = _parse_and_coerce(text, ctx.task_id, self.name, role="primary",
                                 default_message_type=MessageType.PRIMARY_FINAL.value)
        return PrimaryResponse.model_validate(data)

    async def run_peer(self, ctx: AdapterContext) -> PeerAnswer:
        prompt = build_peer_prompt(ctx.task, ctx.task_id, self.name)
        text = await self._invoke(self._append_sandbox(prompt, ctx), ctx.timeout_seconds)
        data = _parse_and_coerce(text, ctx.task_id, self.name, role="peer",
                                 default_message_type=MessageType.PEER_ANSWER.value)
        return PeerAnswer.model_validate(data)

    async def run_conclave_turn(self, ctx: AdapterContext) -> ConclaveTurn:
        others = [c for c in ctx.task.consultants if c != self.name]
        prompt = build_conclave_prompt(task=ctx.task, task_id=ctx.task_id, agent_name=self.name,
                                       prior_messages=ctx.prior_messages, other_participants=others)
        text = await self._invoke(self._append_sandbox(prompt, ctx), ctx.timeout_seconds)
        data = _parse_and_coerce(text, ctx.task_id, self.name, role="participant",
                                 default_message_type=MessageType.CONCLAVE_TURN.value)
        return ConclaveTurn.model_validate(data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CTX_LIMIT_RE = re.compile(
    r"maximum context length is\s+(?P<limit>[\d,]+)\s+tokens.*?requested\s+about\s+(?P<used>[\d,]+)\s+tokens",
    re.IGNORECASE | re.DOTALL,
)


def _http_error_message(model_slug: str, status: int, body: str) -> str:
    """Turn a non-200 into a message that says something useful."""
    low = (body or "").lower()
    if status == 401:
        return f"openrouter[{model_slug}]: unauthorized (bad or missing API key)"
    if status == 402 or "credit" in low or "insufficient" in low:
        return f"openrouter[{model_slug}]: out of credits — top up at openrouter.ai/credits"
    if status == 404 or "not a valid model" in low or "no endpoints found" in low:
        return f"openrouter[{model_slug}]: model not found — check the slug at openrouter.ai/models"
    if status == 429:
        return f"openrouter[{model_slug}]: rate limited (free-tier limit or provider throttling)"
    if status == 400 and "maximum context length" in low:
        m = _CTX_LIMIT_RE.search(body or "")
        if m:
            limit = m.group("limit").replace(",", "")
            used = m.group("used").replace(",", "")
            # 3 chars/token is a safe rule of thumb for code-heavy prompts.
            recommended = int(int(limit) * 3 * 0.85)
            return (f"openrouter[{model_slug}]: prompt overflowed the model's context "
                    f"({used} tokens sent, limit {limit}). Lower `max_context_chars` for "
                    f"this seat in config.yaml — try around {recommended:,} or less.")
        return (f"openrouter[{model_slug}]: prompt overflowed the model's context. "
                f"Lower `max_context_chars` for this seat in config.yaml.")
    return f"openrouter[{model_slug}] returned HTTP {status}"


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
            f"could not extract JSON from openrouter[{agent_name}] response: {e}",
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


__all__ = ["OpenRouterAdapter"]
