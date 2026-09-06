"""Shared subprocess spawn settings for CLI agent adapters.

On Windows, child CLIs spawned by the long-running server must be isolated from
the server's console process group. Otherwise a console control event
(CTRL_C / CTRL_BREAK, or console teardown when the launching terminal closes)
delivered to the server's console propagates to the children and kills them with
STATUS_CONTROL_C_EXIT (0xC000013A) even though the server itself keeps running
(uvicorn installs its own SIGINT/SIGBREAK handlers, so the parent survives while
a freshly-spawned child that shares the console dies immediately).

This was observed as every conclave CLI seat dying ~1s after spawn — e.g.
"codex exited with code 3221225786" with stderr that only got as far as
"Reading prompt from stdin..." — while a standalone run of the exact same
command from a healthy interactive console worked fine.

CREATE_NO_WINDOW runs the child without attaching to the parent's console;
CREATE_NEW_PROCESS_GROUP additionally makes it the root of its own process group
so a group-targeted CTRL_C/CTRL_BREAK can't reach it. We never rely on Ctrl-C to
stop a child; explicit cancellation terminates its complete process tree and
then reaps the root, so disabling the child's Ctrl-C handling is harmless.

On non-Windows platforms each CLI starts a new session so its complete process
group can likewise be terminated without affecting the service.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from typing import Optional

if sys.platform == "win32":  # pragma: no cover - platform-specific
    SPAWN_KWARGS: dict = {
        "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    }
else:  # pragma: no cover - platform-specific
    SPAWN_KWARGS: dict = {"start_new_session": True}


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Best-effort termination of ``proc`` and any CLI descendants."""
    pid = getattr(proc, "pid", None)
    if sys.platform == "win32" and isinstance(pid, int):  # pragma: no cover - integration
        # npm-installed CLIs commonly run through a cmd shim. Terminating only
        # that wrapper can leave its node.exe child consuming tokens. taskkill
        # /T closes the complete descendant tree without invoking a shell.
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                await asyncio.wait_for(killer.communicate(), timeout=5)
            except asyncio.TimeoutError:
                killer.kill()
                await killer.wait()
        except (FileNotFoundError, OSError, ProcessLookupError):
            pass
    elif isinstance(pid, int):  # pragma: no cover - integration
        try:
            os.killpg(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    # Tree termination is best-effort; always fall back to the asyncio handle
    # for the root if the platform helper did not finish it.
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def terminate_process(proc: asyncio.subprocess.Process) -> None:
    """Kill and reap a child process, tolerating an already-exited process.

    ``Process.kill()`` only requests termination.  Awaiting ``wait()`` is what
    releases the OS process handle (and avoids zombies on POSIX).  Keeping this
    in one helper ensures timeout and explicit coroutine cancellation take the
    same cleanup path in every CLI adapter.
    """
    if proc.returncode is None:
        await _kill_process_tree(proc)
    try:
        await proc.wait()
    except ProcessLookupError:
        pass


async def communicate_with_cleanup(
    proc: asyncio.subprocess.Process,
    *,
    input: Optional[bytes] = None,
    timeout: Optional[float] = None,
) -> tuple[bytes, bytes]:
    """Communicate with a child and always reap it on interruption or failure.

    The caller still decides how a transport timeout is presented.  In
    particular, this helper does not turn elapsed time into task cancellation;
    it only prevents a genuinely stuck subprocess from being orphaned.
    """
    try:
        if timeout is None:
            return await proc.communicate(input=input)
        return await asyncio.wait_for(proc.communicate(input=input), timeout=timeout)
    except BaseException:
        await terminate_process(proc)
        raise


__all__ = ["SPAWN_KWARGS", "communicate_with_cleanup", "terminate_process"]
