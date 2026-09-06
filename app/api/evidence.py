"""Evidence search, capture, and inspection endpoints."""

from __future__ import annotations

import json
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.config import get_config
from app.database import connect
from app.services.evidence import (
    ConfiguredJsonSearchProvider, capture_urls, list_snapshots,
)


router = APIRouter(prefix="/api/evidence", tags=["evidence"])


class EvidenceFetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    urls: list[str] = Field(min_length=1, max_length=20)
    task_id: Optional[str] = None
    project_id: Optional[str] = None


class EvidenceSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=1000)
    limit: int = Field(default=5, ge=1, le=10)
    task_id: Optional[str] = None
    project_id: Optional[str] = None


def _validate_owners(task_id: str | None, project_id: str | None) -> None:
    with connect() as conn:
        if task_id:
            row = conn.execute("SELECT permissions_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="task not found")
            if not json.loads(row["permissions_json"]).get("can_access_network"):
                raise HTTPException(status_code=403, detail="task does not allow network evidence acquisition")
        if project_id:
            row = conn.execute(
                "SELECT id FROM decision_projects WHERE id = ? AND archived_at IS NULL", (project_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="project not found")


@router.post("/fetch")
async def fetch_evidence(body: EvidenceFetchRequest):
    _validate_owners(body.task_id, body.project_id)
    try:
        snapshots = await capture_urls(
            body.urls, get_config().evidence, task_id=body.task_id, project_id=body.project_id,
        )
    except (ValueError, OSError, httpx.HTTPError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"evidence": snapshots}


@router.post("/search")
async def search_evidence(body: EvidenceSearchRequest):
    _validate_owners(body.task_id, body.project_id)
    config = get_config()
    provider = ConfiguredJsonSearchProvider(config)
    try:
        results = await provider.search(body.query, body.limit)
        snapshots = await capture_urls(
            [result["url"] for result in results], config.evidence,
            task_id=body.task_id, project_id=body.project_id,
        )
    except (ValueError, OSError, httpx.HTTPError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"query": body.query, "results": results, "evidence": snapshots}


@router.get("")
async def get_evidence(task_id: str | None = None, project_id: str | None = None):
    if not task_id and not project_id:
        raise HTTPException(status_code=400, detail="task_id or project_id is required")
    return {"evidence": list_snapshots(task_id=task_id, project_id=project_id)}


__all__ = ["router"]
