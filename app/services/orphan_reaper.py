"""Orphan task reaper — startup sweep for tasks stuck in `running`.

Phase 1 of the post-DR (tsk_01KRSW6AS3M66B4RRJE3JFAPRV) recoverability plan.
Marks any task whose `updated_at` is older than the threshold as `failed`
with a clear error_message and a `task_orphaned` log entry. Transcript,
agent_runs, agent_messages, and final_results (if any) are preserved
verbatim — only the task status changes.

Deliberately tiny: no UI, no API endpoint, no recovery actions. If we
start seeing stuck tasks in practice the full Recovery Console (Phase 3
of the plan) becomes worth building.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from app.database import connect, now_iso
from app.utils.ids import log_id

logger = logging.getLogger("switchboard.reaper")

DEFAULT_THRESHOLD_HOURS = 1.0


def reap_orphans(threshold_hours: float = DEFAULT_THRESHOLD_HOURS) -> int:
    """Mark `running` tasks idle for >threshold_hours as `failed`.

    Returns the number of tasks reaped. Idempotent — if no orphans exist
    it does nothing.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=threshold_hours)).isoformat()
    reason = f"orphaned: no progress for >{threshold_hours:g}h; marked failed by startup reaper"
    now = now_iso()
    reaped = 0

    with connect() as conn:
        rows = conn.execute(
            "SELECT id, updated_at FROM tasks WHERE status = 'running' AND updated_at < ?",
            (cutoff,),
        ).fetchall()

        for row in rows:
            tid = row["id"]
            conn.execute(
                "UPDATE tasks SET status = 'failed', updated_at = ?, error_message = ? WHERE id = ?",
                (now, reason, tid),
            )
            conn.execute(
                """INSERT INTO logs (id, task_id, level, event_type, message, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    log_id(),
                    tid,
                    "warn",
                    "task_orphaned",
                    reason,
                    json.dumps({"last_updated_at": row["updated_at"], "threshold_hours": threshold_hours}),
                    now,
                ),
            )
            reaped += 1

    if reaped:
        logger.warning("Reaped %d orphaned task(s) (>%gh idle)", reaped, threshold_hours)
    return reaped
