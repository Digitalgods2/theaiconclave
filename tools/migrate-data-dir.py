#!/usr/bin/env python3
"""One-shot: relocate the packaged user-data directory to the rebranded name.

Moves the platform user-data directory from its pre-rebrand name
("AI Switchboard") to the current name ("The AI Conclave"):

  Windows : %LOCALAPPDATA%\\AI Switchboard  ->  %LOCALAPPDATA%\\The AI Conclave
  macOS   : ~/Library/Application Support/AI Switchboard  ->  .../The AI Conclave
  Linux   : ~/.local/share/ai-switchboard  ->  ~/.local/share/ai-conclave

It is a whole-directory atomic rename — the SQLite DB, config, exports,
uploads, and sandboxes move together. Safe to run repeatedly: it does nothing
once migrated, or when there is no legacy directory. It never touches a
repo-local ./data/ directory — only the platform user-data location.

Run it with the service stopped:

    python tools/migrate-data-dir.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.migration import MigrationBlocked, migrate_legacy_data_dir  # noqa: E402
from app.utils.paths import (  # noqa: E402
    legacy_platform_user_data_root,
    platform_user_data_root,
)


def main() -> int:
    old = legacy_platform_user_data_root()
    new = platform_user_data_root()

    print("The AI Conclave — user-data directory migration")
    print(f"  legacy location : {old}")
    print(f"  new location    : {new}")
    print()

    try:
        result = migrate_legacy_data_dir()
    except MigrationBlocked as exc:
        print(f"Migration blocked:\n  {exc}", file=sys.stderr)
        return 2

    if result is None:
        if not old.is_dir():
            print("Nothing to migrate — no legacy directory exists.")
        else:
            print("Nothing to migrate — already migrated, or the legacy directory is empty.")
        return 0

    print("Migration complete.")
    print(f"  moved : {result['src']}")
    print(f"     -> : {result['dst']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
