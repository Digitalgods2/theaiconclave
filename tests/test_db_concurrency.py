"""Tests for SQLite concurrency hardening — busy_timeout and with_retry."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.database import _BUSY_TIMEOUT_MS, connect, init_database, with_retry


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_database(str(db_path))
        yield db_path


# ---------------------------------------------------------------------------
# Connection sets busy_timeout
# ---------------------------------------------------------------------------

def test_busy_timeout_is_set(temp_db):
    """Every connection should have busy_timeout set so SQLite block-waits
    on a write lock rather than returning busy immediately."""
    with connect() as conn:
        row = conn.execute("PRAGMA busy_timeout").fetchone()
    assert row[0] == _BUSY_TIMEOUT_MS


def test_wal_journal_mode(temp_db):
    """WAL mode permits concurrent reads during writes."""
    with connect() as conn:
        row = conn.execute("PRAGMA journal_mode").fetchone()
    assert row[0].lower() == "wal"


def test_foreign_keys_enabled(temp_db):
    """Foreign-key enforcement on every connection."""
    with connect() as conn:
        row = conn.execute("PRAGMA foreign_keys").fetchone()
    assert row[0] == 1


# ---------------------------------------------------------------------------
# with_retry: retries on 'database is locked' / 'database is busy'
# ---------------------------------------------------------------------------

def test_with_retry_succeeds_on_first_attempt(temp_db):
    """Happy path — callable succeeds, no retry."""
    calls = []
    def op():
        calls.append(1)
        return "ok"
    result = with_retry(op)
    assert result == "ok"
    assert len(calls) == 1


def test_with_retry_retries_on_locked(temp_db):
    """If the callable raises 'database is locked', retry up to max_attempts."""
    attempts = []
    def op():
        attempts.append(1)
        if len(attempts) < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"
    result = with_retry(op, max_attempts=5, base_delay=0.001)
    assert result == "ok"
    assert len(attempts) == 3


def test_with_retry_retries_on_busy(temp_db):
    """Same logic for 'database is busy'."""
    attempts = []
    def op():
        attempts.append(1)
        if len(attempts) < 2:
            raise sqlite3.OperationalError("database is busy")
        return "ok"
    result = with_retry(op, max_attempts=3, base_delay=0.001)
    assert result == "ok"
    assert len(attempts) == 2


def test_with_retry_does_not_retry_other_operational_errors(temp_db):
    """Errors other than locked/busy should NOT be retried — they're real bugs."""
    attempts = []
    def op():
        attempts.append(1)
        raise sqlite3.OperationalError("no such table: nonexistent")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        with_retry(op, max_attempts=5, base_delay=0.001)
    assert len(attempts) == 1  # no retries


def test_with_retry_does_not_retry_programming_errors(temp_db):
    """ProgrammingError (e.g., syntax) is a code bug, not contention."""
    attempts = []
    def op():
        attempts.append(1)
        raise sqlite3.ProgrammingError("you didn't bind enough params")
    with pytest.raises(sqlite3.ProgrammingError):
        with_retry(op, max_attempts=5, base_delay=0.001)
    assert len(attempts) == 1


def test_with_retry_gives_up_after_max_attempts(temp_db):
    """If the callable always fails with locked, eventually re-raise."""
    attempts = []
    def op():
        attempts.append(1)
        raise sqlite3.OperationalError("database is locked")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        with_retry(op, max_attempts=3, base_delay=0.001)
    assert len(attempts) == 3
