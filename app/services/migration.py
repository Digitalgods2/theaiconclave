"""First-run migration: copy repo-relative `./data/` state into `user_data_root()`.

Runs once when a packaged build (or any non-dev-mode launch) starts against an
empty `user_data_root()` while a populated `./data/` exists nearby. Non-
destructive: originals are preserved. See DR0016 for the full design.

This module also provides `migrate_legacy_data_dir()` — a one-time relocation
of the packaged user-data directory from its pre-rebrand name ("AI Switchboard")
to the current name ("The AI Conclave"). Unlike the first-run migration above,
that is a whole-directory atomic rename: the DB, WAL/SHM, config, and every
subdirectory move together as one coherent unit, so no `VACUUM INTO` is needed.

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
- **Recoverable batch**: every destination artifact is staged before commit,
  then atomic-renamed under a crash-recovery journal. On any mid-migration
  failure, visible artifacts are rolled back; originals are untouched; the
  migration retries on next launch.
- **Idempotent**: subsequent launches see the destination DB exists and skip
  the whole block (no per-file re-check, which would be a footgun if the
  user intentionally deleted some sandbox).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from app.services import pidlock
from app.utils.paths import (
    is_dev_mode,
    legacy_platform_user_data_root,
    platform_user_data_root,
    user_data_root,
)

logger = logging.getLogger("switchboard.migration")

_OLD_DATA_DIR = Path("data")             # repo-relative source root
_OLD_DB_NAME = "switchboard.db"
_OLD_PID_NAME = "switchboard.pid"

# All artifacts are staged before commit. The manifest is also a crash
# recovery journal: a later launch can roll back a partially renamed batch
# before destination-DB existence is used as the idempotence signal.
_STAGING_DIR_NAME = ".switchboard-migration-staging"
_STAGING_MANIFEST_NAME = "ready.json"

# Subdirectory trees we migrate verbatim.
_SUBDIRS = ("sandboxes", "exports", "uploads", "artifacts")


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

    _recover_interrupted_batch(dst_root)

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
        f"\nThe AI Conclave Switchboard migrated runtime state from {src_root} to {dst_root}.\n"
        f"The originals at {src_root} are intact and can be deleted manually if no longer needed.\n"
    )
    print(msg, file=sys.stderr)
    return summary


def _check_no_live_old_instance(src_root: Path) -> None:
    """Refuse migration if `<src_root>/switchboard.pid` points to a live
    AI Conclave Switchboard process. Closes the cross-root race per DR0016.
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
            f"An AI Conclave Switchboard instance appears to be running against {src_root} "
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
    """Execute a fully staged, recoverable migration batch.

    All copies complete before any destination becomes visible. If a commit
    rename fails, artifacts already committed by this run are rolled back.
    The staging manifest provides the same rollback after a process crash.
    The source tree is never touched.
    """
    summary: dict = {"src": str(src_root), "dst": str(dst_root), "db_bytes": 0, "subdirs": {}}
    staging_root = dst_root / _STAGING_DIR_NAME
    artifact_names = ([_OLD_DB_NAME] if has_db else []) + list(subdirs)
    committed: list[str] = []

    try:
        conflicts = [name for name in artifact_names if (dst_root / name).exists()]
        if conflicts:
            raise MigrationBlocked(
                f"Migration destination already contains: {', '.join(conflicts)}. "
                "The source was left untouched; reconcile those paths and retry."
            )

        if staging_root.exists():
            shutil.rmtree(staging_root)
        staging_root.mkdir(parents=True)

        if has_db:
            staged_db = staging_root / _OLD_DB_NAME
            db_bytes = _vacuum_into(src_root / _OLD_DB_NAME, staged_db)
            summary["db_bytes"] = db_bytes

        for sub in subdirs:
            src_sub = src_root / sub
            staged_sub = staging_root / sub
            shutil.copytree(src_sub, staged_sub)
            file_count = sum(1 for item in staged_sub.rglob("*") if item.is_file())
            summary["subdirs"][sub] = file_count

        # This is written only after every copy succeeds. Its presence means
        # recovery may safely remove these exact destination paths: none of
        # them existed before this transaction (validated above).
        manifest = staging_root / _STAGING_MANIFEST_NAME
        manifest.write_text(json.dumps({"artifacts": artifact_names}), encoding="utf-8")

        for name in artifact_names:
            _commit_staged_artifact(staging_root, dst_root, name)
            committed.append(name)
            logger.info("migration: committed %s to %s", name, dst_root / name)

    except Exception:
        rollback_complete = _remove_committed_artifacts(dst_root, committed)
        if rollback_complete:
            _cleanup_path(staging_root)
        raise

    # Every artifact is now visible. Removing the journal is the commit point.
    # If interrupted, startup recovery recognizes an empty staged set as a
    # complete batch and only removes the leftover journal directory.
    _cleanup_path(staging_root)

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


def _commit_staged_artifact(staging_root: Path, dst_root: Path, name: str) -> None:
    """Atomically rename one fully staged artifact into the destination."""
    (staging_root / name).replace(dst_root / name)


