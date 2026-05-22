"""Tests for the rebrand directory migration (`migrate_legacy_data_dir`)."""

from __future__ import annotations

import pytest

from app.services.migration import MigrationBlocked, migrate_legacy_data_dir


def _populate(root):
    """Write a representative slice of packaged-install state into `root`."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "switchboard.db").write_bytes(b"fake-sqlite-bytes")
    (root / "config.yaml").write_text("server:\n  port: 8787\n", encoding="utf-8")
    sb = root / "sandboxes" / "tsk_demo"
    sb.mkdir(parents=True)
    (sb / "file.txt").write_text("hello", encoding="utf-8")
    (root / "exports").mkdir()


def test_no_legacy_dir_is_noop(tmp_path):
    old = tmp_path / "AI Switchboard"
    new = tmp_path / "The AI Conclave"
    assert migrate_legacy_data_dir(old_root=old, new_root=new) is None
    assert not new.exists()


def test_empty_legacy_dir_is_noop(tmp_path):
    old = tmp_path / "AI Switchboard"
    old.mkdir()
    new = tmp_path / "The AI Conclave"
    assert migrate_legacy_data_dir(old_root=old, new_root=new) is None


def test_renames_legacy_dir(tmp_path):
    old = tmp_path / "AI Switchboard"
    new = tmp_path / "The AI Conclave"
    _populate(old)

    summary = migrate_legacy_data_dir(old_root=old, new_root=new)

    assert summary is not None
    assert not old.exists()
    assert (new / "switchboard.db").read_bytes() == b"fake-sqlite-bytes"
    assert (new / "config.yaml").is_file()
    assert (new / "sandboxes" / "tsk_demo" / "file.txt").read_text() == "hello"


def test_empty_destination_is_cleared_then_renamed(tmp_path):
    old = tmp_path / "AI Switchboard"
    new = tmp_path / "The AI Conclave"
    _populate(old)
    new.mkdir()  # empty placeholder, e.g. created by an earlier user_data_root()

    summary = migrate_legacy_data_dir(old_root=old, new_root=new)

    assert summary is not None
    assert (new / "switchboard.db").is_file()
    assert not old.exists()


def test_nonempty_destination_blocks(tmp_path):
    old = tmp_path / "AI Switchboard"
    new = tmp_path / "The AI Conclave"
    _populate(old)
    new.mkdir()
    (new / "switchboard.db").write_bytes(b"existing-state")

    with pytest.raises(MigrationBlocked):
        migrate_legacy_data_dir(old_root=old, new_root=new)

    # The legacy directory is left untouched on a blocked migration.
    assert (old / "switchboard.db").read_bytes() == b"fake-sqlite-bytes"


def test_idempotent_after_migration(tmp_path):
    old = tmp_path / "AI Switchboard"
    new = tmp_path / "The AI Conclave"
    _populate(old)

    assert migrate_legacy_data_dir(old_root=old, new_root=new) is not None
    # Second run: the legacy directory is gone, so it is a no-op.
    assert migrate_legacy_data_dir(old_root=old, new_root=new) is None
