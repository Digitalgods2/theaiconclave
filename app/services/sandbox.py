"""Project sandbox preparation for code-review-style conclave tasks.

When a task is submitted with `context.extra.include_sandbox: true` and a
`project_path` set, the orchestrator copies the project to a per-task
temp directory at `data/sandboxes/<task_id>/`. Each agent's CLI is then
given read-only access to that copy via its native mechanism:

- Codex: `-C <sandbox>` + `-s read-only`
- Gemini: `--include-directories <sandbox>` + `--approval-mode plan`
- Claude: `--tools "Read" --add-dir <sandbox>`

The sandbox is a snapshot, fixed at task-creation time. Agents read-only;
no writes, no command execution that could modify state. Cleaned up on
task completion. The user's actual project files are never touched.

Permission gates from the task's `permissions` block are honored during
the copy step — `.env`, `.key`, `credentials`, etc. are skipped unless
the task explicitly grants `can_read_env_files` or `can_read_secrets`.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from app.protocol.validators import Permissions
from app.utils.paths import sandboxes_root

logger = logging.getLogger(__name__)

# Directories never copied. Tunable via config in a future revision.
_SKIP_DIRS: set[str] = {
    ".git", ".hg", ".svn",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    "node_modules", ".npm", ".yarn",
    ".venv", "venv", "env", ".env-venv",
    "dist", "build", "target", "out", ".out",
    ".next", ".nuxt", ".svelte-kit", ".turbo",
    ".idea", ".vscode", ".vs",
    "data",            # don't recursively copy data/uploads, data/sandboxes
    "coverage", "htmlcov",
    "logs", "log",
}

# File-name globs never copied.
_SKIP_FILES_GLOB: tuple[str, ...] = (
    "*.pyc", "*.pyo", "*.pyd", "*.so", "*.dll", "*.exe", "*.dylib", "*.o",
    "*.class", "*.jar", "*.war",
    "*.log", "*.tmp", "*.swp", "*.bak",
    ".DS_Store", "Thumbs.db",
    "*.zip", "*.tar", "*.gz", "*.7z", "*.rar",
    "*.mp4", "*.mov", "*.avi", "*.mkv", "*.webm",
    "*.iso", "*.dmg",
)

# Patterns that require the `can_read_env_files` permission.
_ENV_PATTERNS: tuple[str, ...] = (".env", ".env.*", ".envrc")

# Patterns that require the `can_read_secrets` permission.
_SECRET_PATTERNS: tuple[str, ...] = (
    "*.key", "*.pem", "id_rsa*", "*.p12", "*.pfx",
    "credentials*", "secrets.*", "*.crt",
)

# Hard ceilings.
_MAX_FILE_BYTES = 1 * 1024 * 1024              # 1 MiB per file
_MAX_SANDBOX_BYTES = 200 * 1024 * 1024         # 200 MiB total


def sandbox_path_for(task_id: str) -> Path:
    return sandboxes_root() / task_id


def _matches_any(name: str, patterns) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def _should_skip_dir(dirname: str) -> bool:
    return dirname in _SKIP_DIRS


def _should_skip_file(name: str, permissions: Permissions) -> bool:
    if _matches_any(name, _SKIP_FILES_GLOB):
        return True
    if not permissions.can_read_env_files and _matches_any(name, _ENV_PATTERNS):
        return True
    if not permissions.can_read_secrets and _matches_any(name, _SECRET_PATTERNS):
        return True
    return False


def prepare_sandbox(
    project_path: str | Path,
    task_id: str,
    permissions: Permissions,
) -> Optional[Path]:
    """
    Copy `project_path` to `data/sandboxes/<task_id>/` with skip patterns and
    permission gates applied. Returns the sandbox path, or None if the source
    is missing / not a directory.

    Idempotent — if a sandbox for this task_id already exists, returns it
    without re-copying (so task resumption after `awaiting_user_input` works).
    """
    src = Path(project_path).resolve()
    if not src.exists() or not src.is_dir():
        logger.warning("prepare_sandbox: source does not exist or is not a dir: %s", src)
        return None

    dest = sandbox_path_for(task_id)
    if dest.exists():
        logger.info("prepare_sandbox: existing sandbox reused for task %s", task_id)
        return dest

    dest.mkdir(parents=True, exist_ok=True)

    total_bytes = 0
    skipped = 0
    copied = 0
    cap_reached = False

    for root, dirs, files in os.walk(src):
        # Filter directories in-place so os.walk doesn't descend into skipped ones.
        dirs[:] = [d for d in dirs if not _should_skip_dir(d)]
        if cap_reached:
            break

        root_path = Path(root)
        for fname in files:
            if _should_skip_file(fname, permissions):
                skipped += 1
                continue
            src_file = root_path / fname
            try:
                size = src_file.stat().st_size
            except OSError:
                skipped += 1
                continue
            if size > _MAX_FILE_BYTES:
                skipped += 1
                continue
            if total_bytes + size > _MAX_SANDBOX_BYTES:
                logger.warning(
                    "prepare_sandbox: cap %.1f MiB reached for task %s",
                    _MAX_SANDBOX_BYTES / (1024 * 1024), task_id,
                )
                cap_reached = True
                break
            rel = src_file.relative_to(src)
            dest_file = dest / rel
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src_file, dest_file)
                total_bytes += size
                copied += 1
            except OSError as e:
                logger.warning("prepare_sandbox: skip %s: %s", src_file, e)
                skipped += 1

    logger.info(
        "prepare_sandbox: %s -> %s | %d files, %d skipped, %.1f KiB",
        src, dest, copied, skipped, total_bytes / 1024,
    )
    return dest


def cleanup_sandbox(task_id: str) -> bool:
    """Remove the sandbox for a task. Returns True if removed, False if absent."""
    dest = sandbox_path_for(task_id)
    if not dest.exists():
        return False
    try:
        shutil.rmtree(dest)
        logger.info("cleanup_sandbox: removed %s", dest)
        return True
    except OSError as e:
        logger.warning("cleanup_sandbox: failed for %s: %s", dest, e)
        return False


def build_manifest(sandbox: Path, max_entries: int = 400) -> str:
    """
    Compact file-tree listing for the prompt. Returns a string like:

        - app/main.py (2138 bytes)
        - app/services/orchestrator.py (12872 bytes)
        - ...
    """
    if not sandbox.exists():
        return ""
    entries: list[str] = []
    for path in sorted(sandbox.rglob("*")):
        if path.is_dir() or not path.is_file():
            continue
        rel = path.relative_to(sandbox)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        entries.append(f"- {rel.as_posix()} ({size:,} bytes)")
        if len(entries) >= max_entries:
            entries.append(f"- ... [manifest truncated at {max_entries} entries]")
            break
    return "\n".join(entries)


def sweep_orphan_sandboxes(active_task_ids: set[str]) -> int:
    """
    Remove sandboxes for tasks that are no longer active. Called on service
    startup so a crash mid-task doesn't leave orphans accumulating.
    Returns the number of sandboxes removed.
    """
    root = sandboxes_root()
    if not root.exists():
        return 0
    removed = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if child.name in active_task_ids:
            continue
        try:
            shutil.rmtree(child)
            removed += 1
        except OSError as e:
            logger.warning("sweep_orphan_sandboxes: failed to remove %s: %s", child, e)
    if removed:
        logger.info("sweep_orphan_sandboxes: removed %d orphan(s)", removed)
    return removed
