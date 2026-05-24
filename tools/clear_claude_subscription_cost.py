#!/usr/bin/env python3
"""One-shot cleanup for the claude-code subscription-cost recording bug.

Before the fix, `app/agents/claude_adapter.py::_extract_usage_from_claude`
recorded the Claude CLI's `total_cost_usd` envelope field on every run, even
in subscription mode where the Pro/Max plan absorbs the actual bill. Existing
`agent_runs` rows for `claude-code` carry that wrong USD figure.

This script NULLs `cost_usd` for those rows, but only when claude-code is
currently in subscription mode (per `~/.claude/.credentials.json`) — if you
later flip the CLI into API mode, legitimately-recorded API-mode rows will
not be clobbered by re-running this.

Idempotent: only updates rows where `cost_usd > 0`. Safe to re-run.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import connect, init_database  # noqa: E402
from app.utils.paths import default_db_path  # noqa: E402


def main() -> int:
    creds = Path.home() / ".claude" / ".credentials.json"
    if not (creds.exists() and creds.stat().st_size > 0):
        print(
            f"claude-code does not look like it is in subscription mode "
            f"(no {creds}); refusing to clear cost_usd values so this "
            f"can't accidentally wipe legitimate API-mode billing data.",
            file=sys.stderr,
        )
        return 2

    init_database(str(default_db_path()))
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, SUM(cost_usd) AS total FROM agent_runs "
            "WHERE agent_name = 'claude-code' AND cost_usd > 0"
        ).fetchone()
        n = row["n"]
        total = row["total"] or 0.0

        if n == 0:
            print("Nothing to clear — no claude-code rows with recorded cost.")
            return 0

        print(f"Found {n} claude-code agent_runs with recorded cost "
              f"(total ${total:.4f}).")
        conn.execute(
            "UPDATE agent_runs SET cost_usd = NULL "
            "WHERE agent_name = 'claude-code' AND cost_usd > 0"
        )
        print(f"Cleared. Refresh the dashboard's Usage view to see it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
