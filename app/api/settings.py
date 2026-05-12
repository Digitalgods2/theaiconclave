"""Runtime settings API — currently the API-key store used by the dashboard.

Precedence rule (matches the adapters): an environment variable always wins;
the database value is the fallback. So a user who has `OLLAMA_API_KEY` exported
sees "source: env" here, and anything stored in the DB is dormant until the
env var is unset.

Endpoints never return a stored secret except `/reveal` (the dashboard's
eyeball). The env-var value is never revealed — it isn't ours to leak.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Body

from app.services import settings_store

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Maps a settings-API key name to (db_settings_key, env_var_name).
_API_KEYS = {
    "ollama": ("ollama_api_key", "OLLAMA_API_KEY"),
    "openrouter": ("openrouter_api_key", "OPENROUTER_API_KEY"),
}


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def _mask(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 4:
        return "•" * len(value)
    return "•" * min(len(value) - 4, 12) + value[-4:]


def _status(db_key: str, env_name: str) -> dict[str, Any]:
    env_v = _env(env_name)
    if env_v:
        return {"set": True, "source": "env", "masked": _mask(env_v)}
    db_v = settings_store.get_secret(db_key)
    if db_v:
        return {"set": True, "source": "db", "masked": _mask(db_v)}
    return {"set": False, "source": "none", "masked": None}


@router.get("/api-keys")
async def get_api_keys() -> dict[str, Any]:
    """Report which API keys are set and where each one comes from (env > db > none)."""
    return {name: _status(db_key, env_name) for name, (db_key, env_name) in _API_KEYS.items()}


@router.post("/api-keys/{name}")
async def set_api_key(name: str, body: dict = Body(...)) -> dict[str, Any]:
    """Store (or clear) an API key in the database.

    Body: {"value": "sk-..."}  -> stores it.
          {"value": ""} or {"value": null} -> clears the stored value.

    Note: if the corresponding env var is set, the stored value is ignored at
    runtime (env wins) — the response includes the resulting effective source.
    """
    if name not in _API_KEYS:
        return {"ok": False, "error": f"unknown api key: {name}"}
    db_key, env_name = _API_KEYS[name]
    value = body.get("value")
    if value is None or (isinstance(value, str) and value.strip() == ""):
        settings_store.delete_secret(db_key)
        action = "cleared"
    else:
        settings_store.set_secret(db_key, str(value).strip())
        action = "saved"
    return {"ok": True, "action": action, "status": _status(db_key, env_name)}


@router.get("/api-keys/{name}/reveal")
async def reveal_api_key(name: str) -> dict[str, Any]:
    """Return the plaintext of a *database-stored* key, for the dashboard eyeball.

    If the key is sourced from the environment variable, returns value: null with
    a note — env-var secrets are not echoed back.
    """
    if name not in _API_KEYS:
        return {"value": None, "error": f"unknown api key: {name}"}
    db_key, env_name = _API_KEYS[name]
    if _env(env_name):
        return {"value": None, "source": "env",
                "note": f"set via the {env_name} environment variable; not stored here"}
    return {"value": settings_store.get_secret(db_key), "source": "db" if settings_store.get_secret(db_key) else "none"}
