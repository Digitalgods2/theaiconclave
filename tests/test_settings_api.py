"""Tests for the settings API (API-key store) and the env-over-DB precedence rule.

Covers:
- GET /api/settings/api-keys reports set/source/masked
- POST stores a key in the DB; GET then reports source=db with a masked tail
- GET /reveal returns the plaintext of a DB-stored key
- POST with empty value clears the stored key
- env var takes precedence: source=env, masked = env's tail, /reveal refuses
- OpenRouterAdapter._api_key() falls back to the DB when the env var is unset
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import init_database
from app.services import settings_store


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    init_database(str(tmp_path / "test.db"))
    from app.api import settings as settings_module
    app = FastAPI()
    app.include_router(settings_module.router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# DB-backed flow
# ---------------------------------------------------------------------------

def test_get_when_nothing_set(client):
    resp = client.get("/api/settings/api-keys")
    assert resp.status_code == 200
    o = resp.json()["openrouter"]
    assert o == {"set": False, "source": "none", "masked": None}


def test_set_then_get_reports_db_source_and_mask(client):
    resp = client.post("/api/settings/api-keys/openrouter", json={"value": "sk-or-abcdef1234WXYZ"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["action"] == "saved"
    assert body["status"]["source"] == "db"

    g = client.get("/api/settings/api-keys").json()["openrouter"]
    assert g["set"] is True
    assert g["source"] == "db"
    # masked = bullets + last 4 chars
    assert g["masked"].endswith("WXYZ")
    assert "•" in g["masked"]
    assert "sk-or-abc" not in g["masked"]


def test_reveal_returns_plaintext_of_db_key(client):
    client.post("/api/settings/api-keys/openrouter", json={"value": "sk-or-secret-value-123"})
    r = client.get("/api/settings/api-keys/openrouter/reveal").json()
    assert r["value"] == "sk-or-secret-value-123"
    assert r["source"] == "db"


def test_post_empty_clears_stored_key(client):
    client.post("/api/settings/api-keys/openrouter", json={"value": "sk-or-something"})
    assert client.get("/api/settings/api-keys").json()["openrouter"]["source"] == "db"
    cleared = client.post("/api/settings/api-keys/openrouter", json={"value": ""})
    assert cleared.json()["action"] == "cleared"
    assert client.get("/api/settings/api-keys").json()["openrouter"]["source"] == "none"
    # reveal after clear: no value
    assert client.get("/api/settings/api-keys/openrouter/reveal").json()["value"] is None


def test_post_null_clears_stored_key(client):
    client.post("/api/settings/api-keys/openrouter", json={"value": "sk-or-x"})
    client.post("/api/settings/api-keys/openrouter", json={"value": None})
    assert client.get("/api/settings/api-keys").json()["openrouter"]["source"] == "none"


def test_unknown_api_key_name(client):
    resp = client.post("/api/settings/api-keys/notarealkey", json={"value": "x"})
    assert resp.json()["ok"] is False


# ---------------------------------------------------------------------------
# env-over-DB precedence
# ---------------------------------------------------------------------------

def test_env_var_takes_precedence_over_db(client, monkeypatch):
    # Store a DB key, then set an env var — the env one should win in GET.
    client.post("/api/settings/api-keys/openrouter", json={"value": "sk-or-DBKEYvalue"})
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-ENVKEYvalue")
    o = client.get("/api/settings/api-keys").json()["openrouter"]
    assert o["source"] == "env"
    assert o["masked"].endswith("alue")  # env key's tail, not the DB key's


def test_reveal_refuses_when_env_sourced(client, monkeypatch):
    client.post("/api/settings/api-keys/openrouter", json={"value": "sk-or-DBKEYvalue"})
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-ENVKEYvalue")
    r = client.get("/api/settings/api-keys/openrouter/reveal").json()
    assert r["value"] is None
    assert r["source"] == "env"
    assert "environment variable" in r["note"]


# ---------------------------------------------------------------------------
# Adapter fallback: env var, else DB
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_openrouter_adapter_falls_back_to_db_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    init_database(str(tmp_path / "test.db"))
    settings_store.set_secret("openrouter_api_key", "sk-or-db-fallback")
    from app.agents.openrouter_adapter import OpenRouterAdapter
    a = OpenRouterAdapter(name="deepseek", model_slug="deepseek/deepseek-chat")
    assert a._api_key() == "sk-or-db-fallback"
    assert await a.is_available() is True
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env-wins")
    assert a._api_key() == "sk-or-env-wins"


# ---------------------------------------------------------------------------
# settings_store graceful-when-no-DB
# ---------------------------------------------------------------------------

def test_get_secret_returns_none_when_db_uninitialised(monkeypatch):
    # Point the module's db path at None to simulate "never initialised".
    import app.database as db
    monkeypatch.setattr(db, "_db_path", None)
    assert settings_store.get_secret("anything") is None