def _remove_committed_artifacts(dst_root: Path, artifact_names: list[str]) -> bool:
    """Remove artifacts created by an incomplete migration transaction."""
    complete = True
    for name in reversed(artifact_names):
        target = dst_root / name
        try:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        except OSError as e:
            complete = False
            logger.warning("migration rollback: failed to remove %s: %s", target, e)
    return complete


def _cleanup_path(path: Path) -> None:
    """Best-effort removal of one staging path."""
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except OSError as e:
        logger.warning("migration cleanup: failed to remove %s: %s", path, e)


def _recover_interrupted_batch(dst_root: Path) -> None:
    """Roll back a journaled partial commit before idempotence checks.

    A staging directory without a manifest contains only invisible staged
    copies and can be discarded. With a manifest, some listed paths may have
    been renamed into place. If staged artifacts remain, commit was partial
    and those destinations are rolled back. If none remain, every rename
    completed and only journal cleanup was interrupted.
    """
    staging_root = dst_root / _STAGING_DIR_NAME
    if not staging_root.exists():
        return

    manifest = staging_root / _STAGING_MANIFEST_NAME
    if not manifest.is_file():
        _cleanup_path(staging_root)
        return

    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        artifacts = raw.get("artifacts", [])
        if not isinstance(artifacts, list) or not all(isinstance(v, str) for v in artifacts):
            raise ValueError("artifacts must be a list of strings")
        allowed = {_OLD_DB_NAME, *_SUBDIRS}
        if len(artifacts) != len(set(artifacts)) or not set(artifacts).issubset(allowed):
            raise ValueError("artifacts contains an unknown or duplicate name")
    except (OSError, ValueError, json.JSONDecodeError) as e:
        raise MigrationBlocked(
            f"Interrupted migration journal {manifest} is unreadable ({e}). "
            "The source and destination were left untouched for manual review."
        ) from e

    staged_remaining = any((staging_root / name).exists() for name in artifacts)
    destinations_complete = all((dst_root / name).exists() for name in artifacts)
    if (staged_remaining or not destinations_complete) and not _remove_committed_artifacts(
        dst_root, artifacts
    ):
        raise MigrationBlocked(
            f"Could not roll back an interrupted migration under {dst_root}. "
            "Close programs using those files and retry."
        )

    _cleanup_path(staging_root)
    if staging_root.exists():
        raise MigrationBlocked(
            f"Could not clear interrupted migration staging at {staging_root}. "
            "Close programs using it and retry."
        )


def _dir_has_state(root: Path) -> bool:
    """True if `root` holds anything the rebrand migration should preserve."""
    if (root / _OLD_DB_NAME).is_file():
        return True
    if (root / "config.yaml").is_file():
        return True
    return any((root / s).is_dir() for s in _SUBDIRS)


def migrate_legacy_data_dir(
    *,
    old_root: Optional[Path] = None,
    new_root: Optional[Path] = None,
) -> Optional[dict]:
    """Relocate the packaged user-data directory from its pre-rebrand name
    (`AI Switchboard`) to the current name (`The AI Conclave`).

    A whole-directory atomic rename — the DB, its WAL/SHM, `config.yaml`, and
    every subdirectory move together as one coherent unit, so the `VACUUM INTO`
    dance that `maybe_migrate` needs for its file-by-file *copy* is unnecessary.

    Operates on the platform user-data roots directly and is independent of
    dev mode, so it behaves identically whether invoked from
    `tools/migrate-data-dir.py` or from a packaged build's startup.

    Returns a summary dict on success, or None when there is nothing to do
    (no legacy directory, an empty legacy directory, or already migrated).
    Raises `MigrationBlocked` when a live instance holds the legacy directory
    or the destination already holds state.
    """
    old_root = (old_root or legacy_platform_user_data_root()).resolve()
    new_root = (new_root or platform_user_data_root()).resolve()

    if old_root == new_root:
        # Same name on this platform (shouldn't happen) — nothing to do.
        return None
    if not old_root.is_dir():
        return None
    if not _dir_has_state(old_root):
        # Legacy directory exists but holds nothing worth moving.
        return None

    if new_root.exists():
        if new_root.is_dir() and not any(new_root.iterdir()):
            # Empty placeholder (e.g. created by an earlier user_data_root()
            # call) — safe to clear so the atomic rename can land on it.
            new_root.rmdir()
        else:
            raise MigrationBlocked(
                f"The rebranded data directory {new_root} already exists and is "
                f"not empty; migration will not overwrite it. Either remove it and "
                f"retry, or keep using it and delete the legacy {old_root} yourself."
            )

    _check_no_live_old_instance(old_root)

    new_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Atomic same-volume directory rename. old_root and new_root are
        # siblings under the platform data location, so this never crosses a
        # filesystem boundary in practice.
        os.replace(old_root, new_root)
    except OSError as e:
        raise MigrationBlocked(
            f"Could not rename {old_root} to {new_root}: {e}. The directories may "
            f"be on different volumes, or a file in the legacy directory is locked. "
            f"Close anything using it and retry."
        ) from e

    summary = {"src": str(old_root), "dst": str(new_root)}
    logger.info("rebrand migration: renamed %s -> %s", old_root, new_root)
    print(
        f"\nThe AI Conclave: relocated the user-data directory\n"
        f"  from {old_root}\n"
        f"  to   {new_root}\n",
        file=sys.stderr,
    )
    return summary
