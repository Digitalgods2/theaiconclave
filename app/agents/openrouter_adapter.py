"""OpenRouter adapter — pay-per-token access to open-weight / non-frontier models.

OpenRouter is a unified OpenAI-compatible gateway to many model providers
(DeepSeek, Qwen, GLM/Z.ai, Kimi/Moonshot, MiniMax, …). The frontier-class
open-weight models here are pay-per-token (no subscription) and cheap — a
conclave turn costs cents at most.

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

import logging
import os
import re
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

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

# Per-process cache of each model's true input-char ceiling, learned from a 400
# "maximum context length" response. Subsequent calls use the learned (lower)
# ceiling, so the user never has to edit config.yaml. Chars, not tokens —
# already converted at write time. Keyed by model_slug.
_LEARNED_CEILINGS: dict[str, int] = {}

# Code-heavy prompts tokenise at roughly 3 chars/token; multiply by this and
# pad down 15% for tokenizer overhead + response headroom when converting a
# token-limit reported by the API into a usable char-budget.
_TOKENS_TO_CHARS = 3
_CEILING_SAFETY = 0.85


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

    def _effective_max_chars(self) -> int:
        """Return the smaller of the configured ceiling and the learned-from-API
        ceiling (if any). The learned value is per-process and per-model_slug."""
        learned = _LEARNED_CEILINGS.get(self.model_slug)
        if learned is not None:
            return min(self.max_context_chars, learned)
        return self.max_context_chars

    def _sandbox_path_from(self, ctx: AdapterContext) -> Optional[str]:
        try:
            v = ctx.task.context.extra.get("sandbox_path")
        except Exception:  # noqa: BLE001
            return None
        return str(v) if v else None

    def _compose_prompt(self, base_prompt: str, sandbox_path: Optional[str],
                       ceiling_chars: int) -> str:
        """Return base_prompt, optionally with an inlined sandbox section sized
        to fit under `ceiling_chars` minus headroom. Returns base_prompt
        unchanged if there's no sandbox or no budget left."""
        if not sandbox_path:
            return base_prompt
        budget = max(0, ceiling_chars - len(base_prompt) - _SANDBOX_HEADROOM)
        if budget < 2000:
            return base_prompt
        section = build_sandbox_section(sandbox_path, budget)
        if not section:
            return base_prompt
        return base_prompt + "\n\n" + section

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

    async def _post_chat(self, prompt: str, key: str, timeout_seconds: int):
        """One HTTP POST; returns the httpx Response or raises AdapterError on
        transport-level failures. The caller inspects status_code."""
        payload: dict[str, Any] = {
            "model": self.model_slug,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "response_format": {"type": "json_object"},
            "provider": {"data_collection": self.data_collection},
        }
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                return await client.post(
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

    async def _invoke(self, base_prompt: str, timeout_seconds: int,
                      sandbox_path: Optional[str] = None) -> str:
        """Send `base_prompt` (with the sandbox inlined under the current
        ceiling, if a sandbox path is supplied). On HTTP 400 "maximum context
        length", parse the model's real limit from the body, cache it, shrink
        the prompt to fit, and retry ONCE. Subsequent calls in this process use
        the cached ceiling automatically — the user doesn't have to edit config.
        """
        key = self._api_key()
        if key is None:
            raise AdapterError(
                ErrorCode.AGENT_UNAVAILABLE,
                f"{_API_KEY_ENV} not set; configure an OpenRouter API key (env var or Settings → API Keys)",
            )

        ceiling = self._effective_max_chars()
        prompt = self._compose_prompt(base_prompt, sandbox_path, ceiling)
        resp = await self._post_chat(prompt, key, timeout_seconds)

        # Auto-shrink + retry on context overflow. Run this check on EVERY
        # response (HTTP 400 with the OpenAI-style body, OR HTTP 200 with a
        # nested provider error like vLLM's "max_num_tokens (32768)"). One
        # retry only — if that one also overflows, surface the actionable error.
        body_for_check: Any = None
        try:
            body_for_check = resp.json()
        except Exception:  # noqa: BLE001
            body_for_check = None
        limit_tokens = _check_overflow_response(resp.status_code, resp.text or "", body_for_check)
        if limit_tokens:
            learned_chars = int(limit_tokens * _TOKENS_TO_CHARS * _CEILING_SAFETY)
            _LEARNED_CEILINGS[self.model_slug] = learned_chars
            if learned_chars < ceiling:
                logger.info(
                    "openrouter[%s] context overflow: learned ceiling %d chars (was using %d, "
                    "real limit %d tokens); retrying once with a tighter prompt.",
                    self.model_slug, learned_chars, ceiling, limit_tokens,
                )
                new_ceiling = min(self.max_context_chars, learned_chars)
                prompt = self._compose_prompt(base_prompt, sandbox_path, new_ceiling)
                if len(prompt) < len(base_prompt) + 2:
                    # No sandbox attached to trim — base prompt itself overflows.
                    raise AdapterError(
                        ErrorCode.AGENT_ERROR,
                        _overflow_message(self.model_slug, limit_tokens, learned_chars,
                                          had_sandbox=False),
                        details={"status_code": resp.status_code,
                                 "body_tail": (resp.text or "")[-2000:],
                                 "learned_ceiling_chars": learned_chars,
                                 "real_token_limit": limit_tokens},
                    )
                resp = await self._post_chat(prompt, key, timeout_seconds)
                # Re-parse body for the post-retry flow below.
                try:
                    body_for_check = resp.json()
                except Exception:  # noqa: BLE001
                    body_for_check = None

        if resp.status_code != 200:
            raise AdapterError(
                ErrorCode.AGENT_ERROR,
                _http_error_message(self.model_slug, resp.status_code, resp.text),
                details={"status_code": resp.status_code, "body_tail": resp.text[-2000:]},
            )

        body = body_for_check
        if body is None:
            raise AdapterError(
                ErrorCode.AGENT_ERROR,
                f"openrouter[{self.model_slug}] response was not JSON",
                details={"body_tail": (resp.text or "")[-2000:]},
            )

        # OpenRouter sometimes nests an error in a 200 body (the vLLM-via-502
        # case we just retried, or anything else the provider surfaces).
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
        text = await self._invoke(prompt, ctx.timeout_seconds,
                                  sandbox_path=self._sandbox_path_from(ctx))
        data = _parse_and_coerce(text, ctx.task_id, self.name, role="primary",
                                 default_message_type=MessageType.PRIMARY_PROPOSAL.value)
        return PrimaryResponse.model_validate(data)

    async def run_consultant(self, ctx: AdapterContext) -> ConsultantCritique:
        prompt = build_consultant_prompt(task=ctx.task, task_id=ctx.task_id, agent_name=self.name,
                                         prior_messages=ctx.prior_messages)
        text = await self._invoke(prompt, ctx.timeout_seconds,
                                  sandbox_path=self._sandbox_path_from(ctx))
        data = _parse_and_coerce(text, ctx.task_id, self.name, role="consultant",
                                 default_message_type=MessageType.CONSULTANT_CRITIQUE.value)
        return ConsultantCritique.model_validate(data)

    async def run_final(self, ctx: AdapterContext) -> PrimaryResponse:
        prompt = build_final_prompt(task=ctx.task, task_id=ctx.task_id, agent_name=self.name,
                                    prior_messages=ctx.prior_messages)
        text = await self._invoke(prompt, ctx.timeout_seconds,
                                  sandbox_path=self._sandbox_path_from(ctx))
        data = _parse_and_coerce(text, ctx.task_id, self.name, role="primary",
                                 default_message_type=MessageType.PRIMARY_FINAL.value)
        return PrimaryResponse.model_validate(data)

    async def run_peer(self, ctx: AdapterContext) -> PeerAnswer:
        prompt = build_peer_prompt(ctx.task, ctx.task_id, self.name)
        text = await self._invoke(prompt, ctx.timeout_seconds,
                                  sandbox_path=self._sandbox_path_from(ctx))
        data = _parse_and_coerce(text, ctx.task_id, self.name, role="peer",
                                 default_message_type=MessageType.PEER_ANSWER.value)
        return PeerAnswer.model_validate(data)

    async def run_conclave_turn(self, ctx: AdapterContext) -> ConclaveTurn:
        others = [c for c in ctx.task.consultants if c != self.name]
        prompt = build_conclave_prompt(task=ctx.task, task_id=ctx.task_id, agent_name=self.name,
                                       prior_messages=ctx.prior_messages, other_participants=others)
        text = await self._invoke(prompt, ctx.timeout_seconds,
                                  sandbox_path=self._sandbox_path_from(ctx))
        data = _parse_and_coerce(text, ctx.task_id, self.name, role="participant",
                                 default_message_type=MessageType.CONCLAVE_TURN.value)
        return ConclaveTurn.model_validate(data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Multiple overflow-error formats seen in the wild routed through OpenRouter:
#   - OpenAI-style:  "maximum context length is 163840 tokens. However, you
#                     requested about 212253 tokens (212253 of text input)."
#   - vLLM-style:    "The sum of prompt length (95448.0), query length (0)
#                     should not exceed max_num_tokens (32768)"
#   - Generic loose: "...maximum context length is 50000 tokens..."
# All three end up giving us a single integer to learn.
_CTX_LIMIT_RE = re.compile(
    r"maximum context length is\s+(?P<limit>[\d,]+)\s+tokens.*?requested\s+about\s+(?P<used>[\d,]+)\s+tokens",
    re.IGNORECASE | re.DOTALL,
)
_CTX_LIMIT_LOOSE_RE = re.compile(
    r"maximum context length is\s+(?P<limit>[\d,]+)\s+tokens",
    re.IGNORECASE,
)
_CTX_LIMIT_VLLM_RE = re.compile(
    r"max_num_tokens\s*\(\s*(?P<limit>\d+)\s*\)",
    re.IGNORECASE,
)
# Combined overflow indicators — these strings appearing anywhere in the error
# text strongly suggest a context-window failure, even if the limit is unparseable.
_OVERFLOW_HINTS = ("maximum context length", "max_num_tokens", "context window",
                   "context length exceeded", "context_length_exceeded")


def _parse_token_limit(text: str) -> Optional[int]:
    """Return the model's real token limit from an error message, or None."""
    if not text:
        return None
    for rx in (_CTX_LIMIT_VLLM_RE, _CTX_LIMIT_RE, _CTX_LIMIT_LOOSE_RE):
        m = rx.search(text)
        if m:
            try:
                return int(m.group("limit").replace(",", ""))
            except (ValueError, IndexError):
                continue
    return None


def _looks_like_overflow(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(h in low for h in _OVERFLOW_HINTS)


def _check_overflow_response(status: int, body_text: str,
                             body_json: Any) -> Optional[int]:
    """If the response represents a context overflow (HTTP 400 with an OpenAI-
    style body, OR HTTP 200 with a nested provider error in body.error.message),
    return the model's real token limit; else None."""
    candidates: list[str] = []
    if body_text:
        candidates.append(body_text)
    if isinstance(body_json, dict):
        err = body_json.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, str):
                candidates.append(msg)
    for text in candidates:
        if _looks_like_overflow(text):
            tok = _parse_token_limit(text)
            if tok and tok > 0:
                return tok
    return None


def _learn_ceiling_from_body(model_slug: str, body: str) -> Optional[int]:
    """Back-compat helper used by tests: parse a raw body string for an overflow
    limit and cache it as a char budget. Returns the cached chars or None."""
    if not body:
        return None
    tok = _parse_token_limit(body)
    if not tok or tok <= 0:
        if not _looks_like_overflow(body):
            return None
        return None  # body says overflow but no parseable number
    chars = int(tok * _TOKENS_TO_CHARS * _CEILING_SAFETY)
    _LEARNED_CEILINGS[model_slug] = chars
    return chars


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
    # Any overflow-shaped body (HTTP 400 OpenAI-style OR HTTP 200 nested vLLM-style).
    if _looks_like_overflow(body):
        tok = _parse_token_limit(body)
        if tok:
            recommended = int(tok * _TOKENS_TO_CHARS * _CEILING_SAFETY)
            return (f"openrouter[{model_slug}]: prompt overflowed the model's context "
                    f"(real limit {tok:,} tokens). Lower `max_context_chars` for "
                    f"this seat in config.yaml — try around {recommended:,} or less.")
        return (f"openrouter[{model_slug}]: prompt overflowed the model's context. "
                f"Lower `max_context_chars` for this seat in config.yaml.")
    return f"openrouter[{model_slug}] returned HTTP {status}"


def _overflow_message(model_slug: str, limit_tokens: int, learned_chars: int,
                      had_sandbox: bool) -> str:
    """Actionable message for the case where the retry can't help (no sandbox to trim)."""
    if had_sandbox:
        return (f"openrouter[{model_slug}]: prompt still overflowed after a sandbox-trim "
                f"retry (real limit {limit_tokens:,} tokens, learned {learned_chars:,} chars). "
                f"The base prompt + transcript is itself too large.")
    return (f"openrouter[{model_slug}]: prompt overflowed the model's context "
            f"(real limit {limit_tokens:,} tokens). No sandbox attached to trim; the "
            f"base prompt + transcript exceeds the limit. Set `max_context_chars` for "
            f"this seat to around {learned_chars:,} in config.yaml.")


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
