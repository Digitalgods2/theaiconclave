"""File upload endpoint.

Allows the dashboard to attach files (txt, md, pdf, code, images) to tasks.
Uploads are stored at `<user_data_root>/uploads/<file_id>/<original_filename>`
(see DR0016) so the filename is preserved and a single endpoint can serve
metadata.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.utils.ids import _ulid
from app.utils.paths import uploads_root

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

_MAX_BYTES = 20 * 1024 * 1024   # 20 MiB ceiling per file


def _file_id() -> str:
    return f"fil_{_ulid()}"


def _safe_filename(raw: str) -> str:
    """Strip path components and oddities. Keep something the user can recognize."""
    base = Path(raw).name
    # Allow only conservative characters
    safe = "".join(c if c.isalnum() or c in "._-+ " else "_" for c in base)
    return safe or "unnamed"


@router.post("")
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    fid = _file_id()
    filename = _safe_filename(file.filename or "unnamed")
    dest_dir = uploads_root() / fid
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename

    size = 0
    with dest.open("wb") as out:
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_BYTES:
                out.close()
                dest.unlink(missing_ok=True)
                dest_dir.rmdir()
                raise HTTPException(
                    status_code=413,
                    detail=f"file exceeds {_MAX_BYTES // (1024 * 1024)} MiB ceiling",
                )
            out.write(chunk)

    return {
        "file_id": fid,
        "filename": filename,
        "mime_type": file.content_type or "application/octet-stream",
        "size": size,
        "path": str(dest).replace("\\", "/"),
    }


@router.get("/{file_id}/info")
async def info(file_id: str) -> dict[str, Any]:
    safe_id = _safe_filename(file_id)
    if safe_id != file_id:
        raise HTTPException(status_code=400, detail="invalid file_id")
    dest_dir = uploads_root() / file_id
    if not dest_dir.exists():
        raise HTTPException(status_code=404, detail="file not found")
    files = list(dest_dir.iterdir())
    if not files:
        raise HTTPException(status_code=404, detail="file not found")
    f = files[0]
    return {
        "file_id": file_id,
        "filename": f.name,
        "size": f.stat().st_size,
        "path": str(f).replace("\\", "/"),
    }


def resolve_attachment_path(file_id: str) -> Path:
    """Return the filesystem path for an uploaded file_id. Raises FileNotFoundError if missing."""
    if not file_id or "/" in file_id or "\\" in file_id or ".." in file_id:
        raise ValueError(f"invalid file_id: {file_id!r}")
    dest_dir = uploads_root() / file_id
    if not dest_dir.exists():
        raise FileNotFoundError(f"upload directory missing: {dest_dir}")
    files = list(dest_dir.iterdir())
    if not files:
        raise FileNotFoundError(f"no file in upload directory: {dest_dir}")
    return files[0]
