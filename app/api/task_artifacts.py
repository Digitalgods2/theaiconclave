"""Task-artifact inspection, download, preview, and apply routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from app.database import connect
from app.services.artifacts import (
    apply_artifact_to_project,
    get_artifact,
    list_artifacts,
    preview_artifact_apply,
    read_artifact_bytes,
)


router = APIRouter(prefix="/api/tasks", tags=["task-artifacts"])


class ArtifactApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool
    expected_target_sha256: str
    allow_overwrite: bool = False


@router.get("/{task_id}/artifacts")
async def get_task_artifacts(task_id: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {"task_id": task_id, "artifacts": list_artifacts(task_id, include_content=True)}


@router.get("/{task_id}/artifacts/{artifact_id}/download")
async def download_artifact(task_id: str, artifact_id: str) -> Response:
    try:
        artifact = get_artifact(task_id, artifact_id)
        data = read_artifact_bytes(task_id, artifact_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail="artifact not found") from e
    return Response(
        content=data,
        media_type=artifact["mime_type"] or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{artifact["filename"]}"'},
    )


@router.get("/{task_id}/artifacts/{artifact_id}/apply-preview")
async def preview_apply_artifact(task_id: str, artifact_id: str) -> dict[str, Any]:
    try:
        return preview_artifact_apply(task_id, artifact_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e) or "artifact not found") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/{task_id}/artifacts/{artifact_id}/apply")
async def apply_artifact(
    task_id: str, artifact_id: str, request: ArtifactApplyRequest,
) -> dict[str, Any]:
    try:
        return apply_artifact_to_project(
            task_id, artifact_id,
            confirm=request.confirm,
            expected_target_sha256=request.expected_target_sha256,
            allow_overwrite=request.allow_overwrite,
        )
    except FileNotFoundError as e:
        detail = str(e) or "artifact not found"
        raise HTTPException(status_code=404, detail=detail) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


__all__ = ["router"]
