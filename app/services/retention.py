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
from app.utils.paths import default_db_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def effective_db_path(db_path: str | Path | None) -> Path:
    """Resolve the configured DB path exactly as application startup does."""
    return Path(db_path) if db_path else default_db_path()


def db_size_bytes(db_path: str | Path | None) -> int:
    """Total size of the SQLite file + WAL on disk."""
    total = 0
    base = effective_db_path(db_path)
    for suffix in ("", "-wal", "-shm"):
        p = base.with_name(base.name + suffix) if suffix else base
        if p.exists():
            total += p.stat().st_size
    return total


def completed_task_count() -> int:
    """Count terminal tasks that still retain Tier-3 message history.

    Tier-1 task rows are deliberately never auto-deleted, so counting all
    terminal rows made ``max_completed_tasks`` impossible to satisfy. The
    operational count now measures the retention-managed payload: terminal
    tasks whose raw transcript is still present. Trimming a transcript moves
    this count toward the cap without sacrificing task identity, decisions,
    or parent/child links.
    """
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM tasks t
            WHERE t.status IN ('completed', 'failed', 'cancelled')
              AND EXISTS (
                  SELECT 1 FROM agent_messages am WHERE am.task_id = t.id
              )
            """
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


def find_trimmable_tier2_tasks(min_age_days: int) -> list[str]:
    """Return Tier-2-eligible task IDs for post-export trimming, oldest-first.

    Eligibility (all must hold):
      - status is terminal (completed | failed | cancelled)
      - final_results row exists (something to trim)
      - exported_at IS NOT NULL — the markdown export on disk is the
        long-term archive of the verdict
      - created_at older than min_age_days
      - not referenced as parent_task_id by any other task (no live thread)

    Opt-in via retention.trim_tier2_after_export — disabled by default
    per DR0003's "retain indefinitely until exported" stance.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).isoformat()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT t.id, t.export_path
            FROM tasks t
            JOIN final_results fr ON t.id = fr.task_id
            WHERE t.status IN ('completed', 'failed', 'cancelled')
              AND t.exported_at IS NOT NULL
              AND t.created_at < ?
              AND NOT EXISTS (
                  SELECT 1 FROM tasks child WHERE child.parent_task_id = t.id
              )
            ORDER BY t.created_at ASC
            """,
            (cutoff,),
        ).fetchall()
    eligible = []
    for row in rows:
        try:
            archive = Path(row["export_path"]) if row["export_path"] else None
            if archive and archive.is_file() and archive.stat().st_size > 0:
                eligible.append(row["id"])
        except OSError:
            continue
    return eligible


# ---------------------------------------------------------------------------
# Trim
# ---------------------------------------------------------------------------

def _delete_messages_for(task_id: str) -> int:
    with connect() as conn:
        cur = conn.execute("DELETE FROM agent_messages WHERE task_id = ?", (task_id,))
        return cur.rowcount


def _delete_final_result_for(task_id: str) -> int:
    with connect() as conn:
        cur = conn.execute("DELETE FROM final_results WHERE task_id = ?", (task_id,))
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


def retained_db_bytes() -> int:
    """Live SQLite pages, excluding reusable pages that VACUUM will reclaim."""
    with connect() as conn:
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        size = conn.execute("PRAGMA page_size").fetchone()[0]
    return (pages - free) * size


def trim_to_budget(
    *,
    max_db_size_bytes: int,
    max_task_count: int,
    min_age_days: int,
    db_path: str | Path | None,
    trim_tier2_after_export: bool = False,
) -> dict[str, Any]:
    """Run a single retention pass. Returns a summary dict suitable for logging.

    Tier 3 (agent_messages) is trimmed first. If `trim_tier2_after_export` is
    True and budgets are still over after Tier 3, Tier 2 (final_results) is
    additionally trimmed for tasks whose markdown export already exists.
    """
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
        if (retained_db_bytes() <= max_db_size_bytes
                and completed_task_count() <= max_task_count):
            break
        n = _delete_messages_for(tid)
        trimmed_messages += n
        trimmed_tasks.append(tid)

    # Tier 2: only if opt-in flag is set AND Tier 3 wasn't enough.
    trimmed_tier2_tasks: list[str] = []
    trimmed_final_results = 0
    if (trim_tier2_after_export
            and (retained_db_bytes() > max_db_size_bytes
                 or completed_task_count() > max_task_count)):
        for tid in find_trimmable_tier2_tasks(min_age_days):
            if (retained_db_bytes() <= max_db_size_bytes
                    and completed_task_count() <= max_task_count):
                break
            n = _delete_final_result_for(tid)
            if n:
                trimmed_final_results += n
                trimmed_tier2_tasks.append(tid)

    vacuumed = False
    if size_over or trimmed_tasks or trimmed_tier2_tasks:
        vacuumed = _vacuum()

    return {
        "ran": True,
        "triggers": triggers,
        "trimmed_task_count": len(trimmed_tasks),
        "trimmed_message_count": trimmed_messages,
        "trimmed_task_ids": trimmed_tasks,
        "trimmed_tier2_task_count": len(trimmed_tier2_tasks),
        "trimmed_tier2_task_ids": trimmed_tier2_tasks,
        "trimmed_final_result_count": trimmed_final_results,
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
            operation = asyncio.create_task(asyncio.to_thread(
                trim_to_budget,
                max_db_size_bytes=config.retention.max_db_size_mb * 1024 * 1024,
                max_task_count=config.retention.max_completed_tasks,
                min_age_days=config.retention.min_task_age_days,
                db_path=effective_db_path(config.database.path),
                trim_tier2_after_export=getattr(
                    config.retention, "trim_tier2_after_export", False
                ),
            ))
            try:
                result = await asyncio.shield(operation)
            except asyncio.CancelledError:
                # Retain the instance lock until the database writer has stopped.
                await operation
                raise
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
