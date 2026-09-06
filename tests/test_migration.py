"""Tests for `app/services/migration.py` — DR0016 first-run migration."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.services import migration
from app.utils import paths


def _seed_old_data(repo_root: Path) -> None:
    """Create a faux `./data/` source tree at `repo_root/data` with a DB,
    sandboxes, exports, and uploads."""
    old = repo_root / "data"
    old.mkdir()
    # Real SQLite DB (not just an empty file) so VACUUM INTO works.
    db = old / "switchboard.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE smoke (k TEXT, v TEXT)")
    conn.execute("INSERT INTO smoke VALUES ('hello', 'world')")
    conn.commit()
    conn.close()
    (old / "sandboxes" / "tsk_abc").mkdir(parents=True)
    (old / "sandboxes" / "tsk_abc" / "main.py").write_text("# sandbox file", encoding="utf-8")
    (old / "exports").mkdir()
    (old / "exports" / "tsk_xyz.md").write_text("# export", encoding="utf-8")
    (old / "uploads" / "fil_q").mkdir(parents=True)
    (old / "uploads" / "fil_q" / "upload.txt").write_text("upload", encoding="utf-8")


def test_migration_skipped_in_dev_mode(tmp_path, monkeypatch):
    """If we're in dev mode (cwd has pyproject.toml + config.example.yaml),
    migration is a no-op even when ./data/ exists."""
    monkeypatch.delenv("SWITCHBOARD_DATA_DIR", raising=False)
    (tmp_path / "pyproject.toml").write_text("[tool.test]", encoding="utf-8")
    (tmp_path / "config.example.yaml").write_text("protocol_version: '1.0'", encoding="utf-8")
    _seed_old_data(tmp_path)
    monkeypatch.chdir(tmp_path)
    paths.reset_cache()

    result = migration.maybe_migrate()
    assert result is None  # nothing migrated


def test_migration_skipped_when_no_source(tmp_path, monkeypatch):
    """If `./data/` doesn't exist, migration is a no-op."""
    monkeypatch.chdir(tmp_path)  # cwd has no `data/` subdir

    result = migration.maybe_migrate()
    assert result is None


def test_migration_copies_db_via_vacuum_into(tmp_path, monkeypatch):
    """The DB is transferred with VACUUM INTO — the resulting file is a
    standalone valid SQLite DB (no WAL/SHM dependency)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_old_data(repo)
    monkeypatch.chdir(repo)

    result = migration.maybe_migrate()
    assert result is not None
    assert result["db_bytes"] > 0

    new_db = paths.default_db_path()
    assert new_db.is_file()

    # Open it and read the seeded row.
    conn = sqlite3.connect(str(new_db))
    row = conn.execute("SELECT k, v FROM smoke").fetchone()
    conn.close()
    assert row == ("hello", "world")


def test_migration_copies_subdir_trees(tmp_path, monkeypatch):
    """Sandboxes, exports, uploads all transfer with contents intact."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_old_data(repo)
    monkeypatch.chdir(repo)

    migration.maybe_migrate()

    assert (paths.sandboxes_root() / "tsk_abc" / "main.py").read_text() == "# sandbox file"
    assert (paths.exports_root() / "tsk_xyz.md").read_text() == "# export"
    assert (paths.uploads_root() / "fil_q" / "upload.txt").read_text() == "upload"


def test_migration_does_not_delete_originals(tmp_path, monkeypatch):
    """Originals at `./data/` are preserved."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_old_data(repo)
    monkeypatch.chdir(repo)

    migration.maybe_migrate()

    assert (repo / "data" / "switchboard.db").exists()
    assert (repo / "data" / "sandboxes" / "tsk_abc" / "main.py").exists()
    assert (repo / "data" / "exports" / "tsk_xyz.md").exists()
    assert (repo / "data" / "uploads" / "fil_q" / "upload.txt").exists()


def test_migration_idempotent(tmp_path, monkeypatch):
    """Second call after a successful migration is a no-op."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_old_data(repo)
    monkeypatch.chdir(repo)

    first = migration.maybe_migrate()
    assert first is not None

    second = migration.maybe_migrate()
    assert second is None  # destination DB exists; skip the whole block


