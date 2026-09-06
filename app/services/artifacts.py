"""Task artifact capture and application helpers.

Artifacts are app-owned draft outputs under user_data_root()/artifacts. They
do not grant agents direct write access to the user's project; applying an
artifact to the project is an explicit API/dashboard action.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import mimetypes
import os
import shutil
import tempfile
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
                "requires_approval": action.requires_approval,
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
                "requires_approval": action.requires_approval,
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
                "requires_approval": action.requires_approval,
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


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve_apply_target(task_id: str, artifact_id_value: str):
    with connect() as conn:
        task_row = conn.execute(
            "SELECT project_path, status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    if task_row is None:
        raise FileNotFoundError("task not found")
    if task_row["status"] != "completed":
        raise ValueError("artifacts may only be applied from a completed task")
    project_path = task_row["project_path"]
    if not project_path:
        raise ValueError("task has no project_path")
    project_root = Path(project_path).resolve()
    if not project_root.is_dir():
        raise ValueError("task project_path no longer exists or is not a directory")
    artifact = get_artifact(task_id, artifact_id_value)
    metadata = artifact.get("metadata") or {}
    relpath = _safe_relpath(metadata.get("target_path") or artifact["filename"], artifact["filename"])
    target = (project_root / relpath).resolve()
    if project_root not in target.parents and target != project_root:
        raise ValueError("artifact target escapes project_path")
    return artifact, metadata, target, relpath


def preview_artifact_apply(task_id: str, artifact_id_value: str) -> dict[str, Any]:
    """Return the exact target state and proposed text diff without writing."""
    artifact, metadata, target, relpath = _resolve_apply_target(task_id, artifact_id_value)
    data = read_artifact_bytes(task_id, artifact_id_value)
    exists = target.is_file()
    current = target.read_bytes() if exists else b""
    current_hash = _sha256(current) if exists else "missing"
    mode = metadata.get("apply_mode")
    proposed = data
    operation = "wrote_file"
    if artifact["kind"] == "edit" and mode == "search_replace":
        if not exists:
            raise ValueError(f"target file does not exist: {relpath}")
        edit = json.loads(data.decode("utf-8"))
        text = current.decode("utf-8")
        occurrences = text.count(edit["search"])
        if occurrences != 1:
            raise ValueError(
                f"search text must occur exactly once in {relpath}; found {occurrences}"
            )
        proposed = text.replace(edit["search"], edit["replace"], 1).encode("utf-8")
        operation = "applied_search_replace"
    elif not (artifact["kind"] == "file" and mode == "write_file"):
        raise ValueError("this artifact kind is review/download only")

    diff = ""
    try:
        before_lines = current.decode("utf-8").splitlines(keepends=True)
        after_lines = proposed.decode("utf-8").splitlines(keepends=True)
        diff = "".join(difflib.unified_diff(
            before_lines, after_lines,
            fromfile=f"a/{relpath}" if exists else "/dev/null",
            tofile=f"b/{relpath}",
        ))[:40_000]
    except UnicodeDecodeError:
        diff = "[binary content; text diff unavailable]"
    return {
        "task_id": task_id,
        "artifact_id": artifact_id_value,
        "operation": operation,
        "target_path": str(target),
        "target_exists": exists,
        "will_overwrite": exists and operation == "wrote_file",
        "expected_target_sha256": current_hash,
        "proposed_sha256": _sha256(proposed),
        "diff": diff,
    }


def _atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except Exception:
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def apply_artifact_to_project(
    task_id: str,
    artifact_id_value: str,
    *,
    confirm: bool = False,
    expected_target_sha256: str | None = None,
    allow_overwrite: bool = False,
) -> dict[str, Any]:
    """Apply a reviewed draft using an optimistic-lock and recoverable backup."""
    if not confirm:
        raise ValueError("explicit confirmation is required")
    preview = preview_artifact_apply(task_id, artifact_id_value)
    actual_hash = preview["expected_target_sha256"]
    if expected_target_sha256 is None:
        raise ValueError("expected_target_sha256 from the apply preview is required")
    if expected_target_sha256 != actual_hash:
        raise ValueError("target changed after preview; refresh and review the new diff")
    if preview["will_overwrite"] and not allow_overwrite:
        raise ValueError("target already exists; allow_overwrite=true is required")

    artifact, metadata, target, _relpath = _resolve_apply_target(task_id, artifact_id_value)

    data = read_artifact_bytes(task_id, artifact_id_value)
    mode = metadata.get("apply_mode")
    backup_path: Path | None = None
    if target.is_file():
        backup_dir = artifacts_root() / task_id / artifact_id_value / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"{target.name}.{now_iso().replace(':', '-')}.bak"
        shutil.copy2(target, backup_path)

    if artifact["kind"] == "file" and mode == "write_file":
        _atomic_write(target, data)
        applied = "wrote_file"
    elif artifact["kind"] == "edit" and mode == "search_replace":
        edit = json.loads(data.decode("utf-8"))
        text = target.read_text(encoding="utf-8")
        search = edit["search"]
        replace = edit["replace"]
        if text.count(search) != 1:
            raise ValueError("target changed after preview; search text is no longer unique")
        _atomic_write(target, text.replace(search, replace, 1).encode("utf-8"))
        applied = "applied_search_replace"
    else:
        raise ValueError("this artifact kind is review/download only")

    now = now_iso()
    metadata["applied_at"] = now
    metadata["applied_to"] = str(target)
    metadata["applied_sha256"] = _sha256(target.read_bytes())
    if backup_path is not None:
        metadata["backup_path"] = _storage_relpath(backup_path)
    with connect() as conn:
        conn.execute(
            "UPDATE task_artifacts SET updated_at = ?, metadata_json = ? WHERE id = ?",
            (now, json.dumps(metadata, sort_keys=True), artifact_id_value),
        )
        conn.execute(
            """INSERT INTO logs
               (id, task_id, level, event_type, message, metadata_json, created_at)
               VALUES (?, ?, 'info', 'artifact_applied', ?, ?, ?)""",
            (
                f"log_artifact_{artifact_id_value}_{now}", task_id,
                f"Applied artifact {artifact_id_value} to {target}",
                json.dumps({
                    "artifact_id": artifact_id_value,
                    "operation": applied,
                    "target_path": str(target),
                    "backup_path": metadata.get("backup_path"),
                    "before_sha256": actual_hash,
                    "after_sha256": metadata["applied_sha256"],
                }, sort_keys=True),
                now,
            ),
        )
    return {
        "task_id": task_id,
        "artifact_id": artifact_id_value,
        "status": "applied",
        "operation": applied,
        "target_path": str(target),
        "applied_at": now,
        "backup_path": metadata.get("backup_path"),
        "sha256": metadata["applied_sha256"],
    }
