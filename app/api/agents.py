"""Agent listing, connection test, and pricing endpoints."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException

from app.services import agent_registry

router = APIRouter(prefix="/api/agents", tags=["agents"])

logger = logging.getLogger("switchboard.agents")


@router.get("")
async def list_agents(include_internal: bool = False) -> dict:
    """List registered agents. Hides adapters marked `internal=True` (e.g. fake) by default."""
    names = agent_registry.list_names()
    if include_internal:
        return {"agents": names}
    public = [n for n in names if not agent_registry.get(n).internal]
    return {"agents": public}


@router.post("/{agent_name}/test")
async def test_agent(agent_name: str) -> dict:
    try:
        adapter = agent_registry.get(agent_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"agent {agent_name} not registered")
    result = await adapter.test_connection()
    return result.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Pricing endpoint — live OpenRouter prices for the OR-backed seats, plus a
# "subscription" note for CLI / Ollama Cloud seats which don't bill per-token.
# ---------------------------------------------------------------------------

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_OR_CACHE_TTL = 300  # seconds; OpenRouter pricing rarely changes more than hourly
_or_cache: dict[str, Any] = {"data": None, "fetched_at": 0.0, "error": None}

# Per-turn estimate basis. Rough average of a conclave/consult turn: 5K input,
# 1K output. Documented in the response so users know what's being assumed.
_EST_INPUT_TOKENS = 5000
_EST_OUTPUT_TOKENS = 1000

# CLI seats can be authenticated two ways:
#   - OAuth / subscription (default) — `claude /login`, `codex login`, etc.
#   - API key — set the corresponding env var
# Each frontier CLI uses API mode whenever its env var is set; otherwise it
# falls back to OAuth. We detect the env var at request time. The model
# itself is declared per-CLI in config.yaml (agents.<name>.model_slug) —
# each CLI represents one specific model in the conclave.
_CLI_API_ENV_VARS = {
    "claude-code": ["ANTHROPIC_API_KEY"],
    "codex": ["OPENAI_API_KEY"],
    "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
}


def _detect_cli_auth_from_files(name: str) -> tuple[Optional[str], Optional[str]]:
    """Read each CLI's auth state file and return (mode, source).

    mode is one of: "subscription", "api", or None (unknown — no signal in
    the file, or no file at all).

    Each CLI uses a different file and a different schema:
        Codex        → ~/.codex/auth.json carries an explicit `auth_mode` field
                       ("apikey" = API mode; anything else = subscription).
        Gemini       → ~/.gemini/settings.json has `security.auth.selectedType`
                       ("gemini-api-key" / "api-key" = API mode;
                        "oauth-personal" etc. = subscription).
        Claude Code  → ~/.claude/.credentials.json existence currently signals
                       OAuth-based subscription. (Claude Code's CLI removes
                       this file on /logout, so existence is a reasonable
                       proxy. If a future CLI version stores explicit auth
                       mode, extend the logic here.)
    """
    home = Path.home()
    if name == "codex":
        p = home / ".codex" / "auth.json"
        if p.exists() and p.stat().st_size > 0:
            try:
                import json
                data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
                mode = data.get("auth_mode")
                if isinstance(mode, str):
                    if mode.lower() in ("apikey", "api-key", "api_key"):
                        return ("api", f"~/.codex/auth.json (auth_mode={mode})")
                    return ("subscription", f"~/.codex/auth.json (auth_mode={mode})")
            except Exception:
                pass
            return ("subscription", "~/.codex/auth.json")
    elif name == "gemini":
        settings = home / ".gemini" / "settings.json"
        if settings.exists():
            try:
                import json
                data = json.loads(settings.read_text(encoding="utf-8", errors="replace"))
                sel = ((data.get("security") or {}).get("auth") or {}).get("selectedType")
                if sel:
                    sel_low = sel.lower()
                    if "api" in sel_low or "key" in sel_low:
                        return ("api", f"~/.gemini/settings.json (selectedType={sel})")
                    return ("subscription", f"~/.gemini/settings.json (selectedType={sel})")
            except Exception:
                pass
        # Fallback: oauth_creds.json existence ONLY when settings.json had no
        # selectedType (the credentials file persists across auth-mode changes,
        # so we can't trust it once selectedType has spoken).
        p = home / ".gemini" / "oauth_creds.json"
        if p.exists() and p.stat().st_size > 0:
            return ("subscription", "~/.gemini/oauth_creds.json")
    elif name == "claude-code":
        p = home / ".claude" / ".credentials.json"
        if p.exists() and p.stat().st_size > 0:
            return ("subscription", "~/.claude/.credentials.json")
    return (None, None)


def _detect_cli_auth_mode(name: str) -> tuple[str, Optional[str]]:
    """Return (mode, signal) — "subscription" or "api" plus the source.

    Resolution order:
        1. The CLI's own auth-state file (most authoritative — reflects what
           the user actually selected via /login or /auth).
        2. Env var presence (fallback signal of API intent if no file says).
        3. Default to subscription.
    """
    file_mode, file_source = _detect_cli_auth_from_files(name)
    if file_mode in ("api", "subscription"):
        return (file_mode, file_source)
    env_vars = _CLI_API_ENV_VARS.get(name, [])
    for var in env_vars:
        if os.environ.get(var):
            return ("api", var)
    return ("subscription", None)


# ---------------------------------------------------------------------------
# Best-effort detection of the model each CLI is *actually* set to use.
# Each CLI stores model preferences differently; some don't persist them.
# We surface (detected_slug, source) so the dashboard can flag drift between
# what's declared in config.yaml and what's on disk in the CLI's settings.
# ---------------------------------------------------------------------------

def _normalize_cli_model_to_slug(cli: str, raw: str) -> str:
    """Best-effort map of a CLI's bare model name to an OpenRouter slug.

    Each CLI stores model names in its own dialect:
        Codex          "gpt-5.5"           → "openai/gpt-5.5"
        Claude Code    "claude-opus-4-7"   → "anthropic/claude-opus-4.7"
                       "opus"              → "anthropic/claude-opus-4.7"
                       "sonnet"            → "anthropic/claude-sonnet-4.6"
        Gemini         "gemini-2.5-pro"    → "google/gemini-2.5-pro"

    The mapping is intentionally narrow and conservative; unrecognized names
    pass through with a provider prefix so OpenRouter pricing lookup either
    succeeds or returns nothing (in which case we show "(slug not in catalog)").
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    # Already prefixed (slash present)
    if "/" in raw:
        return raw

    if cli == "codex":
        return "openai/" + raw

    if cli == "claude-code":
        # Common Claude Code aliases used by `/model <alias>` and settings.json
        alias_map = {
            "opus": "anthropic/claude-opus-4.7",
            "sonnet": "anthropic/claude-sonnet-4.6",
            "haiku": "anthropic/claude-haiku-4.5",
            "default": "anthropic/claude-sonnet-4.6",
        }
        if raw.lower() in alias_map:
            return alias_map[raw.lower()]
        # Claude CLI sometimes uses dashes (claude-opus-4-7) where OpenRouter
        # uses dots (claude-opus-4.7). Convert the last numeric segment.
        # e.g. "claude-opus-4-7" → "claude-opus-4.7"
        import re
        normalized = re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", raw)
        return "anthropic/" + normalized

    if cli == "gemini":
        return "google/" + raw

    return raw


