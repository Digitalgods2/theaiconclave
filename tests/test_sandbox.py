"""Tests for the project-sandbox feature.

Covers: copy + ignore patterns, permission gates, per-file and total caps,
idempotent reuse, cleanup, orphan sweep, manifest rendering.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.protocol.validators import Permissions
from app.services.sandbox import (
    SANDBOXES_ROOT,
    build_manifest,
    cleanup_sandbox,
    prepare_sandbox,
    sandbox_path_for,
    sweep_orphan_sandboxes,
)


def _make_default_perms(read_env=False, read_secrets=False) -> Permissions:
    return Permissions(
        can_read_files=True,
        can_write_files=False,
        can_run_commands=False,
        can_access_network=False,
        can_install_packages=False,
        can_apply_patches=False,
        can_read_env_files=read_env,
        can_read_secrets=read_secrets,
    )


def _populate_project(root: Path):
    """Create a small synthetic project tree with the kinds of files a
    real codebase would contain — sources, ignored dirs, secrets, etc."""
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("def main(): pass\n", encoding="utf-8")
    (root / "src" / "helpers.py").write_text("# helpers\n", encoding="utf-8")
    (root / "README.md").write_text("# Project\n", encoding="utf-8")
    (root / ".env").write_text("SECRET_KEY=abc123\n", encoding="utf-8")
    (root / "id_rsa").write_text("PRIVATE KEY\n", encoding="utf-8")
    (root / "credentials.json").write_text("{}", encoding="utf-8")
    # Ignored directories
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("ignored\n", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "main.cpython-313.pyc").write_bytes(b"\x00\x00")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "lib.js").write_text("ignored\n", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "lib.py").write_text("ignored\n", encoding="utf-8")
    # Binary-looking files that should be skipped by extension
    (root / "build.zip").write_bytes(b"PK\x03\x04")
    (root / "lib.so").write_bytes(b"\x7fELF")


@pytest.fixture
def src_project(tmp_path: Path):
    """A synthetic source project for copying."""
    src = tmp_path / "src_project"
    src.mkdir()
    _populate_project(src)
    yield src


@pytest.fixture
def task_id() -> str:
    return "tsk_TEST_SANDBOX"


@pytest.fixture(autouse=True)
def isolate_sandboxes_root(tmp_path, monkeypatch):
    """Redirect SANDBOXES_ROOT to a temp dir for each test so we don't touch real state."""
    test_root = tmp_path / "sandboxes"
    monkeypatch.setattr("app.services.sandbox.SANDBOXES_ROOT", test_root)
    yield test_root


# ---------------------------------------------------------------------------
# Basic copy: source files preserved, ignored dirs skipped, default perms
# skip env and secret files
# ---------------------------------------------------------------------------

def test_copy_preserves_source_skips_ignored(src_project, task_id):
    perms = _make_default_perms()
    sandbox = prepare_sandbox(src_project, task_id, perms)
    assert sandbox is not None
    # Source files copied
    assert (sandbox / "src" / "main.py").exists()
    assert (sandbox / "src" / "helpers.py").exists()
    assert (sandbox / "README.md").exists()
    # Standard ignored dirs not copied
    assert not (sandbox / ".git").exists()
    assert not (sandbox / "__pycache__").exists()
    assert not (sandbox / "node_modules").exists()
    assert not (sandbox / ".venv").exists()
    # Binary-extension files not copied
    assert not (sandbox / "build.zip").exists()
    assert not (sandbox / "lib.so").exists()


# ---------------------------------------------------------------------------
# Permission gates: .env requires can_read_env_files, secrets require can_read_secrets
# ---------------------------------------------------------------------------

def test_default_perms_skip_env_and_secrets(src_project, task_id):
    perms = _make_default_perms()  # both off
    sandbox = prepare_sandbox(src_project, task_id, perms)
    assert sandbox is not None
    assert not (sandbox / ".env").exists()
    assert not (sandbox / "id_rsa").exists()
    assert not (sandbox / "credentials.json").exists()


def test_env_permission_allows_env_file(src_project, task_id):
    perms = _make_default_perms(read_env=True)
    sandbox = prepare_sandbox(src_project, task_id, perms)
    assert sandbox is not None
    assert (sandbox / ".env").exists()
    # Secrets still off
    assert not (sandbox / "id_rsa").exists()
    assert not (sandbox / "credentials.json").exists()


def test_secrets_permission_allows_keys_and_credentials(src_project, task_id):
    perms = _make_default_perms(read_secrets=True)
    sandbox = prepare_sandbox(src_project, task_id, perms)
    assert sandbox is not None
    assert (sandbox / "id_rsa").exists()
    assert (sandbox / "credentials.json").exists()
    # .env still off (gated separately)
    assert not (sandbox / ".env").exists()


# ---------------------------------------------------------------------------
# Idempotent reuse: calling prepare_sandbox twice for same task_id is a no-op
# ---------------------------------------------------------------------------

def test_idempotent_reuse(src_project, task_id):
    perms = _make_default_perms()
    sandbox_a = prepare_sandbox(src_project, task_id, perms)
    assert sandbox_a is not None
    # Add a marker file inside the existing sandbox
    (sandbox_a / "marker.txt").write_text("kept", encoding="utf-8")
    # Second call should NOT recopy; marker should survive
    sandbox_b = prepare_sandbox(src_project, task_id, perms)
    assert sandbox_b == sandbox_a
    assert (sandbox_a / "marker.txt").exists()


# ---------------------------------------------------------------------------
# Cleanup removes sandbox
# ---------------------------------------------------------------------------

def test_cleanup_removes_sandbox(src_project, task_id):
    perms = _make_default_perms()
    sandbox = prepare_sandbox(src_project, task_id, perms)
    assert sandbox is not None
    assert sandbox.exists()
    removed = cleanup_sandbox(task_id)
    assert removed is True
    assert not sandbox.exists()
    # Second call returns False (already gone)
    assert cleanup_sandbox(task_id) is False


# ---------------------------------------------------------------------------
# Missing source returns None
# ---------------------------------------------------------------------------

def test_missing_source_returns_none(tmp_path, task_id):
    perms = _make_default_perms()
    sandbox = prepare_sandbox(tmp_path / "does-not-exist", task_id, perms)
    assert sandbox is None


# ---------------------------------------------------------------------------
# Manifest renders the file tree
# ---------------------------------------------------------------------------

def test_manifest_lists_copied_files(src_project, task_id):
    perms = _make_default_perms()
    sandbox = prepare_sandbox(src_project, task_id, perms)
    assert sandbox is not None
    manifest = build_manifest(sandbox)
    assert "src/main.py" in manifest
    assert "src/helpers.py" in manifest
    assert "README.md" in manifest
    # Skipped paths should not appear
    assert ".git" not in manifest
    assert "node_modules" not in manifest


# ---------------------------------------------------------------------------
# Orphan sweep: removes sandboxes whose task IDs aren't in the active set
# ---------------------------------------------------------------------------

def test_sweep_orphan_sandboxes(src_project, isolate_sandboxes_root):
    perms = _make_default_perms()
    s_active = prepare_sandbox(src_project, "tsk_ACTIVE", perms)
    s_orphan_a = prepare_sandbox(src_project, "tsk_ORPHAN_A", perms)
    s_orphan_b = prepare_sandbox(src_project, "tsk_ORPHAN_B", perms)
    assert s_active and s_orphan_a and s_orphan_b
    removed = sweep_orphan_sandboxes(active_task_ids={"tsk_ACTIVE"})
    assert removed == 2
    assert s_active.exists()
    assert not s_orphan_a.exists()
    assert not s_orphan_b.exists()
