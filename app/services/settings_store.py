"""Tiny key/value store over the existing `settings` table.

Used for app-level configuration that the user sets through the dashboard
(currently: API keys). Distinct from `config.yaml`, which is operator-set and
read at startup — `settings` is user-editable at runtime and persisted in the DB.

Secrets stored here live in plaintext in `data/switchboard.db`. That file is
single-user, local-only, and gitignored; this is the same trust boundary as a
`.env` file on the same machine. We do not encrypt at rest (no key-management
story for a single-user local tool), but the API never echoes a stored secret
except via the explicit `/reveal` endpoint, and never logs it.
"""

from __future__ import annotations

from typing import Optional

from app.database import connect, now_iso


def get_secret(key: str) -> Optional[str]:
    """Return the stored value for `key`, or None if absent.

    Returns None (rather than raising) if the database hasn't been initialised —
    so call sites like an adapter's API-key lookup degrade gracefully in unit
    tests that construct adapters directly without a DB.
    """
    try:
        with connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None
    except Exception:  # noqa: BLE001 — DB not initialised, locked, etc.
        return None


def set_secret(key: str, value: str) -> None:
    """Upsert `key` -> `value`. Caller is responsible for stripping/validating."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO settings (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (key, value, now_iso()),
        )


def delete_secret(key: str) -> None:
    """Remove `key` if present. No-op if absent or DB not initialised."""
    try:
        with connect() as conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    except Exception:  # noqa: BLE001
        pass


__all__ = ["get_secret", "set_secret", "delete_secret"]
