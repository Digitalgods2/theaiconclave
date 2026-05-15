"""Help-documentation metadata endpoint.

The help document (`app/dashboard/help.html`) carries its own version metadata
in HTML comments at the top of the file:

    <!-- HELP_DOC_VERSION: 1.0.0 -->
    <!-- HELP_COVERED_APP_VERSION: 0.1.0 -->
    <!-- HELP_LAST_UPDATED: 2026-05-14 -->

On service startup (`sync_help_metadata_from_file`), those comments are read
and upserted into the `settings` table. The HTML is the source of truth; the
DB is the runtime query target. This keeps the version visually next to the
prose so a maintainer cannot bump the doc without bumping the indicator.

At request time, `/api/help/metadata` reads the three rows back from the DB,
joins the live app version, and computes a `currency` flag by comparing
major.minor of the running app to the version the doc was last audited
against.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter

from app.services.settings_store import get_secret, set_secret

router = APIRouter(prefix="/api", tags=["help"])

logger = logging.getLogger("switchboard.help")

_HELP_FILE = Path(__file__).resolve().parents[1] / "dashboard" / "help.html"

# Setting keys for help metadata. Kept in the existing `settings` table.
KEY_DOC_VERSION = "help.doc_version"
KEY_COVERED_APP_VERSION = "help.covered_app_version"
KEY_LAST_UPDATED = "help.last_updated"

# Fallbacks used only if help.html is missing or its comments don't parse.
_FALLBACK_DOC_VERSION = "0.0.0"
_FALLBACK_COVERED_APP_VERSION = "0.0.0"
_FALLBACK_LAST_UPDATED = "unknown"

_COMMENT_PATTERNS = {
    KEY_DOC_VERSION: re.compile(r"<!--\s*HELP_DOC_VERSION:\s*(\S+)\s*-->"),
    KEY_COVERED_APP_VERSION: re.compile(r"<!--\s*HELP_COVERED_APP_VERSION:\s*(\S+)\s*-->"),
    KEY_LAST_UPDATED: re.compile(r"<!--\s*HELP_LAST_UPDATED:\s*(\S+)\s*-->"),
}


def _parse_help_file() -> dict[str, str]:
    """Read the first ~2 KB of help.html and extract the three version comments.

    Returns a dict keyed by the setting key (`help.doc_version`, etc.). Any
    missing comment falls back to its fallback constant.
    """
    parsed: dict[str, str] = {
        KEY_DOC_VERSION: _FALLBACK_DOC_VERSION,
        KEY_COVERED_APP_VERSION: _FALLBACK_COVERED_APP_VERSION,
        KEY_LAST_UPDATED: _FALLBACK_LAST_UPDATED,
    }
    if not _HELP_FILE.exists():
        return parsed
    try:
        head = _HELP_FILE.read_text(encoding="utf-8")[:2048]
    except Exception:  # noqa: BLE001
        return parsed
    for key, pattern in _COMMENT_PATTERNS.items():
        m = pattern.search(head)
        if m:
            parsed[key] = m.group(1).strip()
    return parsed


def sync_help_metadata_from_file() -> None:
    """Read help.html's top comments and upsert the three settings rows.

    Called from the FastAPI lifespan after `init_database()`. Safe to call
    repeatedly; only writes a row when the new value differs from the stored
    one. Logs (info) on bump, (warning) when the file is missing.
    """
    parsed = _parse_help_file()
    if not _HELP_FILE.exists():
        logger.warning(
            "help.html not found at %s; help metadata not synced (using fallbacks)",
            _HELP_FILE,
        )
        return
    for key, value in parsed.items():
        current = get_secret(key)
        if current != value:
            set_secret(key, value)
            logger.info("help metadata sync: %s -> %s (was %r)", key, value, current)


def _major_minor(version: str) -> Optional[tuple[int, int]]:
    """Parse `1.2.3` → (1, 2). Returns None if the string isn't parseable."""
    parts = version.split(".")
    if len(parts) < 2:
        return None
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return None


def _compute_currency(covered: str, live: str) -> str:
    """Return one of: `current`, `app_newer`, `older_app`, `unknown`.

    Compared on major.minor only — patch differences do not change the
    indicator (per the help-doc design call).
    """
    a = _major_minor(covered)
    b = _major_minor(live)
    if a is None or b is None:
        return "unknown"
    if a == b:
        return "current"
    if b > a:
        return "app_newer"
    return "older_app"


@router.get("/help/metadata")
async def help_metadata() -> dict:
    """Help document metadata + live app version + computed currency flag."""
    from app.main import app as _app  # local import: avoid circular at module load

    doc_version = get_secret(KEY_DOC_VERSION) or _FALLBACK_DOC_VERSION
    covered = get_secret(KEY_COVERED_APP_VERSION) or _FALLBACK_COVERED_APP_VERSION
    last_updated = get_secret(KEY_LAST_UPDATED) or _FALLBACK_LAST_UPDATED
    app_version = _app.version

    return {
        "doc_version": doc_version,
        "covered_app_version": covered,
        "last_updated": last_updated,
        "app_version": app_version,
        "currency": _compute_currency(covered, app_version),
    }
