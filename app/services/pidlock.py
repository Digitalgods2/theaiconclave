"""PID lockfile — single-instance enforcement for the AI Conclave Switchboard service.

Why: every running process spins up its own worker_loop that polls the same
SQLite for pending tasks. If two processes are alive at once (e.g. you forgot
about an old one on a different port), tasks race between them — and if their
registries differ, the same task can run with different agents on different
runs. We've been bitten by this. This module makes it impossible.

How: at startup we write `data/switchboard.pid` containing our PID and the
process's creation-time stamp. If the file already exists when we start, we
read it and check whether that PID is *actually alive* AND was created at
the recorded time (defending against PID reuse). If yes, we refuse to start
with a clear error. If no (stale lockfile from a crashed prior run), we
quietly take it over.

Cross-platform:
  - Windows: ctypes calls into kernel32 for OpenProcess + GetExitCodeProcess
             + GetProcessTimes. Read-only operations — no risk of accidentally
             terminating anything.
  - POSIX:   os.kill(pid, 0) for the alive check (signal 0 is a no-op
             permission test), /proc/<pid>/stat for creation time on Linux,
             psutil-like fallback omitted (no extra deps).
"""

from __future__ import annotations

import logging
import os
import platform
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("switchboard.pidlock")

_LOCKFILE_NAME = "switchboard.pid"

# Windows ctypes prelude — only loaded on Windows so this module imports cleanly
# on Linux/macOS in CI.
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259

    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    ]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL


def _process_create_time_windows(pid: int) -> Optional[float]:
    """Return Unix-epoch seconds for the given PID's creation, or None if
    the process doesn't exist / can't be queried."""
    h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        creation = _FILETIME()
        exit_t = _FILETIME()
        kernel_t = _FILETIME()
        user_t = _FILETIME()
        ok = _kernel32.GetProcessTimes(
            h, ctypes.byref(creation), ctypes.byref(exit_t),
            ctypes.byref(kernel_t), ctypes.byref(user_t),
        )
        if not ok:
            return None
        # FILETIME is 100-ns intervals since 1601-01-01.
        # Unix epoch is 1970-01-01. Constant offset = 11644473600 seconds.
        as_int = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        unix_seconds = (as_int / 10_000_000.0) - 11_644_473_600.0
        return unix_seconds
    finally:
        _kernel32.CloseHandle(h)


def _process_create_time_linux(pid: int) -> Optional[float]:
    """Read /proc/<pid>/stat field 22 (starttime, in clock ticks since boot)
    and convert to Unix epoch. Returns None on failure."""
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            data = f.read()
        # Field 2 is comm in parens; skip past it to avoid space-in-name issues.
        rparen = data.rfind(")")
        if rparen < 0:
            return None
        fields = data[rparen + 2 :].split()
        # After (comm), field 3 was 'state'; remaining fields are 0-indexed
        # starting at state. starttime is field 22 in the original ps(1)
        # numbering, i.e. fields[19] in this slice.
        starttime_ticks = int(fields[19])
        clock_hz = os.sysconf("SC_CLK_TCK")
        with open("/proc/stat", "r") as f:
            for line in f:
                if line.startswith("btime "):
                    btime = int(line.split()[1])
                    return btime + (starttime_ticks / clock_hz)
        return None
    except (FileNotFoundError, PermissionError, ValueError, IndexError, OSError):
        return None


def _pid_alive_and_create_time(pid: int) -> tuple[bool, Optional[float]]:
    """Return (alive, create_time_unix_seconds) for the given PID.
    create_time is None if the PID isn't alive or we can't read it."""
    if pid <= 0:
        return (False, None)
    if sys.platform == "win32":
        ct = _process_create_time_windows(pid)
        if ct is None:
            return (False, None)
        # GetProcessTimes succeeded → process exists. But it might be a zombie
        # awaiting cleanup — verify with GetExitCodeProcess.
        h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return (False, None)
        try:
            exit_code = wintypes.DWORD()
            ok = _kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
            if not ok:
                return (False, None)
            return (exit_code.value == _STILL_ACTIVE, ct)
        finally:
            _kernel32.CloseHandle(h)
    else:
        # POSIX
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return (False, None)
        except PermissionError:
            # Process exists but we can't signal it (different uid). Treat as
            # alive — being conservative is the right side to err on for a
            # single-user local app.
            ct = _process_create_time_linux(pid) if platform.system() == "Linux" else None
            return (True, ct)
        except OSError:
            return (False, None)
        ct = _process_create_time_linux(pid) if platform.system() == "Linux" else None
        return (True, ct)


