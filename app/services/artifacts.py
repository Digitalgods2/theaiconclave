"""Task artifact capture and application helpers.

Artifacts are app-owned draft outputs under user_data_root()/artifacts. They
do not grant agents direct write access to the user's project; applying an
artifact to the project is an explicit API/dashboard action.
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

from app.database import connect, now_iso
from app.protocol.validators import FinalResult, RecommendedAction, TaskRequest
from app.utils.ids import artifact_id
from app.utils.paths import artifacts_root, user_data_root

_TEXT_PREVIEW_LIMIT = 40_000


def safe_filename(raw: str) -> str:
    base = Path(str(raw or "artifact")).name
    safe = "".join(c if c.isalnum() or c in "._-+ " else "_" for c in base)
    return safe or "artifact"


def _safe_relpath(raw: str | None, fallback: str) -> str:
    candidate = str(raw or fallback).replace("\\", "/").strip()
    if not candidate:
        candidate = fallback
    while candidate.startswith("/"):
        candidate = candidate[1:]
    parts = []
    for part in candidate.split("/"):
        if not part or part == "." or part == "..":
            continue
        parts.append(safe_filename(part))
    return "/".join(parts) if parts else safe_filename(fallback)


def _storage_relpath(path: Path) -> str:
    try:
        return path.resolve().relative_to(user_data_root().resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _storage_abspath(storage_path: str) -> Path:
    path = Path(storage_path)
    if path.is_absolute():
        return path
    return (user_data_root() / storage_path).resolve()


def _insert_artifact(
    *,
    task_id: str,
    kind: str,
    title: str,
    filename: str,
    mime_type: str,
    data: bytes,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    aid = artifact_id()
    dest_dir = artifacts_root() / task_id / aid
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = safe_filename(filename)
    dest = dest_dir / safe_name
    dest.write_bytes(data)
    now = now_iso()
    storage_path = _storage_relpath(dest)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO task_artifacts
            (id, task_id, created_at, updated_at, kind, title, filename,
             mime_type, size_bytes, storage_path, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                aid, task_id, now, now, kind, title, safe_name, mime_type,
                len(data), storage_path, json.dumps(metadata, sort_keys=True),
            ),
        )
    return {
        "id": aid,
        "task_id": task_id,
        "created_at": now,
        "updated_at": now,
        "kind": kind,
        "title": title,
        "filename": safe_name,
        "mime_type": mime_type,
        "size_bytes": len(data),
        "storage_path": storage_path,
        "metadata": metadata,
    }


def _content_from_payload(payload: dict[str, Any]) -> str | None:
    for key in ("content", "text", "body", "code"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return None


def _edit_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    path = payload.get("path") or payload.get("file") or payload.get("target")
    search = payload.get("search") or payload.get("old") or payload.get("before")
    replace = payload.get("replace") or payload.get("replacement") or payload.get("new") or payload.get("after")
    if isinstance(path, str) and isinstance(search, str) and isinstance(replace, str):
        return {
            "path": _safe_relpath(path, "edit.txt"),
            "search": search,
            "replace": replace,
        }
    return None


def _artifact_from_action(
    *,
    task_id: str,
    agent_name: str,
    action: RecommendedAction,
    index: int,
) -> dict[str, Any] | None:
    kind = (action.kind or "").lower()
    payload = action.payload if isinstance(action.payload, dict) else {}
    target = payload.get("path") or payload.get("file") or payload.get("target")

    if kind in {"create_file", "write_file"}:
        content = _content_from_payload(payload)
        if content is None:
            return None
        relpath = _safe_relpath(str(target or f"artifact_{index}.txt"), f"artifact_{index}.txt")
        filename = safe_filename(Path(relpath).name)
        mime_type = mimetypes.guess_type(filename)[0] or "text/plain"
        return _insert_artifact(
            task_id=task_id,
            kind="file",
            title=action.description or f"Draft file {relpath}",
            filename=filename,
            mime_type=mime_type,
            data=content.encode("utf-8"),
            metadata={
                "agent": agent_name,
                "source_action_kind": action.kind,
                "description": action.description,
                "target_path": relpath,
                "apply_mode": "write_file",
            },
        )

    if kind == "edit_file":
        edit = _edit_payload(payload)
        if edit is None:
            return None
        relpath = edit["path"]
        data = json.dumps(edit, indent=2, sort_keys=True).encode("utf-8")
        return _insert_artifact(
            task_id=task_id,
            kind="edit",
            title=action.description or f"Draft edit {relpath}",
            filename=safe_filename(Path(relpath).name + ".edit.json"),
            mime_type="application/json",
            data=data,
            metadata={
                "agent": agent_name,
                "source_action_kind": action.kind,
                "description": action.description,
                "target_path": relpath,
                "apply_mode": "search_replace",
            },
        )

    if kind == "apply_patch" or "patch" in kind:
        patch = payload.get("patch") or payload.get("diff") or _content_from_payload(payload)
        if not isinstance(patch, str):
            return None
        filename = safe_filename(str(target or f"patch_{index}.patch"))
        if not filename.endswith(".patch"):
            filename += ".patch"
        return _insert_artifact(
            task_id=task_id,
            kind="patch",
            title=action.description or "Draft patch",
            filename=filename,
            mime_type="text/x-patch",
            data=patch.encode("utf-8"),
            metadata={
                "agent": agent_name,
                "source_action_kind": action.kind,
                "description": action.description,
                "target_path": _safe_relpath(str(target or filename), filename),
                "apply_mode": "manual_patch",
            },
        )

    return None


def capture_from_final_result(task: TaskRequest, task_id: str, result: FinalResult) -> list[dict[str, Any]]:
    agent_name = result.primary_agent or task.primary_agent or "final"
    artifacts: list[dict[str, Any]] = []
    for index, action in enumerate(result.recommended_actions, start=1):
        artifact = _artifact_from_action(
            task_id=task_id,
            agent_name=agent_name,
            action=action,
            index=index,
        )
        if artifact:
            artifacts.append(artifact)
    return artifacts


def list_artifacts(task_id: str, *, include_content: bool = False) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM task_artifacts
               WHERE task_id = ? ORDER BY created_at, id""",
            (task_id,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = _row_to_artifact(row)
        if include_content:
            path = _storage_abspath(item["storage_path"])
            try:
                if item["mime_type"].startswith("text/") or item["mime_type"] == "application/json":
                    text = path.read_text(encoding="utf-8")
                    item["content"] = text[:_TEXT_PREVIEW_LIMIT]
                    item["content_truncated"] = len(text) > _TEXT_PREVIEW_LIMIT
            except OSError:
                item["content_error"] = "artifact file missing"
        out.append(item)
    return out


def get_artifact(task_id: str, artifact_id_value: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM task_artifacts WHERE task_id = ? AND id = ?",
            (task_id, artifact_id_value),
        ).fetchone()
    if row is None:
        raise FileNotFoundError("artifact not found")
    return _row_to_artifact(row)


def read_artifact_bytes(task_id: str, artifact_id_value: str) -> bytes:
    artifact = get_artifact(task_id, artifact_id_value)
    path = _storage_abspath(artifact["storage_path"])
    return path.read_bytes()


def _row_to_artifact(row) -> dict[str, Any]:
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (ValueError, TypeError):
        metadata = {}
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "kind": row["kind"],
        "title": row["title"],
        "filename": row["filename"],
        "mime_type": row["mime_type"],
        "size_bytes": row["size_bytes"],
        "storage_path": row["storage_path"],
        "metadata": metadata,
    }