def test_migration_blocked_by_live_old_pidlock(tmp_path, monkeypatch):
    """If `./data/switchboard.pid` points to a live process, migration refuses."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_old_data(repo)
    # Drop a pidlock pointing at the current process.
    import os
    from app.services import pidlock as pidlock_mod
    my_pid = os.getpid()
    my_ct = pidlock_mod._my_create_time()
    (repo / "data" / "switchboard.pid").write_text(f"{my_pid} {my_ct}\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    with pytest.raises(migration.MigrationBlocked):
        migration.maybe_migrate()

    # Destination DB should NOT exist (nothing was copied).
    assert not paths.default_db_path().exists()


def test_migration_stale_pidlock_is_ignored(tmp_path, monkeypatch):
    """A pidlock pointing at a dead PID is treated as stale; migration proceeds."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_old_data(repo)
    # PID 0 is reserved / not a real process on every platform.
    (repo / "data" / "switchboard.pid").write_text("999999 0.0\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    result = migration.maybe_migrate()
    assert result is not None
    assert paths.default_db_path().exists()


def test_migration_old_pid_is_not_migrated(tmp_path, monkeypatch):
    """The old switchboard.pid is NOT copied into the new root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_old_data(repo)
    (repo / "data" / "switchboard.pid").write_text("999999 0.0\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    migration.maybe_migrate()
    assert not (paths.user_data_root() / "switchboard.pid").exists()


def test_migration_partial_source(tmp_path, monkeypatch):
    """Only-DB case (no sandboxes/exports/uploads) still works."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "data").mkdir()
    db = repo / "data" / "switchboard.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE smoke (k TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.chdir(repo)

    result = migration.maybe_migrate()
    assert result is not None
    assert paths.default_db_path().exists()
    assert result["subdirs"] == {}


def test_staging_failure_leaves_no_partial_destination_and_retries(tmp_path, monkeypatch):
    """A late copy failure must not commit the already-staged DB or subdirs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_old_data(repo)
    monkeypatch.chdir(repo)

    real_copytree = migration.shutil.copytree

    def fail_on_exports(src, dst, *args, **kwargs):
        if Path(src).name == "exports":
            raise OSError("injected copy failure")
        return real_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(migration.shutil, "copytree", fail_on_exports)
    with pytest.raises(OSError, match="injected copy failure"):
        migration.maybe_migrate()

    dst_root = paths.user_data_root()
    assert not (dst_root / "switchboard.db").exists()
    assert not (dst_root / "sandboxes").exists()
    assert not (dst_root / "exports").exists()
    assert not (dst_root / migration._STAGING_DIR_NAME).exists()

    # The source is intact and remains a complete retry source.
    monkeypatch.setattr(migration.shutil, "copytree", real_copytree)
    result = migration.maybe_migrate()
    assert result is not None
    assert (dst_root / "switchboard.db").exists()
    assert (dst_root / "sandboxes" / "tsk_abc" / "main.py").exists()
    assert (dst_root / "exports" / "tsk_xyz.md").exists()


def test_commit_failure_rolls_back_already_committed_artifacts(tmp_path, monkeypatch):
    """A rename failure after DB commit rolls the entire visible batch back."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_old_data(repo)
    monkeypatch.chdir(repo)

    real_commit = migration._commit_staged_artifact

    def fail_on_sandboxes(staging_root, dst_root, name):
        if name == "sandboxes":
            raise OSError("injected commit failure")
        return real_commit(staging_root, dst_root, name)

    monkeypatch.setattr(migration, "_commit_staged_artifact", fail_on_sandboxes)
    with pytest.raises(OSError, match="injected commit failure"):
        migration.maybe_migrate()

    dst_root = paths.user_data_root()
    assert not (dst_root / "switchboard.db").exists()
    assert not (dst_root / "sandboxes").exists()
    assert not (dst_root / migration._STAGING_DIR_NAME).exists()

    monkeypatch.setattr(migration, "_commit_staged_artifact", real_commit)
    assert migration.maybe_migrate() is not None
    assert (dst_root / "switchboard.db").exists()


def test_recovery_rolls_back_journaled_partial_commit(tmp_path, monkeypatch):
    """A process crash after the DB rename cannot make the next run skip."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_old_data(repo)
    monkeypatch.chdir(repo)

    dst_root = paths.user_data_root()
    staging = dst_root / migration._STAGING_DIR_NAME
    staging.mkdir()
    # Simulate: DB committed, sandboxes remained staged, then process died.
    (dst_root / "switchboard.db").write_bytes(b"partial")
    (staging / "sandboxes").mkdir()
    (staging / migration._STAGING_MANIFEST_NAME).write_text(
        '{"artifacts":["switchboard.db","sandboxes"]}', encoding="utf-8"
    )

    result = migration.maybe_migrate()
    assert result is not None
    with sqlite3.connect(str(dst_root / "switchboard.db")) as conn:
        assert conn.execute("SELECT v FROM smoke WHERE k='hello'").fetchone() == ("world",)
    assert not staging.exists()
