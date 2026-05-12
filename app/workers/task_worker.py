"""Background worker that picks up pending tasks and runs them."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.config import Config
from app.database import connect, now_iso, with_retry
from app.services.orchestrator import run_task

logger = logging.getLogger(__name__)


def _claim_next_pending() -> Optional[str]:
    """
    Atomically claim the oldest pending task by setting its status to running.
    Uses UPDATE ... RETURNING (SQLite >= 3.35) to avoid races with another
    claim attempt. Wrapped in with_retry because this runs on every worker
    tick and is the most contention-prone write.
    """
    def _do() -> Optional[str]:
        with connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = 'running', updated_at = ?
                WHERE id = (
                    SELECT id FROM tasks WHERE status = 'pending'
                    ORDER BY created_at LIMIT 1
                )
                RETURNING id
                """,
                (now_iso(),),
            )
            row = cursor.fetchone()
            return row["id"] if row else None
    return with_retry(_do)


async def worker_loop(config: Config) -> None:
    """Poll the tasks table and process pending tasks one at a time."""
    interval = config.orchestration.worker_poll_interval_seconds
    logger.info("Worker started; polling every %d seconds.", interval)
    while True:
        try:
            tid = _claim_next_pending()
            if tid:
                logger.info("Picked up task %s", tid)
                await run_task(tid)
            else:
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("Worker cancelled.")
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("Worker error: %s", e)
            await asyncio.sleep(interval)
