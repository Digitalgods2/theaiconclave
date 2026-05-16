"""Inline a project sandbox into a prompt for adapters that can't browse files.

The CLI adapters (codex / claude-code / gemini) get native read access to the
per-task sandbox directory. The HTTP adapters (OpenRouter) don't — they only
receive the prompt text. So for those, when a task has a sandbox, we paste
a read-only file tree + the contents of as many files as fit into a character
budget, so the model can actually examine the code instead of asking the user
to describe it.

The sandbox directory is already filtered by `app/services/sandbox.py` (skips
.git, node_modules, __pycache__, permission-gated secrets, etc.), so this module
only needs to: walk it, skip binaries and oversized files, prioritise the files
most likely to matter (top-level / shallow source + config first), and stop when
the budget runs out — listing what was omitted so the model knows it's partial.
"""

from __future__ import annotations

import os
from pathlib import Path


# Files we never inline regardless of budget (binary / generated / huge-by-nature).
_SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".pdf", ".zip", ".gz", ".tar", ".7z", ".rar", ".jar", ".war",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".class",
    ".pyc", ".pyo", ".pyd", ".wasm",
    ".mp3", ".mp4", ".wav", ".mov", ".avi", ".mkv", ".flac", ".ogg",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".db", ".sqlite", ".sqlite3", ".lock",
    ".min.js", ".min.css", ".map",
}
# Per-file hard cap — a single giant file shouldn't eat the whole budget.
_PER_FILE_CAP = 200_000
# Source / config extensions that get priority when the budget is tight.
_PRIORITY_EXT = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".go", ".rs", ".rb", ".java",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".php", ".swift", ".kt", ".scala",
    ".sh", ".ps1", ".sql",
    ".md", ".rst", ".txt",
    ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".env.example", ".conf",
    ".html", ".css", ".vue", ".svelte",
}
_PRIORITY_NAMES = {
    "readme", "readme.md", "main.py", "app.py", "__main__.py", "index.js",
    "index.ts", "main.go", "main.rs", "cargo.toml", "go.mod", "package.json",
    "pyproject.toml", "requirements.txt", "config.yaml", "config.example.yaml",
    "dockerfile", "makefile",
}


def _is_binary(path: Path) -> bool:
    """Cheap heuristic: a NUL byte in the first 8 KiB means binary."""
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(8192)
    except OSError:
        return True


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _priority_rank(rel: str, name: str, ext: str) -> tuple:
    """Lower sorts first: known entry-point names, then shallow paths, then by name.
    Non-priority extensions sort after everything (rank 1 vs 0)."""
    is_pri_ext = (ext in _PRIORITY_EXT) or (name in _PRIORITY_NAMES)
    is_pri_name = name in _PRIORITY_NAMES
    depth = rel.count("/")
    return (0 if is_pri_ext else 1, 0 if is_pri_name else 1, depth, rel.lower())


def _walk(sandbox: Path) -> list[tuple[str, Path, int]]:
    """Return [(relative_posix_path, abs_path, size_bytes), ...] for inlineable files."""
    out: list[tuple[str, Path, int]] = []
    for root, dirs, files in os.walk(sandbox):
        # os.walk on the sandbox dir; it's already filtered, but be defensive.
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in files:
            p = Path(root) / fn
            ext = "".join(Path(fn).suffixes[-1:]).lower() or Path(fn).suffix.lower()
            # handle multi-suffix like .min.js
            lower = fn.lower()
            if any(lower.endswith(s) for s in _SKIP_EXT):
                continue
            if ext in _SKIP_EXT:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size == 0 or size > _PER_FILE_CAP:
                continue
            if _is_binary(p):
                continue
            try:
                rel = p.relative_to(sandbox).as_posix()
            except ValueError:
                rel = p.name
            out.append((rel, p, size))
    return out


def build_sandbox_section(sandbox_path: str, char_budget: int) -> str:
    """Build a 'PROJECT FILES' prompt section for the sandbox at `sandbox_path`,
    fitting within roughly `char_budget` characters.

    Always includes a file tree (cheap). Includes file contents in priority
    order until the budget is spent, then notes how many were omitted. Returns
    "" if the path doesn't exist or has nothing inlineable.
    """
    sandbox = Path(sandbox_path)
    if not sandbox.exists() or not sandbox.is_dir():
        return ""
    files = _walk(sandbox)
    if not files:
        return ""

    files.sort(key=lambda t: _priority_rank(t[0], Path(t[0]).name.lower(),
                                             Path(t[0]).suffix.lower()))

    header = (
        "## PROJECT FILES (read-only snapshot)\n\n"
        "You do not have a file-browsing tool. The project's files are inlined below "
        "for you to read directly — treat this as a read-only snapshot (you cannot "
        "execute or modify anything). Cite specific files and lines in your reasoning.\n\n"
    )

    # File tree (always included).
    tree_lines = ["### File tree", "```"]
    for rel, _p, size in sorted(files, key=lambda t: t[0]):
        tree_lines.append(f"{rel}  ({_human_size(size)})")
    tree_lines.append("```\n")
    tree = "\n".join(tree_lines)

    # Budget left for file contents after the header + tree.
    overhead = len(header) + len(tree) + 200
    remaining = max(0, char_budget - overhead)

    body_parts: list[str] = ["### File contents\n"]
    used = 0
    included = 0
    omitted = 0
    for rel, p, size in files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            omitted += 1
            continue
        block = f"\n--- {rel} ---\n{text}\n"
        if used + len(block) > remaining:
            omitted += 1
            continue
        body_parts.append(block)
        used += len(block)
        included += 1

    if included == 0:
        # Budget too small for any contents — tree only.
        return header + tree + (
            f"\n_(Context budget too small to inline file contents — "
            f"{len(files)} file(s) listed in the tree above; ask for specific ones if needed.)_\n"
        )

    footer = ""
    if omitted:
        footer = (
            f"\n_({omitted} more file(s) omitted to stay within the context budget — "
            f"see the full list in the file tree above. Ask for any specific file by path "
            f"if you need it.)_\n"
        )
    return header + tree + "".join(body_parts) + footer


__all__ = ["build_sandbox_section"]
