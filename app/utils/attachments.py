"""Helpers for extracting attachment information from a task's context.

Each adapter handles images via its CLI's native mechanism (Codex `-i`, Gemini
`@path`, Claude `--add-dir` + Read tool). These helpers locate the actual files
on disk so the adapters don't all have to duplicate the resolution logic.
"""

from __future__ import annotations

from pathlib import Path

from app.protocol.validators import TaskRequest
from app.utils import file_loader


def image_attachment_paths(task: TaskRequest) -> list[Path]:
    """Return the resolved filesystem paths for all image attachments on a task."""
    from app.api.uploads import resolve_attachment_path

    out: list[Path] = []
    attachments = task.context.extra.get("attachments") or []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        file_id = att.get("file_id")
        if not file_id:
            continue
        try:
            path = resolve_attachment_path(file_id)
        except (FileNotFoundError, ValueError):
            continue
        if file_loader.is_image(path):
            out.append(path)
    return out
