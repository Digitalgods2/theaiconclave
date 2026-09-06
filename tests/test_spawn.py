"""Subprocess lifecycle guarantees shared by the CLI adapters."""

from __future__ import annotations

import asyncio

import pytest

from app.agents._spawn import communicate_with_cleanup, terminate_process


class _Process:
    def __init__(self, *, result: tuple[bytes, bytes] | None = None) -> None:
        self.returncode = None
        self.killed = False
        self.waited = False
        self._result = result
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def communicate(self, input=None):
        self.started.set()
        if self._result is not None:
            self.returncode = 0
            return self._result
        await self.release.wait()
        self.returncode = 0
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        self.release.set()

    async def wait(self) -> int:
        self.waited = True
        self.returncode = -9
        return self.returncode


async def test_communicate_timeout_kills_and_reaps_child():
    proc = _Process()

    with pytest.raises(asyncio.TimeoutError):
        await communicate_with_cleanup(proc, timeout=0.01)

    assert proc.killed is True
    assert proc.waited is True


async def test_coroutine_cancellation_kills_and_reaps_child():
    proc = _Process()
    running = asyncio.create_task(communicate_with_cleanup(proc, timeout=60))
    await proc.started.wait()

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert proc.killed is True
    assert proc.waited is True


async def test_successful_communication_does_not_kill_child():
    proc = _Process(result=(b"version", b""))

    result = await communicate_with_cleanup(proc, timeout=1)

    assert result == (b"version", b"")
    assert proc.killed is False
    assert proc.waited is False


async def test_no_timeout_waits_normally_until_completion():
    proc = _Process(result=(b"complete", b""))

    result = await communicate_with_cleanup(proc, timeout=None)

    assert result == (b"complete", b"")
    assert proc.killed is False


async def test_terminate_process_waits_for_already_exited_child():
    proc = _Process()
    proc.returncode = 0

    await terminate_process(proc)

    assert proc.killed is False
    assert proc.waited is True
