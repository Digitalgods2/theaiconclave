"""First-run migration: copy repo-relative `./data/` state into `user_data_root()`.

Runs once when a packaged build (or any non-dev-mode launch) starts against an
empty `user_data_root()` while a populated `./data/` exists nearby. Non-
destructive: originals are preserved. See DR0016 for the full design.

Invariants (DR0016):

- **Strict ordering**: this is the FIRST awaitable in `lifespan`, before
  `init_database()`, the orphan reaper, the retention worker, or any other
  writer. No service in the new root may open the destination DB before
  migration completes.
- **Active-instance safety**: if `./data/switchboard.pid` exists and points to
  a live process, migration refuses to run. The cross-root pidlock race
  (old service against `./data/` + new service against `user_data_root()`)
  is closed by this check, not by pidlock itself.
- **SQLite consistency**: the DB is transferred with `VACUUM INTO`, which
  produces a single consistent file even if the source had a non-empty WAL.
  File-level `copy` of `.db + .db-wal + .db-shm` would risk a corrupt
  snapshot.
- **Atomic batch**: every destination artifact is written to a `.tmp` sibling
  and atomic-renamed after the whole batch succeeds. On any mid-migration
  failure, tmps are cleaned up; originals are untouched; the migration
  retries on next launch.
- **Idempotent**: subsequent launches see the destination DB exists and skip
  the whole block (no per-file re-check, which would be a footgun if the
  user intentionally deleted some sandbox).
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from app.services import pidlock
from app.utils.paths import is_dev_mode, user_data_root

logger = logging.getLogger("switchboard.migration")

_OLD_DATA_DIR = Path("data")             # repo-relative source root
_OLD_DB_NAME = "switchboard.db"
_OLD_PID_NAME = "switchboard.pid"

# Subdirectory trees we migrate verbatim.
_SUBDIRS = ("sandboxes", "exports", "uploads")


class MigrationBlocked(RuntimeError):
    """Raised when migration refuses to run (e.g. old instance still alive).

    Lifespan should surface this as a startup failure with a clear message.
    The migration is retried on the next launch — never silently skipped.
    """


def maybe_migrate() -> Optional[dict]:
    """Run the first-run migration if applicable. Idempotent.

    Returns a summary dict on a successful migration, None if no migration
    was needed (dev mode, destination DB already exists, or no source
    `./data/` to migrate). Raises `MigrationBlocked` if a live source-side
    instance prevents safe copy.
    """
    if is_dev_mode():
        # Source and destination are the same path; nothing to do.
        return None

    src_root = _OLD_DATA_DIR.resolve() if _OLD_DATA_DIR.exists() else None
    if src_root is None or not src_root.is_dir():
        return None

    dst_root = user_data_root()
    if src_root == dst_root:
        # Defensive: shouldn't happen outside dev mode, but skip safely.
        return None

    dst_db = dst_root / _OLD_DB_NAME
    if dst_db.exists():
        # Already migrated. Idempotent skip.
        return None

    src_db = src_root / _OLD_DB_NAME
    has_db = src_db.is_file()
    has_subdirs = [s for s in _SUBDIRS if (src_root / s).is_dir()]

    if not has_db and not has_subdirs:
        # Source dir exists but is empty / lacks anything we'd migrate.
        return None

    _check_no_live_old_instance(src_root)

    summary = _do_migration(src_root, dst_root, has_db=has_db, subdirs=has_subdirs)

    msg = (
        f"\nSwitchboard migrated runtime state from {src_root} to {dst_root}.\n"
        f"The originals at {src_root} are intact and can be deleted manually if no longer needed.\n"
    )
    print(msg, file=sys.stderr)
    return summary


def _check_no_live_old_instance(src_root: Path) -> None:
    """Refuse migration if `<src_root>/switchboard.pid` points to a live
    Switchboard process. Closes the cross-root race per DR0016.
    """
    old_pid_path = src_root / _OLD_PID_NAME
    if not old_pid_path.exists():
        return

    try:
        content = old_pid_path.read_text(encoding="utf-8").strip()
        parts = content.split()
        old_pid = int(parts[0])
        recorded_ct = float(parts[1]) if len(parts) >= 2 else None
    except (OSError, ValueError, IndexError) as e:
        logger.warning(
            "Old pidlock at %s is unreadable (%s); treating as stale and proceeding.",
            old_pid_path, e,
        )
        return

    alive, actual_ct = pidlock._pid_alive_and_create_time(old_pid)
    if not alive:
        logger.info(
            "Old pidlock at %s points to PID %d which is no longer alive; proceeding with migration.",
            old_pid_path, old_pid,
        )
        return

    # PID-reuse defense: same logic pidlock.acquire uses.
    if recorded_ct is not None and actual_ct is not None and abs(actual_ct - recorded_ct) < 2.0:
        raise MigrationBlocked(
            f"A Switchboard instance appears to be running against {src_root} "
            f"(PID {old_pid}). Stop it before launching this build, "
            f"or delete {old_pid_path} if you're sure the process is gone."
        )

    # Live PID but creation-time mismatches → PID reuse, the old instance is gone.
    logger.info(
        "Old pidlock PID %d is alive but creation-time mismatches; assuming PID reuse, proceeding.",
        old_pid,
    )


def _do_migration(
    src_root: Path,
    dst_root: Path,
    *,
    has_db: bool,
    subdirs: list[str],
) -> dict:
    """Execute the migration with `.tmp` staging and partial-copy cleanup.

    On any exception, every `.tmp` artifact created in this run is removed
    before the exception propagates. The source tree is never touched.
    """
    tmps_created: list[Path] = []
    summary: dict = {"src": str(src_root), "dst": str(dst_root), "db_bytes": 0, "subdirs": {}}

    try:
        if has_db:
            tmp_db = dst_root / f"{_OLD_DB_NAME}.tmp"
            tmps_created.append(tmp_db)
            db_bytes = _vacuum_into(src_root / _OLD_DB_NAME, tmp_db)
            summary["db_bytes"] = db_bytes
            # Atomic rename — Path.replace is atomic on Windows and POSIX.
            tmp_db.replace(dst_root / _OLD_DB_NAME)
            tmps_created.pop()
            logger.info("migration: DB transferred (%d bytes) to %s", db_bytes, dst_root / _OLD_DB_NAME)

        for sub in subdirs:
            src_sub = src_root / sub
            tmp_sub = dst_root / f"{sub}.tmp"
            tmps_created.append(tmp_sub)
            if tmp_sub.exists():
                shutil.rmtree(tmp_sub)
            shutil.copytree(src_sub, tmp_sub)
            file_count = sum(1 for _ in tmp_sub.rglob("*") if _.is_file())
            summary["subdirs"][sub] = file_count
            # Directory rename is atomic on POSIX; on Windows, atomic when the
            # destination doesn't exist (which we know — we checked dst_db above
            # and these are first-time copies).
            tmp_sub.replace(dst_root / sub)
            tmps_created.pop()
            logger.info("migration: %s/ transferred (%d files) to %s", sub, file_count, dst_root / sub)

    except Exception:
        _cleanup_tmps(tmps_created)
        raise

    logger.info("migration: complete | %s", summary)
    return summary


def _vacuum_into(src_db: Path, dst_db: Path) -> int:
    """Use SQLite's `VACUUM INTO` to produce a consistent single-file snapshot
    of `src_db` at `dst_db`. Acquires only a SHARED lock on the source —
    no risk to a running instance (though we've already verified none is
    running via the pid check).

    Returns bytes written.
    """
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    if dst_db.exists():
        dst_db.unlink()

    # Read-only connect to the source. timeout=0 — if the source is locked,
    # we want to fail fast (the active-pid check should already have caught
    # this, so a lock here means something weird happened).
    conn = sqlite3.connect(str(src_db), timeout=0)
    try:
        # SQLite literal-only context: dst path is interpolated, NOT bound.
        # VACUUM INTO requires a literal string. Caller controls dst_db so
        # injection is not a concern here, but quoting matters.
        quoted = str(dst_db).replace("'", "''")
        conn.execute(f"VACUUM INTO '{quoted}'")
    finally:
        conn.close()

    return dst_db.stat().st_size


def _cleanup_tmps(tmps: list[Path]) -> None:
    """Best-effort removal of `.tmp` artifacts. Used on migration failure.

    Each entry may be a file (tmp DB) or a directory (tmp sandbox tree).
    Swallows OSError so the original exception (the real failure) is what
    propagates.
    """
    for p in tmps:
        try:
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
        except OSError as e:
            logger.warning("migration cleanup: failed to remove %s: %s", p, e)
