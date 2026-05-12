"""Git-aware helpers.

Currently a single endpoint that runs `git diff` against the user's project_path
and returns the result. Used by the dashboard's "Attach git diff" button so the
user can include their current uncommitted changes as context in a conclave task
without copy-pasting.

Read-only — this never modifies the repo. Subprocess sandbox: working dir is
forced to the supplied path; no shell, no PATH-resolved env injection.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException

router = APIRouter(prefix="/api/git", tags=["git"])

logger = logging.getLogger(__name__)


@router.post("/diff")
async def git_diff(body: dict = Body(...)) -> dict[str, Any]:
    """
    Run `git diff` (and `git diff --cached` for staged changes) against the
    supplied project_path. Returns the combined patch text plus a small stat
    summary.

    Body: {"project_path": "/path/to/repo", "include_staged": true (default)}
    """
    project_path = body.get("project_path")
    include_staged = body.get("include_staged", True)

    if not project_path or not isinstance(project_path, str):
        raise HTTPException(status_code=400, detail="project_path (string) required")

    path = Path(project_path).resolve()
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=400, detail=f"project_path does not exist or is not a directory: {path}")
    if not (path / ".git").exists():
        raise HTTPException(status_code=400, detail=f"not a git repository: {path}")

    git_path = shutil.which("git")
    if git_path is None:
        raise HTTPException(status_code=500, detail="git executable not found on PATH")

    async def _run(*args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            git_path, "-C", str(path), *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise HTTPException(status_code=504, detail="git command timed out")
        return (
            proc.returncode or 0,
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
        )

    diffs: list[str] = []
    stats_lines: list[str] = []

    # Unstaged changes
    rc, out, err = await _run("diff")
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"git diff failed: {err.strip()[-500:]}")
    if out:
        diffs.append("=== Unstaged changes ===\n" + out)

    # Stat summary for unstaged
    rc, stat_out, _ = await _run("diff", "--stat")
    if stat_out.strip():
        stats_lines.append("Unstaged:\n" + stat_out.rstrip())

    # Staged changes (optional)
    if include_staged:
        rc, out, err = await _run("diff", "--cached")
        if rc != 0:
            raise HTTPException(status_code=500, detail=f"git diff --cached failed: {err.strip()[-500:]}")
        if out:
            diffs.append("=== Staged changes ===\n" + out)
        rc, stat_out, _ = await _run("diff", "--cached", "--stat")
        if stat_out.strip():
            stats_lines.append("Staged:\n" + stat_out.rstrip())

    # Branch and HEAD for context
    _, branch_out, _ = await _run("rev-parse", "--abbrev-ref", "HEAD")
    _, sha_out, _ = await _run("rev-parse", "--short", "HEAD")

    diff_text = "\n\n".join(diffs) if diffs else "(no changes)"
    stat_summary = "\n\n".join(stats_lines) if stats_lines else "(clean working tree)"

    return {
        "project_path": str(path),
        "branch": branch_out.strip() or "(detached)",
        "head": sha_out.strip(),
        "stat_summary": stat_summary,
        "diff": diff_text,
        "diff_bytes": len(diff_text.encode("utf-8")),
    }
