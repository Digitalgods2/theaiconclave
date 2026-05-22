"""Double-clickable launcher for The AI Conclave Switchboard. Cross-platform.

Windows: run by `pythonw.exe` (via the .pyw extension) so no console window
appears. Invoked by the desktop shortcut created by
`tools/install-desktop-shortcut.ps1`.

macOS: run by `python3` from inside an .app bundle's launcher script.
Created by `tools/install-desktop-app.sh`. The .app contains
`Contents/MacOS/launcher` which `exec`s `python3 launch.pyw`. No Terminal
window appears because .app bundles don't open one.

Linux: same as macOS but no convenience installer ships in the repo. Drop
a .desktop file in `~/.local/share/applications/` that points at
`python3 /path/to/launch.pyw`.

What the launcher does:

  1. Checks whether the service is already running by polling /api/health.
  2. If not, starts `python -m uvicorn app.main:app` as a fully detached
     background process — closing this launcher does not kill the service.
     Windows: CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW.
     POSIX:   start_new_session=True (POSIX setsid equivalent).
  3. Polls /api/health for up to 30 seconds until it responds.
  4. Opens http://127.0.0.1:8787/ in the default browser.
  5. Exits. (The uvicorn subprocess keeps running.)

To stop the service: kill the python process (Task Manager on Windows,
`pkill -f 'uvicorn app.main'` on macOS/Linux), or delete the pidlock file
at <repo>/data/switchboard.pid. The single-instance pidlock means
re-running this launcher while the service is up is a no-op on the service
side — it just opens a fresh browser tab.

Logs at `<repo>/data/launcher.log` capture both the launcher's own events
and uvicorn's stdout/stderr — useful when the browser tab fails to open.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

REPO = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8787
URL = f"http://{HOST}:{PORT}/"
HEALTH_URL = f"http://{HOST}:{PORT}/api/health"
STARTUP_TIMEOUT_SECONDS = 30
LOG_PATH = REPO / "data" / "launcher.log"


def _log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def _already_running() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _resolve_python_exe() -> str:
    """Resolve the Python interpreter for the uvicorn subprocess.

    On Windows when running under `pythonw.exe`, we switch to `python.exe`
    so uvicorn's stdout/stderr can be captured into the log file normally.
    On POSIX (macOS/Linux), `sys.executable` is always the right answer.
    """
    exe = sys.executable
    if os.name == "nt" and exe.lower().endswith("pythonw.exe"):
        return exe[: -len("pythonw.exe")] + "python.exe"
    return exe


def _start_uvicorn() -> None:
    """Spawn uvicorn in a fully detached child process. Closing the launcher
    does NOT kill it on either platform.

    Windows: CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW.
    POSIX:   start_new_session=True — equivalent of POSIX setsid(2), the
             child runs in its own session and survives the launcher exit.
    """
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logf = LOG_PATH.open("a", encoding="utf-8")
    logf.write(f"\n--- starting uvicorn {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    logf.flush()

    popen_kwargs: dict = {
        "cwd": str(REPO),
        "stdout": logf,
        "stderr": logf,
        "stdin": subprocess.DEVNULL,
        "close_fds": False,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        popen_kwargs["start_new_session"] = True

    cmd = [
        _resolve_python_exe(), "-m", "uvicorn",
        "app.main:app", "--host", HOST, "--port", str(PORT),
    ]
    subprocess.Popen(cmd, **popen_kwargs)
    # Deliberately leak `logf` — the child holds it open for the lifetime of
    # the service; the OS reaps it when uvicorn exits.


def _wait_for_health(timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _already_running():
            return True
        time.sleep(0.5)
    return False


def _show_error(message: str) -> None:
    """Surface a startup failure in a small native dialog instead of silently
    dying. Falls back silently if tkinter isn't available."""
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("The AI Conclave Switchboard launch failed", message)
        root.destroy()
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    _log("launcher: invoked")

    if _already_running():
        _log("launcher: service already up, opening browser")
        webbrowser.open(URL)
        return 0

    _start_uvicorn()
    _log(f"launcher: uvicorn started, waiting up to {STARTUP_TIMEOUT_SECONDS}s for /api/health")

    if _wait_for_health(STARTUP_TIMEOUT_SECONDS):
        _log("launcher: health ok, opening browser")
        webbrowser.open(URL)
        return 0

    _log("launcher: timed out waiting for /api/health")
    _show_error(
        f"The AI Conclave Switchboard didn't respond on {URL} within {STARTUP_TIMEOUT_SECONDS} seconds.\n\n"
        f"Check the log:\n{LOG_PATH}\n\n"
        "Common causes: port 8787 already in use, pidlock collision with a "
        "stale instance, or a Python environment issue."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
