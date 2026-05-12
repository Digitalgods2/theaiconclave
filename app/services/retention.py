"""Database retention policy per decision 0003.

The conclave deliberated on what metric should decide trimming and converged on
a two-part approach:

    Operational trigger (when):  DB size OR completed-task count
    Semantic selection (what):   structural tiering

Tier 1 — never auto-trim:
    - The task row itself (preserves user_decision and parent_task_id linkages)
    - Tasks with unresolved dissent (agreement_level in {major_disagreement,
      unresolved}) that don't have a user_decision recorded — these are open
      questions awaiting Glen's judgment.

Tier 2 — retain indefinitely until exported:
    - final_results rows. Carry the conclave's verdict and the agreement_level.

Tier 3 — trim first:
    - agent_messages rows for tasks that are terminal, summarized
      (final_results exists), old enough (min_age_days), unreferenced
      (no other task points at them via parent_task_id), and either
      cleanly resolved (consensus) or formally decided (user_decision set).

The retention worker runs on a configurable interval. VACUUM is invoked after
a successful trim so SQLite actually reclaims the space.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.database import connect, with_retry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def db_size_bytes(db_path: str | Path) -> int:
    """Total size of the SQLite file + WAL on disk."""
    total = 0
    base = Path(db_path)
    for suffix in ("", "-wal", "-shm"):
        p = base.with_name(base.name + suffix) if suffix else base
        if p.exists():
            total += p.stat().st_size
    return total


def completed_task_count() -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status IN ('completed', 'failed', 'cancelled')"
        ).fetchone()
    return row["n"]


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def find_trimmable_tasks(min_age_days: int) -> list[str]:
    """Return Tier-3-eligible task IDs, oldest-first.

    Eligibility (all must hold):
      - status is terminal (completed | failed | cancelled)
      - final_results row exists (already summarized)
      - created_at older than min_age_days
      - not referenced as parent_task_id by any other task (no live thread)
      - agent_messages rows still exist (not already trimmed)
      - resolution status: agreement_level=='consensus' OR user_decision IS NOT NULL
        (i.e. cleanly converged, OR Glen recorded a decision that closes the
         dissent — unresolved dissent without a decision stays Tier 1)
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).isoformat()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT t.id
            FROM tasks t
            JOIN final_results fr ON t.id = fr.task_id
            WHERE t.status IN ('completed', 'failed', 'cancelled')
              AND t.created_at < ?
              AND (
                  fr.agreement_level = 'consensus'
                  OR t.user_decision IS NOT NULL
              )
              AND NOT EXISTS (
                  SELECT 1 FROM tasks child WHERE child.parent_task_id = t.id
              )
              AND EXISTS (
                  SELECT 1 FROM agent_messages am WHERE am.task_id = t.id
              )
            ORDER BY t.created_at ASC
            """,
            (cutoff,),
        ).fetchall()
    return [r["id"] for r in rows]


# ---------------------------------------------------------------------------
# Trim
# ---------------------------------------------------------------------------

def _delete_messages_for(task_id: str) -> int:
    with connect() as conn:
        cur = conn.execute("DELETE FROM agent_messages WHERE task_id = ?", (task_id,))
        return cur.rowcount


def _vacuum() -> bool:
    """Run VACUUM with retry-on-locked. Long-running operation; can collide
    with other writers despite busy_timeout. with_retry handles those cases
    with exponential backoff before giving up."""
    def _do() -> None:
        with connect() as conn:
            conn.execute("VACUUM")
    try:
        with_retry(_do, max_attempts=3, base_delay=0.5)
        return True
    except sqlite3.OperationalError as e:
        logger.warning("VACUUM failed after retries (likely sustained contention): %s", e)
        return False


def trim_to_budget(
    *,
    max_db_size_bytes: int,
    max_task_count: int,
    min_age_days: int,
    db_path: str | Path,
) -> dict[str, Any]:
    """Run a single retention pass. Returns a summary dict suitable for logging."""
    initial_size = db_size_bytes(db_path)
    initial_count = completed_task_count()

    size_over = initial_size > max_db_size_bytes
    count_over = initial_count > max_task_count

    if not (size_over or count_over):
        return {
            "ran": False,
            "reason": "under budget",
            "db_size_bytes": initial_size,
            "completed_task_count": initial_count,
        }

    triggers = []
    if size_over:
        triggers.append(f"db_size {initial_size} > {max_db_size_bytes}")
    if count_over:
        triggers.append(f"task_count {initial_count} > {max_task_count}")

    eligible = find_trimmable_tasks(min_age_days)
    trimmed_tasks: list[str] = []
    trimmed_messages = 0

    for tid in eligible:
        # Re-check budgets each iteration; stop when both are satisfied.
        if (db_size_bytes(db_path) <= max_db_size_bytes
                and completed_task_count() <= max_task_count):
            break
        n = _delete_messages_for(tid)
        trimmed_messages += n
        trimmed_tasks.append(tid)

    vacuumed = False
    if trimmed_tasks:
        vacuumed = _vacuum()

    return {
        "ran": True,
        "triggers": triggers,
        "trimmed_task_count": len(trimmed_tasks),
        "trimmed_message_count": trimmed_messages,
        "trimmed_task_ids": trimmed_tasks,
        "vacuumed": vacuumed,
        "before": {"db_size_bytes": initial_size, "completed_task_count": initial_count},
        "after": {
            "db_size_bytes": db_size_bytes(db_path),
            "completed_task_count": completed_task_count(),
        },
    }


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

async def retention_loop(config) -> None:
    """Periodic retention worker. Runs once on startup, then every interval."""
    if not getattr(config.retention, "enabled", True):
        logger.info("Retention worker disabled.")
        return

    interval = config.retention.check_interval_seconds
    logger.info(
        "Retention worker started. Budget: %s MB / %s tasks. Min age: %s days. Check every %s s.",
        config.retention.max_db_size_mb,
        config.retention.max_completed_tasks,
        config.retention.min_task_age_days,
        interval,
    )

    while True:
        try:
            result = trim_to_budget(
                max_db_size_bytes=config.retention.max_db_size_mb * 1024 * 1024,
                max_task_count=config.retention.max_completed_tasks,
                min_age_days=config.retention.min_task_age_days,
                db_path=config.database.path,
            )
            if result.get("ran"):
                logger.info("Retention pass: %s", result)
            else:
                logger.debug("Retention pass: under budget; nothing trimmed.")
        except asyncio.CancelledError:
            logger.info("Retention worker cancelled.")
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("Retention worker error: %s", e)
        await asyncio.sleep(interval)
