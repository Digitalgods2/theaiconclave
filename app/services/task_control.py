"""In-process control for actively running deliberation tasks.

The task row is the durable source of truth for status.  This registry adds
the missing process-local piece: it lets the HTTP cancel endpoint interrupt
the coroutine that is currently waiting on an agent.  Adapter cancellation
handlers are then responsible for terminating any child process they own.
"""

from __future__ import annotations

import asyncio


_active_tasks: dict[str, asyncio.Task[None]] = {}


def register(task_id: str, task: asyncio.Task[None]) -> None:
    """Register the asyncio task currently executing ``task_id``."""
    _active_tasks[task_id] = task


def unregister(task_id: str, task: asyncio.Task[None]) -> None:
    """Remove ``task`` without disturbing a newer runner for the same ID."""
    if _active_tasks.get(task_id) is task:
        _active_tasks.pop(task_id, None)


def cancel(task_id: str) -> bool:
    """Request immediate cancellation of an active task.

    Returns ``True`` when a live coroutine was found.  Pending tasks and tasks
    running in another process still rely on the durable cancelled DB status.
    """
    task = _active_tasks.get(task_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


def is_active(task_id: str) -> bool:
    task = _active_tasks.get(task_id)
    return bool(task is not None and not task.done())


def clear() -> None:
    """Test helper; production entries are removed by the worker's finally."""
    _active_tasks.clear()


__all__ = ["cancel", "clear", "is_active", "register", "unregister"]
