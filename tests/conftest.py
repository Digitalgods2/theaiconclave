"""Test fixtures for the whole suite.

Per DR0016, the app's runtime state lives under `user_data_root()`, which
resolves via the `SWITCHBOARD_DATA_DIR` env var, dev-mode walk-up, or a
platform-specific user-data directory. The default resolver caches its
result for the lifetime of the process — convenient in production, hostile
in tests where every case wants a clean slate.

The fixtures here:

- pin `SWITCHBOARD_DATA_DIR` to a per-test `tmp_path` so each test gets an
  isolated root with no leakage between cases
- reset the `paths.py` and `config.py` caches around each test
- run automatically (autouse) so existing tests don't need to be updated
"""

from __future__ import annotations

import os

import pytest

from app import config as config_module
from app.utils import paths


@pytest.fixture(autouse=True)
def _isolated_user_data_root(tmp_path, monkeypatch):
    """Each test gets a fresh `user_data_root()` rooted at `tmp_path`.

    Autouse so tests that never touch paths directly still pick up a clean
    root and don't accidentally read or write to the developer's real data
    directory. Cleans up after the test by resetting the cache.
    """
    monkeypatch.setenv("SWITCHBOARD_DATA_DIR", str(tmp_path))
    paths.reset_cache()
    config_module.reset_cache()
    yield
    paths.reset_cache()
    config_module.reset_cache()
