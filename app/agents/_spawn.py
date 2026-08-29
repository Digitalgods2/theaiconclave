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
stop a child — timeouts use proc.kill() (TerminateProcess) — so disabling the
child's default Ctrl-C handling is harmless.

On non-Windows platforms this is an empty dict (no-op).
"""

from __future__ import annotations

import subprocess
import sys

if sys.platform == "win32":  # pragma: no cover - platform-specific
    SPAWN_KWARGS: dict = {
        "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    }
else:  # pragma: no cover - platform-specific
    SPAWN_KWARGS: dict = {}

__all__ = ["SPAWN_KWARGS"]