def apply_artifact_to_project(task_id: str, artifact_id_value: str) -> dict[str, Any]:
    with connect() as conn:
        task_row = conn.execute(
            "SELECT project_path FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    if task_row is None:
        raise FileNotFoundError("task not found")
    project_path = task_row["project_path"]
    if not project_path:
        raise ValueError("task has no project_path")
    project_root = Path(project_path).resolve()
    artifact = get_artifact(task_id, artifact_id_value)
    metadata = artifact.get("metadata") or {}
    relpath = _safe_relpath(metadata.get("target_path") or artifact["filename"], artifact["filename"])
    target = (project_root / relpath).resolve()
    if project_root not in target.parents and target != project_root:
        raise ValueError("artifact target escapes project_path")

    data = read_artifact_bytes(task_id, artifact_id_value)
    mode = metadata.get("apply_mode")
    if artifact["kind"] == "file" and mode == "write_file":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        applied = "wrote_file"
    elif artifact["kind"] == "edit" and mode == "search_replace":
        edit = json.loads(data.decode("utf-8"))
        if not target.exists():
            raise ValueError(f"target file does not exist: {relpath}")
        text = target.read_text(encoding="utf-8")
        search = edit["search"]
        replace = edit["replace"]
        if search not in text:
            raise ValueError(f"search text not found in {relpath}")
        target.write_text(text.replace(search, replace, 1), encoding="utf-8")
        applied = "applied_search_replace"
    else:
        raise ValueError("this artifact kind is review/download only")

    now = now_iso()
    metadata["applied_at"] = now
    metadata["applied_to"] = str(target)
    with connect() as conn:
        conn.execute(
            "UPDATE task_artifacts SET updated_at = ?, metadata_json = ? WHERE id = ?",
            (now, json.dumps(metadata, sort_keys=True), artifact_id_value),
        )
    return {
        "task_id": task_id,
        "artifact_id": artifact_id_value,
        "status": "applied",
        "operation": applied,
        "target_path": str(target),
        "applied_at": now,
    }
