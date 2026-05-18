"""Health check endpoint.

Exposes:
- `status`: overall service health (DB connectivity)
- `seats`: per-adapter readiness (DR0017) — each entry includes `available`,
  a short machine-stable `reason`, and a user-facing `hint` with remediation
  text when unavailable. The dashboard surfaces this so users can fix a
  missing CLI or API key without combing the logs.

Seat readiness checks are cached for `_SEATS_CACHE_TTL_SECONDS` so rapid
dashboard polling doesn't spawn subprocesses on every call.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from fastapi import APIRouter

from app.agents import base as agent_base
from app.database import connect
from app.services import agent_registry

router = APIRouter(prefix="/api", tags=["health"])

_SEATS_CACHE_TTL_SECONDS = 30.0
_seats_cache: dict[str, Any] = {"expires_at": 0.0, "payload": []}
_seats_cache_lock = asyncio.Lock()


def _adapter_kind(adapter) -> str:
    """Classify an adapter for the dashboard's per-seat panel.

    `cli` = subprocess-backed (codex / claude-code / gemini).
    `api` = HTTP-backed (openrouter seats).
    `test` = internal/fake adapters (excluded from the user-facing seats array).
    """
    if getattr(adapter, "internal", False):
        return "test"
    if hasattr(adapter, "model_slug"):
        return "api"
    return "cli"


async def _readiness_for(adapter) -> agent_base.Readiness:
    try:
        return await adapter.readiness()
    except Exception as e:  # noqa: BLE001
        return agent_base.Readiness(
            available=False,
            reason="readiness_check_failed",
            hint=f"Internal error checking readiness for '{adapter.name}': {e}",
        )


async def _seats_payload() -> list[dict]:
    """Compute the seats array now. Internal/test adapters are filtered out."""
    adapters = [agent_registry.get(name) for name in agent_registry.list_names()]
    user_facing = [a for a in adapters if not getattr(a, "internal", False)]

    results = await asyncio.gather(*(_readiness_for(a) for a in user_facing))

    return [
        {
            "name": adapter.name,
            "kind": _adapter_kind(adapter),
            "available": r.available,
            "reason": r.reason,
            "hint": r.hint,
        }
        for adapter, r in zip(user_facing, results)
    ]


async def _cached_seats() -> list[dict]:
    now = time.monotonic()
    if now < _seats_cache["expires_at"]:
        return _seats_cache["payload"]
    async with _seats_cache_lock:
        # Re-check inside the lock — another coroutine may have populated.
        now = time.monotonic()
        if now < _seats_cache["expires_at"]:
            return _seats_cache["payload"]
        payload = await _seats_payload()
        _seats_cache["payload"] = payload
        _seats_cache["expires_at"] = now + _SEATS_CACHE_TTL_SECONDS
        return payload


def _invalidate_seats_cache() -> None:
    """Test-only: flush the cache so the next call recomputes."""
    _seats_cache["expires_at"] = 0.0
    _seats_cache["payload"] = []


@router.get("/health")
async def health() -> dict:
    """Service-level health check, plus per-seat readiness."""
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
        status = "ok"
        error: Optional[str] = None
    except Exception as e:  # noqa: BLE001
        status = "degraded"
        error = str(e)

    seats = await _cached_seats()

    out: dict = {"status": status, "seats": seats}
    if error is not None:
        out["error"] = error
    return out
