"""Tests for `app/utils/paths.py` — the user_data_root resolver."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.utils import paths


def test_env_override_wins(tmp_path, monkeypatch):
    """SWITCHBOARD_DATA_DIR is the top of the precedence chain."""
    monkeypatch.setenv("SWITCHBOARD_DATA_DIR", str(tmp_path))
    paths.reset_cache()
    assert paths.user_data_root() == tmp_path.resolve()
    assert paths.is_dev_mode() is False


def test_dev_mode_detected_from_repo_anchor(tmp_path, monkeypatch):
    """When cwd has pyproject.toml + config.example.yaml, dev mode wins
    (in the absence of SWITCHBOARD_DATA_DIR)."""
    monkeypatch.delenv("SWITCHBOARD_DATA_DIR", raising=False)
    (tmp_path / "pyproject.toml").write_text("[tool.test]", encoding="utf-8")
    (tmp_path / "config.example.yaml").write_text("protocol_version: '1.0'", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    paths.reset_cache()

    assert paths.user_data_root() == (tmp_path / "data").resolve()
    assert paths.is_dev_mode() is True


def test_dev_mode_walks_up_from_subdir(tmp_path, monkeypatch):
    """The walk-up search inspects cwd and every ancestor."""
    monkeypatch.delenv("SWITCHBOARD_DATA_DIR", raising=False)
    (tmp_path / "pyproject.toml").write_text("[tool.test]", encoding="utf-8")
    (tmp_path / "config.example.yaml").write_text("protocol_version: '1.0'", encoding="utf-8")
    nested = tmp_path / "sub" / "deeper"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    paths.reset_cache()

    assert paths.user_data_root() == (tmp_path / "data").resolve()
    assert paths.is_dev_mode() is True


def test_dev_mode_requires_both_files(tmp_path, monkeypatch):
    """`pyproject.toml` alone doesn't trigger dev mode — both files needed."""
    monkeypatch.delenv("SWITCHBOARD_DATA_DIR", raising=False)
    (tmp_path / "pyproject.toml").write_text("[tool.test]", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    paths.reset_cache()

    # No config.example.yaml — falls through to platform default.
    assert paths.user_data_root() != (tmp_path / "data").resolve()
    assert paths.is_dev_mode() is False


def test_platform_root_fallback(tmp_path, monkeypatch):
    """With no env var and no dev anchor, falls to a platform-specific path."""
    monkeypatch.delenv("SWITCHBOARD_DATA_DIR", raising=False)
    # Move to a directory that has neither pyproject.toml nor config.example.yaml.
    bare = tmp_path / "elsewhere"
    bare.mkdir()
    monkeypatch.chdir(bare)
    # Redirect platform-specific bases to tmp so we don't write to real dirs.
    if sys.platform == "win32":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    else:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    paths.reset_cache()

    root = paths.user_data_root()
    assert root.is_dir()
    assert "AI Switchboard" in str(root) or "ai-switchboard" in str(root)
    assert paths.is_dev_mode() is False


def test_root_is_cached(tmp_path, monkeypatch):
    """Second call returns the same value without re-resolving."""
    monkeypatch.setenv("SWITCHBOARD_DATA_DIR", str(tmp_path))
    paths.reset_cache()
    first = paths.user_data_root()

    # Change the env var — cached value should ignore it.
    monkeypatch.setenv("SWITCHBOARD_DATA_DIR", str(tmp_path / "different"))
    second = paths.user_data_root()
    assert first == second

    # After reset, the new value takes effect.
    paths.reset_cache()
    third = paths.user_data_root()
    assert third != first


def test_derived_helpers_under_root(tmp_path, monkeypatch):
    """All derived helpers resolve to subdirs of user_data_root()."""
    monkeypatch.setenv("SWITCHBOARD_DATA_DIR", str(tmp_path))
    paths.reset_cache()
    root = paths.user_data_root()

    assert paths.user_config_path() == root / "config.yaml"
    assert paths.default_db_path() == root / "switchboard.db"
    assert paths.sandboxes_root() == root / "sandboxes"
    assert paths.uploads_root() == root / "uploads"
    assert paths.exports_root() == root / "exports"
    assert paths.artifacts_root() == root / "artifacts"
    assert paths.logs_root() == root / "logs"

    # Subdir resolvers are lazy-create.
    assert paths.sandboxes_root().is_dir()
    assert paths.uploads_root().is_dir()
    assert paths.exports_root().is_dir()
    assert paths.artifacts_root().is_dir()
    assert paths.logs_root().is_dir()


def test_env_override_expands_user(tmp_path, monkeypatch):
    """SWITCHBOARD_DATA_DIR supports ~ expansion."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))   # Windows
    monkeypatch.setenv("SWITCHBOARD_DATA_DIR", "~/switchboard-test")
    paths.reset_cache()

    root = paths.user_data_root()
    assert "~" not in str(root)
    assert str(root).endswith("switchboard-test")