def _detect_codex_model() -> Optional[tuple[str, str]]:
    """Read ~/.codex/config.toml for `model = "..."`. Returns (slug, source) or None."""
    cfg_path = Path.home() / ".codex" / "config.toml"
    if not cfg_path.exists():
        return None
    try:
        text = cfg_path.read_text(encoding="utf-8", errors="replace")
        # Cheap parser — only the top-level `model = "..."` line matters here.
        # We avoid pulling in tomllib import for one regex.
        import re
        m = re.search(r'^\s*model\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if not m:
            return None
        raw = m.group(1)
        return (_normalize_cli_model_to_slug("codex", raw), "~/.codex/config.toml")
    except Exception as e:  # noqa: BLE001
        logger.warning("codex model detection failed: %s", e)
        return None


def _detect_claude_code_model() -> Optional[tuple[str, str]]:
    """Read ~/.claude/settings.json for `model`. Returns (slug, source) or None."""
    cfg_path = Path.home() / ".claude" / "settings.json"
    if not cfg_path.exists():
        return None
    try:
        import json
        data = json.loads(cfg_path.read_text(encoding="utf-8", errors="replace"))
        raw = data.get("model")
        if not isinstance(raw, str) or not raw:
            return None
        return (_normalize_cli_model_to_slug("claude-code", raw), "~/.claude/settings.json")
    except Exception as e:  # noqa: BLE001
        logger.warning("claude-code model detection failed: %s", e)
        return None


def _detect_cli_model(name: str) -> Optional[tuple[str, str]]:
    """Return (detected_slug, source_path) for the CLI, or None if not detectable."""
    if name == "codex":
        return _detect_codex_model()
    if name == "claude-code":
        return _detect_claude_code_model()
    # Gemini CLI doesn't persist a model selection in a known stable location.
    return None


async def _get_openrouter_prices() -> dict[str, dict[str, Any]]:
    """Fetch OpenRouter's /models catalog and return a `{slug -> info}` index.

    Cached in-process for `_OR_CACHE_TTL` seconds. On a fetch error, returns
    the last successful payload (if any), or an empty dict.
    """
    now = time.time()
    if _or_cache["data"] is not None and (now - float(_or_cache["fetched_at"])) < _OR_CACHE_TTL:
        return _or_cache["data"]
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(_OPENROUTER_MODELS_URL)
            r.raise_for_status()
            payload = r.json()
        models = payload.get("data", []) or []
        idx: dict[str, dict[str, Any]] = {}
        for m in models:
            mid = m.get("id")
            if not isinstance(mid, str) or not mid:
                continue
            pricing = m.get("pricing", {}) or {}
            idx[mid] = {
                "prompt": pricing.get("prompt"),
                "completion": pricing.get("completion"),
                "context_length": m.get("context_length"),
                "name": m.get("name"),
            }
        _or_cache["data"] = idx
        _or_cache["fetched_at"] = now
        _or_cache["error"] = None
        return idx
    except Exception as e:  # noqa: BLE001
        logger.warning("openrouter pricing fetch failed: %s", e)
        _or_cache["error"] = str(e)
        return _or_cache["data"] or {}


def _safe_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


@router.get("/pricing")
async def agents_pricing(include_internal: bool = False) -> dict:
    """Return every registered agent with its pricing info.

    Shape per item:
        {
          "name": "kimi",
          "kind": "openrouter" | "ollama_cloud" | "cli" | "internal",
          "model_id": "moonshotai/kimi-k2.6" | "qwen3-coder:480b-cloud" | None,
          "context_length": 262142 | None,
          "input_per_million_usd": 0.73 | None,
          "output_per_million_usd": 3.49 | None,
          "per_turn_estimate_usd": 0.00715 | None,
          "billing": "per-token" | "subscription",
          "note": "..." | None,
        }
    """
    or_prices = await _get_openrouter_prices()

    items: list[dict[str, Any]] = []
    for name in agent_registry.list_names():
        try:
            adapter = agent_registry.get(name)
        except KeyError:
            continue
        if not include_internal and adapter.internal:
            continue

        cls_name = type(adapter).__name__
        item: dict[str, Any] = {
            "name": name,
            "kind": "cli",
            "model_id": None,
            "context_length": None,
            "input_per_million_usd": None,
            "output_per_million_usd": None,
            "per_turn_estimate_usd": None,
            "billing": "subscription",
            "note": None,
        }

        if cls_name == "OpenRouterAdapter":
            item["kind"] = "openrouter"
            item["billing"] = "per-token"
            slug = getattr(adapter, "model_slug", None)
            item["model_id"] = slug
            pinfo = or_prices.get(slug or "")
            if pinfo:
                input_per_token = _safe_float(pinfo.get("prompt"))
                output_per_token = _safe_float(pinfo.get("completion"))
                if input_per_token is not None:
                    item["input_per_million_usd"] = round(input_per_token * 1_000_000, 4)
                if output_per_token is not None:
                    item["output_per_million_usd"] = round(output_per_token * 1_000_000, 4)
                if input_per_token is not None and output_per_token is not None:
                    item["per_turn_estimate_usd"] = round(
                        input_per_token * _EST_INPUT_TOKENS
                        + output_per_token * _EST_OUTPUT_TOKENS,
                        5,
                    )
                item["context_length"] = pinfo.get("context_length")
            else:
                item["note"] = "Not found in OpenRouter catalog (slug may be stale)"
        elif cls_name == "OllamaCloudAdapter":
            item["kind"] = "ollama_cloud"
            item["model_id"] = getattr(adapter, "model_id", None)
            item["note"] = "Ollama Cloud subscription — no public per-token rate"
        elif cls_name == "FakeAdapter":
            item["kind"] = "internal"
            item["note"] = "Test adapter (no model)"
        else:
            # CLI seats (Codex, Gemini, Claude Code). The user *declares* the
            # intended model in config.yaml (agents.<name>.model_slug). We
            # also try to *detect* what the CLI is actually configured for
            # (Codex reads ~/.codex/config.toml; Claude Code reads
            # ~/.claude/settings.json if present; Gemini has no persistent
            # selection). When declared ≠ detected, flag drift so the user
            # can fix either the config or the CLI.
            from app.config import load_config
            cfg = load_config()
            agent_cfg = (cfg.agents or {}).get(name)
            declared_slug = getattr(agent_cfg, "model_slug", None) if agent_cfg else None

            detected = _detect_cli_model(name)
            if detected:
                item["detected_model_slug"], item["detected_source"] = detected
            else:
                item["detected_model_slug"] = None
                item["detected_source"] = None
            item["declared_model_slug"] = declared_slug

            # Drift = we have both a declared and a detected slug, and they differ.
            item["drift"] = bool(
                declared_slug and item["detected_model_slug"]
                and declared_slug != item["detected_model_slug"]
            )

            # The pricing reference uses the DETECTED slug when available
            # (most accurate), falling back to the declared slug otherwise.
            pricing_slug = item["detected_model_slug"] or declared_slug

            mode, signal = _detect_cli_auth_mode(name)
            if mode == "api":
                item["kind"] = "cli-api"
                item["billing"] = "per-token"
                item["model_id"] = pricing_slug
                pinfo = or_prices.get(pricing_slug or "")
                if pinfo:
                    input_per_token = _safe_float(pinfo.get("prompt"))
                    output_per_token = _safe_float(pinfo.get("completion"))
                    if input_per_token is not None:
                        item["input_per_million_usd"] = round(input_per_token * 1_000_000, 4)
                    if output_per_token is not None:
                        item["output_per_million_usd"] = round(output_per_token * 1_000_000, 4)
                    if input_per_token is not None and output_per_token is not None:
                        item["per_turn_estimate_usd"] = round(
                            input_per_token * _EST_INPUT_TOKENS
                            + output_per_token * _EST_OUTPUT_TOKENS,
                            5,
                        )
                    item["context_length"] = pinfo.get("context_length")
                    item["note"] = "API mode (" + signal + ")."
                elif pricing_slug:
                    item["note"] = (
                        "API mode (" + signal + "). "
                        + pricing_slug + " not found in OpenRouter catalog — verify the slug."
                    )
                else:
                    item["note"] = (
                        "API mode (" + signal + "), but no model_slug declared in config.yaml "
                        "for this agent. Add agents." + name + ".model_slug to see pricing."
                    )
            else:
                item["kind"] = "cli"
                item["billing"] = "subscription"
                item["model_id"] = pricing_slug  # show what would be billed if API mode
                if signal:
                    item["note"] = (
                        "Subscription mode (" + signal + ") — no per-token charges."
                    )
                else:
                    vars_to_set = _CLI_API_ENV_VARS.get(name, [])
                    hint = (" Set " + "/".join(vars_to_set) + " to switch to API billing.") if vars_to_set else ""
                    item["note"] = "Subscription billing — no per-token charges." + hint

        items.append(item)

    return {
        "items": items,
        "currency": "USD",
        "estimate_basis": {
            "input_tokens": _EST_INPUT_TOKENS,
            "output_tokens": _EST_OUTPUT_TOKENS,
        },
        "openrouter_cache_age_seconds": int(time.time() - float(_or_cache["fetched_at"]))
        if _or_cache["data"] is not None else None,
        "openrouter_error": _or_cache.get("error"),
    }
