"""Double-clickable launcher for AI Switchboard.

Run by `pythonw.exe` (note the .pyw extension) so no console window appears.
The launcher:

  1. Checks whether the service is already running by polling /api/health.
  2. If not, starts `python -m uvicorn app.main:app` as a fully detached
     background process — closing this launcher does not kill the service.
  3. Polls /api/health for up to 30 seconds until it responds.
  4. Opens http://127.0.0.1:8787/ in the default browser.
  5. Exits. (The uvicorn subprocess keeps running.)

To stop the service, either kill the python process in Task Manager or
delete the pidlock file at <repo>/data/switchboard.pid. The single-instance
pidlock means re-running this launcher while the service is up is a no-op
on the service side — it just opens a fresh browser tab.

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
    """When running under `pythonw.exe`, switch to `python.exe` for the child
    so uvicorn's stdout/stderr can be captured into the log file normally.
    """
    exe = sys.executable
    lower = exe.lower()
    if lower.endswith("pythonw.exe"):
        return exe[: -len("pythonw.exe")] + "python.exe"
    return exe


def _start_uvicorn() -> None:
    """Spawn uvicorn in a fully detached child process."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logf = LOG_PATH.open("a", encoding="utf-8")
    logf.write(f"\n--- starting uvicorn {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    logf.flush()

    flags = 0
    if os.name == "nt":
        flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )

    cmd = [
        _resolve_python_exe(), "-m", "uvicorn",
        "app.main:app", "--host", HOST, "--port", str(PORT),
    ]
    subprocess.Popen(
        cmd,
        cwd=str(REPO),
        stdout=logf,
        stderr=logf,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=False,
    )
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
        messagebox.showerror("AI Switchboard launch failed", message)
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
        f"AI Switchboard didn't respond on {URL} within {STARTUP_TIMEOUT_SECONDS} seconds.\n\n"
        f"Check the log:\n{LOG_PATH}\n\n"
        "Common causes: port 8787 already in use, pidlock collision with a "
        "stale instance, or a Python environment issue."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
