r"""Platform-aware path resolvers for the AI Conclave Switchboard runtime state.

All writable runtime state (SQLite DB, sandboxes, uploads, exports, logs,
pidlock, and — when not running from a repo — the user's config.yaml)
routes through `user_data_root()`. See DR0016 for the full rationale.

Precedence for the root:

1. `SWITCHBOARD_DATA_DIR` environment variable (test/CI/packager override).
2. **Dev-mode fallback**: if the cwd or any ancestor contains both
   `pyproject.toml` and `config.example.yaml`, the root is `<that-dir>/data/`.
   Preserves the in-repo developer experience exactly.
3. Platform-conventional user-data directory:
     - Windows: `%LOCALAPPDATA%\AI Switchboard`
     - macOS:   `~/Library/Application Support/AI Switchboard`
     - Linux:   `$XDG_DATA_HOME/ai-switchboard` (or `~/.local/share/ai-switchboard`)

Result is cached after first call. All derived helpers create their
directory lazily on first use; nothing executes side effects at import.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

_APP_DIRNAME_WIN_MAC = "AI Switchboard"
_APP_DIRNAME_LINUX = "ai-switchboard"

_cached_root: Optional[Path] = None
_cached_is_dev_mode: Optional[bool] = None


def _find_dev_anchor(start: Optional[Path] = None) -> Optional[Path]:
    """Walk upward from `start` (default: cwd) looking for a directory that
    contains BOTH `pyproject.toml` and `config.example.yaml`. Returns that
    directory if found, else None.

    Both files have to be present — `pyproject.toml` alone matches any Python
    project, but `config.example.yaml` is AI Conclave Switchboard-specific, so the pair
    is a tight signal that we're running from an AI Conclave Switchboard checkout.
    """
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "config.example.yaml").is_file():
            return candidate
    return None


def _platform_user_data_root() -> Path:
    """Return the platform-conventional user-data root for this app."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / _APP_DIRNAME_WIN_MAC
        return Path.home() / "AppData" / "Local" / _APP_DIRNAME_WIN_MAC

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _APP_DIRNAME_WIN_MAC

    # Linux / other Unix
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / _APP_DIRNAME_LINUX
    return Path.home() / ".local" / "share" / _APP_DIRNAME_LINUX


def user_data_root() -> Path:
    """Resolve and return the writable-state root. Cached after first call.

    Creates the directory if it does not exist.
    """
    global _cached_root, _cached_is_dev_mode

    if _cached_root is not None:
        return _cached_root

    override = os.environ.get("SWITCHBOARD_DATA_DIR")
    if override:
        root = Path(override).expanduser().resolve()
        _cached_is_dev_mode = False
    else:
        dev_anchor = _find_dev_anchor()
        if dev_anchor is not None:
            root = dev_anchor / "data"
            _cached_is_dev_mode = True
        else:
            root = _platform_user_data_root()
            _cached_is_dev_mode = False

    root.mkdir(parents=True, exist_ok=True)
    _cached_root = root
    return root


def is_dev_mode() -> bool:
    """True iff user_data_root() resolved to a repo-relative ./data/ path.

    Useful for skipping the first-run migration when running from source.
    Forces resolution if not yet cached.
    """
    if _cached_is_dev_mode is None:
        user_data_root()  # populates the cache
    return bool(_cached_is_dev_mode)


def reset_cache() -> None:
    """Clear the cached root. Test-only — production code should never call
    this. Tests that flip `SWITCHBOARD_DATA_DIR` between cases need it.
    """
    global _cached_root, _cached_is_dev_mode
    _cached_root = None
    _cached_is_dev_mode = None


def _subdir(name: str) -> Path:
    """Resolve `<user_data_root>/<name>`, creating it lazily."""
    p = user_data_root() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def user_config_path() -> Path:
    """Path to the user's `config.yaml` inside `user_data_root()`.

    The file may not exist — callers decide whether to read or seed it.
    The parent directory is created.
    """
    user_data_root()  # ensure root exists
    return user_data_root() / "config.yaml"


def default_db_path() -> Path:
    """Default SQLite database path: `<user_data_root>/switchboard.db`.

    The file may not exist — `init_database()` creates it. The parent
    directory is created.
    """
    user_data_root()
    return user_data_root() / "switchboard.db"


def sandboxes_root() -> Path:
    """Per-task sandbox copies live under here, one subdirectory per task."""
    return _subdir("sandboxes")


def uploads_root() -> Path:
    """Per-upload directories live under here, one subdirectory per file_id."""
    return _subdir("uploads")


def exports_root() -> Path:
    """Markdown decision-record exports + per-task downloads land here."""
    return _subdir("exports")


def artifacts_root() -> Path:
    """Per-task draft artifacts produced from agent recommendations live here."""
    return _subdir("artifacts")


def logs_root() -> Path:
    """Reserved for future durable file logging. Created so a later record
    can adopt it without further plumbing. Today's logging is stderr-only.
    """
    return _subdir("logs")
