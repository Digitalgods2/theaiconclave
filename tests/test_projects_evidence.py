"""Decision-project, evidence, migration, and network-boundary regressions."""

from __future__ import annotations

import socket

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import projects as projects_api
from app.config import Config, EvidenceConfig, validate_server_boundary
from app.database import connect, init_database
from app.services.evidence import EvidenceError, list_snapshots, save_snapshot, validate_public_url
from app.services import pidlock


@pytest.fixture
def db(tmp_path):
    init_database(tmp_path / "test.db")
    return tmp_path


def test_numbered_migrations_are_recorded_and_idempotent(db):
    with connect() as conn:
        first = [row["version"] for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )]
        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
    init_database(db / "test.db")
    with connect() as conn:
        second = [row["version"] for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )]
    assert first == second == [1, 2, 3, 4]
    assert {"decision_projects", "evidence_snapshots"} <= tables


def test_project_crud_and_archive(db):
    app = FastAPI()
    app.include_router(projects_api.router)
    with TestClient(app) as client:
        created = client.post("/api/projects", json={
            "name": "Release decision", "instructions": "Preserve dissent.",
            "default_evidence_urls": ["https://example.com/spec"],
        })
        assert created.status_code == 200
        project_id = created.json()["id"]
        project = client.get(f"/api/projects/{project_id}").json()["project"]
        assert project["instructions"] == "Preserve dissent."
        assert project["default_evidence_urls"] == ["https://example.com/spec"]
        archived = client.patch(f"/api/projects/{project_id}", json={"archived": True})
        assert archived.status_code == 200
        assert client.get("/api/projects").json()["projects"] == []


async def test_private_evidence_addresses_are_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ])
    with pytest.raises(EvidenceError, match="non-public"):
        await validate_public_url("https://localhost/internal", EvidenceConfig())


def test_evidence_snapshot_is_immutable_and_auditable(db):
    document = {
        "url": "https://example.com/spec", "title": "Specification",
        "publisher": "example.com", "content_text": "Frozen source text",
        "content_sha256": "a" * 64,
        "quality": {"score": 0.8}, "metadata": {"content_type": "text/plain"},
    }
    saved = save_snapshot(document, task_id=None, project_id=None)
    loaded = list_snapshots(ids=[saved["id"]])
    assert loaded[0]["content_text"] == "Frozen source text"
    assert loaded[0]["content_sha256"] == "a" * 64


@pytest.mark.parametrize("config", [
    Config.model_validate({"server": {"host": "0.0.0.0"}}),
    Config.model_validate({"server": {"host": "192.168.1.20", "allow_remote": True}}),
])
def test_remote_binding_requires_opt_in_and_token(config, monkeypatch):
    monkeypatch.delenv("CONCLAVE_API_TOKEN", raising=False)
    with pytest.raises(ValueError):
        validate_server_boundary(config)


def test_remote_binding_with_token_is_explicitly_allowed(monkeypatch):
    monkeypatch.setenv("CONCLAVE_API_TOKEN", "a-secure-token-at-least-16")
    config = Config.model_validate({
        "server": {"host": "0.0.0.0", "allow_remote": True},
    })
    validate_server_boundary(config)


def test_unknown_config_key_is_rejected():
    with pytest.raises(ValueError):
        Config.model_validate({"server": {"prot": 8787}})


def test_pidlock_acquisition_is_exclusive(tmp_path):
    lock_path = pidlock.acquire(tmp_path)
    try:
        with pytest.raises(pidlock.PidLockBusy):
            pidlock.acquire(tmp_path)
    finally:
        pidlock.release(lock_path)