def _my_create_time() -> Optional[float]:
    return _pid_alive_and_create_time(os.getpid())[1]


class PidLockBusy(RuntimeError):
    """Raised when the lockfile is held by a *live* AI Conclave Switchboard process."""


def acquire(data_dir: Path) -> Path:
    """Acquire the lockfile, or raise PidLockBusy if another live instance holds it.

    Returns the lockfile path. Caller is responsible for calling release().
    Stale lockfiles (PID dead, or PID alive but with a different creation
    time than recorded — i.e. PID was reused for a different process) are
    silently overwritten.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / _LOCKFILE_NAME

    if lock_path.exists():
        try:
            content = lock_path.read_text(encoding="utf-8").strip()
            parts = content.split()
            stale_pid = int(parts[0])
            recorded_ct = float(parts[1]) if len(parts) >= 2 else None
        except (ValueError, OSError) as e:
            logger.warning("Unreadable lockfile %s (%s); overwriting.", lock_path, e)
            stale_pid, recorded_ct = -1, None

        alive, actual_ct = _pid_alive_and_create_time(stale_pid)
        if alive and recorded_ct is not None and actual_ct is not None:
            # PID reuse defense: only refuse to start if BOTH the PID is alive
            # AND the creation time matches what we recorded. A mismatch means
            # the OS handed this PID to a different process after the AI Conclave
            # Switchboard exited — the lockfile is stale.
            if abs(actual_ct - recorded_ct) < 2.0:  # 2-second tolerance
                raise PidLockBusy(
                    f"The AI Conclave Switchboard is already running as PID {stale_pid} "
                    f"(started at {datetime.fromtimestamp(actual_ct).isoformat()}).\n"
                    f"Stop it first (Ctrl+C in its terminal), or if you're sure "
                    f"it's a zombie, delete {lock_path} and try again."
                )
            logger.info(
                "Lockfile PID %d is alive but creation-time mismatches (recorded=%s actual=%s); "
                "assuming PID reuse, overwriting.",
                stale_pid, recorded_ct, actual_ct,
            )
        elif alive and recorded_ct is None:
            # Old lockfile format without creation time. Be conservative.
            raise PidLockBusy(
                f"The AI Conclave Switchboard appears to be running as PID {stale_pid} (lockfile lacks "
                f"creation timestamp — can't verify). Stop it or delete {lock_path} to proceed."
            )
        else:
            logger.info("Removing stale lockfile from PID %d (no longer alive).", stale_pid)

    my_pid = os.getpid()
    my_ct = _my_create_time()
    line = f"{my_pid} {my_ct if my_ct is not None else ''}\n"
    lock_path.write_text(line, encoding="utf-8")
    logger.info("Acquired pidlock at %s (PID %d).", lock_path, my_pid)
    return lock_path


def release(lock_path: Path) -> None:
    """Remove our lockfile. Idempotent. Safe to call from a `finally`."""
    try:
        if not lock_path.exists():
            return
        # Verify the lockfile is ours before deleting (paranoid: another
        # instance shouldn't have stomped on it, but if it did, don't delete
        # someone else's).
        try:
            content = lock_path.read_text(encoding="utf-8").strip()
            recorded_pid = int(content.split()[0])
            if recorded_pid != os.getpid():
                logger.warning(
                    "Lockfile %s now holds a different PID (%d, ours is %d); "
                    "leaving it alone.", lock_path, recorded_pid, os.getpid(),
                )
                return
        except (ValueError, OSError):
            pass  # corrupt or unreadable — just try to remove it
        lock_path.unlink()
        logger.info("Released pidlock at %s.", lock_path)
    except OSError as e:
        logger.warning("Failed to release pidlock %s: %s", lock_path, e)
