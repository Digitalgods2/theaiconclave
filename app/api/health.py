"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from app.database import connect

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health() -> dict:
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ok"}
    except Exception as e:  # noqa: BLE001
        return {"status": "degraded", "error": str(e)}
