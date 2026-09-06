"""Persistent decision-project API."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.database import connect, now_iso
from app.utils.ids import project_id


router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectFields(BaseModel):
    @field_validator("name", "description", "instructions", "default_evidence_urls", "archived",
                     mode="before", check_fields=False)
    @classmethod
    def validate_fields(cls, value, info):
        if value is None:
            raise ValueError(f"{info.field_name} cannot be null; omit it to leave it unchanged")
        if info.field_name == "name" and isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("name must contain non-whitespace characters")
        return value


class ProjectCreate(ProjectFields):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    instructions: str = Field(default="", max_length=20_000)
    default_evidence_urls: list[str] = Field(default_factory=list, max_length=20)


class ProjectUpdate(ProjectFields):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=4000)
    instructions: Optional[str] = Field(default=None, max_length=20_000)
    default_evidence_urls: Optional[list[str]] = Field(default=None, max_length=20)
    archived: Optional[bool] = None


def _project_dict(row) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"],
        "description": row["description"], "instructions": row["instructions"],
        "default_evidence_urls": json.loads(row["default_evidence_urls_json"]),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "archived_at": row["archived_at"],
        "task_count": row["task_count"] if "task_count" in row.keys() else None,
        "evidence_count": row["evidence_count"] if "evidence_count" in row.keys() else None,
    }


@router.post("")
async def create_project(body: ProjectCreate) -> dict[str, Any]:
    pid = project_id()
    now = now_iso()
    try:
        with connect() as conn:
            conn.execute(
                """INSERT INTO decision_projects
                   (id, name, description, instructions, default_evidence_urls_json,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (pid, body.name.strip(), body.description, body.instructions,
                 json.dumps(body.default_evidence_urls), now, now),
            )
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=409, detail="a project with that name already exists") from e
    return {"id": pid, "name": body.name.strip(), "created_at": now}


@router.get("")
async def list_projects(include_archived: bool = False) -> dict[str, Any]:
    where = "" if include_archived else "WHERE p.archived_at IS NULL"
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT p.*,
                       (SELECT COUNT(*) FROM tasks t WHERE t.decision_project_id = p.id) task_count,
                       (SELECT COUNT(*) FROM evidence_snapshots e WHERE e.project_id = p.id) evidence_count
                FROM decision_projects p {where}
                ORDER BY p.updated_at DESC"""
        ).fetchall()
    return {"projects": [_project_dict(row) for row in rows]}


@router.get("/{project_id_value}")
async def get_project(project_id_value: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            """SELECT p.*,
                      (SELECT COUNT(*) FROM tasks t WHERE t.decision_project_id = p.id) task_count,
                      (SELECT COUNT(*) FROM evidence_snapshots e WHERE e.project_id = p.id) evidence_count
               FROM decision_projects p WHERE p.id = ?""",
            (project_id_value,),
        ).fetchone()
        tasks = conn.execute(
            """SELECT id, status, mode, user_request, created_at, updated_at
               FROM tasks WHERE decision_project_id = ? ORDER BY created_at DESC LIMIT 100""",
            (project_id_value,),
        ).fetchall()
    if row is None:
        raise HTTPException(status_code=404, detail="project not found")
    return {"project": _project_dict(row), "tasks": [dict(task) for task in tasks]}


@router.patch("/{project_id_value}")
async def update_project(project_id_value: str, body: ProjectUpdate) -> dict[str, Any]:
    values = body.model_dump(exclude_unset=True)
    if not values:
        return await get_project(project_id_value)
    assignments: list[str] = []
    params: list[Any] = []
    for field in ("name", "description", "instructions"):
        if field in values:
            assignments.append(f"{field} = ?")
            params.append(values[field].strip() if field == "name" else values[field])
    if "default_evidence_urls" in values:
        assignments.append("default_evidence_urls_json = ?")
        params.append(json.dumps(values["default_evidence_urls"]))
    if "archived" in values:
        assignments.append("archived_at = ?")
        params.append(now_iso() if values["archived"] else None)
    assignments.append("updated_at = ?")
    params.append(now_iso())
    params.append(project_id_value)
    try:
        with connect() as conn:
            cursor = conn.execute(
                f"UPDATE decision_projects SET {', '.join(assignments)} WHERE id = ?", params
            )
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=409, detail="a project with that name already exists") from e
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="project not found")
    return await get_project(project_id_value)


__all__ = ["router"]
