"""Tests for `app/config.py` — DR0016 discovery order and first-run seeding."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import config as config_module
from app.utils import paths


def _write_yaml(path: Path, port: int) -> None:
    """Drop a minimal valid config that sets a recognizable port."""
    path.write_text(f"server:\n  port: {port}\n", encoding="utf-8")


def test_explicit_path_arg_wins(tmp_path):
    """load_config(path=...) overrides every other discovery rule."""
    cfg_file = tmp_path / "custom.yaml"
    _write_yaml(cfg_file, 9001)
    cfg = config_module.load_config(path=cfg_file)
    assert cfg.server.port == 9001


def test_env_override_wins_over_user_root(tmp_path, monkeypatch):
    """SWITCHBOARD_CONFIG, when set, beats the user_data_root config.yaml."""
    user_cfg = paths.user_config_path()
    _write_yaml(user_cfg, 8001)

    env_cfg = tmp_path / "from-env.yaml"
    _write_yaml(env_cfg, 9002)
    monkeypatch.setenv("SWITCHBOARD_CONFIG", str(env_cfg))

    cfg = config_module.load_config()
    assert cfg.server.port == 9002


def test_user_config_path_used_in_packaged_mode(tmp_path, monkeypatch):
    """In packaged mode (no dev anchor in cwd), <user_data_root>/config.yaml wins."""
    monkeypatch.delenv("SWITCHBOARD_CONFIG", raising=False)
    user_cfg = paths.user_config_path()
    _write_yaml(user_cfg, 8050)

    cfg = config_module.load_config()
    assert cfg.server.port == 8050


def test_first_run_seeds_user_config_from_packaged_example(tmp_path, monkeypatch):
    """When user_config_path() is missing and a packaged example is available,
    the example is copied in once and used."""
    monkeypatch.delenv("SWITCHBOARD_CONFIG", raising=False)
    # No user config yet.
    assert not paths.user_config_path().exists()

    # Place a fake "packaged" example next to cwd. Since we're under the
    # autouse fixture, cwd is the repo (the real config.example.yaml IS
    # available). Verify the seed copies it.
    real_example = Path("config.example.yaml")
    if not real_example.exists():
        pytest.skip("no config.example.yaml at repo root; skipping packaged-seed test")

    cfg = config_module.load_config()
    # The seed should have produced a copy at user_config_path()
    assert paths.user_config_path().exists()
    # And the loaded config should have come from the example file.
    assert cfg.protocol_version == "1.0"


def test_dev_mode_prefers_repo_config_yaml(tmp_path, monkeypatch):
    """When dev-mode is detected, ./config.yaml beats ./config.example.yaml."""
    monkeypatch.delenv("SWITCHBOARD_CONFIG", raising=False)
    # Set up a faux repo anchor under tmp_path.
    (tmp_path / "pyproject.toml").write_text("[tool.test]", encoding="utf-8")
    (tmp_path / "config.example.yaml").write_text("server:\n  port: 7000\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("server:\n  port: 7777\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # Re-resolve user_data_root inside the faux repo.
    monkeypatch.delenv("SWITCHBOARD_DATA_DIR", raising=False)
    paths.reset_cache()
    config_module.reset_cache()
    assert paths.is_dev_mode()

    cfg = config_module.load_config()
    assert cfg.server.port == 7777


def test_database_path_defaults_to_none(tmp_path):
    """DatabaseConfig.path is None by default (DR0016) so the caller can
    substitute default_db_path() at resolution time."""
    cfg = config_module.Config()
    assert cfg.database.path is None


def test_explicit_database_path_still_wins(tmp_path):
    """An explicit `database.path` in YAML overrides the None default."""
    cfg_file = tmp_path / "with-db.yaml"
    cfg_file.write_text("database:\n  path: /custom/db.sqlite\n", encoding="utf-8")
    cfg = config_module.load_config(path=cfg_file)
    assert cfg.database.path == "/custom/db.sqlite"


def test_get_config_is_cached(tmp_path, monkeypatch):
    """get_config() returns the same instance on repeated calls."""
    user_cfg = paths.user_config_path()
    _write_yaml(user_cfg, 8200)

    first = config_module.get_config()
    second = config_module.get_config()
    assert first is second


def test_get_config_reset_picks_up_changes(tmp_path, monkeypatch):
    """reset_cache() forces re-load on the next get_config() call."""
    user_cfg = paths.user_config_path()
    _write_yaml(user_cfg, 8300)
    first = config_module.get_config()
    assert first.server.port == 8300

    _write_yaml(user_cfg, 8301)
    config_module.reset_cache()
    second = config_module.get_config()
    assert second.server.port == 8301


def test_missing_env_path_falls_through(tmp_path, monkeypatch):
    """SWITCHBOARD_CONFIG pointing at a nonexistent file logs a warning and
    proceeds through the rest of the discovery chain."""
    monkeypatch.setenv("SWITCHBOARD_CONFIG", str(tmp_path / "ghost.yaml"))

    # No user config either — should fall through to Config() defaults.
    cfg = config_module.load_config()
    assert cfg.server.port == 8787  # built-in default
