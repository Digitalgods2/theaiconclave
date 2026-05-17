"""Pure tool implementations for the OpenRouter tool-loop (DR0015).

These functions are called by the OpenRouter adapter when a model invokes
`read_file` / `list_dir` / `glob` during a tool-loop turn. They:

  - Resolve the requested path against the per-task sandbox root
  - Refuse path-traversal attempts (`..`, absolute paths outside the sandbox,
    symlinks pointing out)
  - Apply the same ignore rules the sandbox already uses (`.git`,
    `node_modules`, `__pycache__`, build outputs, etc.) so a `glob("**/*")`
    doesn't pull garbage
  - Cap their output (per-call path count, per-file size) so a single bad
    call can't blow the per-turn `max_tool_bytes` budget single-handedly

Return shape: `{"ok": True, "content": "..."}` on success, `{"ok": False,
"error": "..."}` on rejected input. Errors are deliberately string-only —
they're rendered straight back to the model as the next-turn tool-result
content so the model can self-correct ("file not found", "outside
sandbox", etc.).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


# Match the sandbox's own ignore-set + a few extras to avoid surfacing
# non-source files agents probably don't want.
_IGNORE_DIRS = {
    ".git", ".hg", ".svn",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    "node_modules", "bower_components",
    ".venv", "venv", "env",
    "dist", "build", "out", "target", ".next", ".nuxt", ".svelte-kit",
    ".idea", ".vscode",
    "coverage", "htmlcov",
    "data",
}
_IGNORE_FILE_EXT = {
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib", ".class", ".o", ".a",
    ".exe", ".bin",
    ".zip", ".tar", ".gz", ".7z", ".rar", ".jar",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".mp3", ".mp4", ".wav", ".mov", ".avi", ".mkv", ".flac", ".ogg",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".db", ".sqlite", ".sqlite3",
}

# Per-file cap on read_file output. Keeps a single big file from devouring
# the per-turn byte budget; the agent can ask for a different file if it
# needs more context.
_READ_FILE_CAP = 80_000

# Per-call cap on glob results. DR0015 sets 200; honored here.
DEFAULT_MAX_GLOB_PATHS = 200


def _safe_resolve(sandbox_root: Path, requested: str) -> Optional[Path]:
    """Resolve `requested` against `sandbox_root` and return the absolute path
    if and only if the result is inside the sandbox. Else None.

    Handles: leading slash on relative paths, `..` traversal, and Windows
    drive letters. Symlinks are resolved fully so a symlink-out is also caught.
    """
    if requested is None:
        return None
    s = str(requested).strip()
    # Treat "" and "." as the sandbox root.
    if s in ("", ".", "./"):
        return sandbox_root
    # Strip a leading slash so `/app/main.py` is interpreted relative to the
    # sandbox root, the way a model would naively write it. (We never honor
    # absolute paths outside the sandbox.)
    if s.startswith(("/", "\\")):
        s = s.lstrip("/\\")
    candidate = (sandbox_root / s).resolve()
    try:
        candidate.relative_to(sandbox_root.resolve())
    except ValueError:
        return None
    return candidate


def _is_ignored_path(rel_posix: str) -> bool:
    """Cheap check against the standard ignore set, applied to any path
    segment (so `foo/.git/bar` is ignored even though `.git` is mid-path)."""
    parts = rel_posix.split("/")
    if any(part in _IGNORE_DIRS for part in parts):
        return True
    name = parts[-1].lower() if parts else ""
    return any(name.endswith(ext) for ext in _IGNORE_FILE_EXT)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_read_file(sandbox_root: Path, path: str) -> dict:
    """Read a single file from the sandbox. Capped at `_READ_FILE_CAP` chars;
    truncation is signaled in the output."""
    resolved = _safe_resolve(sandbox_root, path)
    if resolved is None:
        return {"ok": False, "error": f"path outside sandbox: {path!r}"}
    if not resolved.exists():
        return {"ok": False, "error": f"file not found: {path!r}"}
    if not resolved.is_file():
        return {"ok": False, "error": f"not a file: {path!r}"}
    rel = resolved.relative_to(sandbox_root.resolve()).as_posix()
    if _is_ignored_path(rel):
        return {"ok": False, "error": f"path in ignore set ({rel!r}); pick a source file"}
    try:
        raw = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"ok": False, "error": f"could not read {path!r}: {e}"}
    if len(raw) > _READ_FILE_CAP:
        return {
            "ok": True,
            "content": raw[:_READ_FILE_CAP],
            "truncated": True,
            "original_chars": len(raw),
        }
    return {"ok": True, "content": raw, "truncated": False}


def tool_list_dir(sandbox_root: Path, path: str) -> dict:
    """List the immediate contents of a sandbox directory. Returns a list of
    `{name, kind}` where kind is "file" or "dir". Ignored items are filtered."""
    resolved = _safe_resolve(sandbox_root, path)
    if resolved is None:
        return {"ok": False, "error": f"path outside sandbox: {path!r}"}
    if not resolved.exists():
        return {"ok": False, "error": f"directory not found: {path!r}"}
    if not resolved.is_dir():
        return {"ok": False, "error": f"not a directory: {path!r}"}
    entries: list[dict] = []
    try:
        for child in sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            rel = child.relative_to(sandbox_root.resolve()).as_posix()
            if _is_ignored_path(rel):
                continue
            entries.append({"name": child.name, "kind": "dir" if child.is_dir() else "file"})
    except OSError as e:
        return {"ok": False, "error": f"could not list {path!r}: {e}"}
    return {"ok": True, "entries": entries}


def tool_glob(sandbox_root: Path, pattern: str,
              max_paths: int = DEFAULT_MAX_GLOB_PATHS) -> dict:
    """Return file paths matching `pattern`, relative to sandbox root.

    Supports standard glob syntax including `**` (recursive). Ignored paths
    are filtered. Capped at `max_paths` to avoid runaway audit-trail bloat.
    """
    if not pattern or not isinstance(pattern, str):
        return {"ok": False, "error": "glob pattern must be a non-empty string"}
    # Refuse absolute-looking patterns that try to escape; treat as relative.
    pat = pattern.lstrip("/\\")
    if ".." in pat.split("/"):
        return {"ok": False, "error": "glob pattern may not contain '..'"}
    try:
        matches = list(sandbox_root.glob(pat))
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"glob error: {e}"}
    out: list[str] = []
    truncated = False
    for p in sorted(matches, key=lambda x: x.as_posix()):
        if not p.is_file():
            continue
        try:
            rel = p.resolve().relative_to(sandbox_root.resolve()).as_posix()
        except ValueError:
            continue
        if _is_ignored_path(rel):
            continue
        out.append(rel)
        if len(out) >= max_paths:
            truncated = True
            break
    return {"ok": True, "paths": out, "truncated": truncated, "total_returned": len(out)}
