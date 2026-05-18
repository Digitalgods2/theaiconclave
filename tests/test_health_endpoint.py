"""Tests for the /api/health endpoint shape (DR0017).

Covers:
- top-level `status` + `seats` keys
- seats array filters out internal adapters (fake)
- each seat entry has the required fields
- readiness cache is exercised
- per-seat reason / hint propagate from the adapter
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.base import BaseAdapter, Readiness, AdapterTestResult
from app.api import health as health_module
from app.database import init_database
from app.services import agent_registry


class _StubAdapter(BaseAdapter):
    """Minimal adapter that returns a canned Readiness."""

    def __init__(self, name: str, readiness_payload: Readiness, internal: bool = False) -> None:
        super().__init__()
        self.name = name
        self.internal = internal
        self._readiness = readiness_payload

    async def is_available(self) -> bool:
        return self._readiness.available

    async def readiness(self) -> Readiness:
        return self._readiness

    async def test_connection(self) -> AdapterTestResult:
        return AdapterTestResult(available=self._readiness.available, elapsed_ms=0)

    async def run_primary(self, ctx): raise NotImplementedError
    async def run_consultant(self, ctx): raise NotImplementedError
    async def run_final(self, ctx): raise NotImplementedError
    async def run_peer(self, ctx): raise NotImplementedError
    async def run_conclave_turn(self, ctx): raise NotImplementedError


@pytest.fixture
def health_client(tmp_path, monkeypatch):
    """Spin up a TestClient mounting the health router against a fresh registry.

    The autouse `_isolated_user_data_root` fixture (conftest.py) already pins
    SWITCHBOARD_DATA_DIR; we just need a DB and a stubbed registry.
    """
    init_database(str(tmp_path / "test.db"))
    agent_registry.clear()

    # Populate the registry with stubs covering every interesting case.
    agent_registry.register(_StubAdapter(
        name="alpha",
        readiness_payload=Readiness(available=True, reason="ok", hint=""),
    ))
    agent_registry.register(_StubAdapter(
        name="beta",
        readiness_payload=Readiness(
            available=False,
            reason="command_not_found",
            hint="Install Beta CLI.",
        ),
    ))
    agent_registry.register(_StubAdapter(
        name="hidden",
        readiness_payload=Readiness(available=True, reason="ok", hint=""),
        internal=True,
    ))

    # Bust the seats cache so each test starts from a clean computation.
    health_module._invalidate_seats_cache()

    app = FastAPI()
    app.include_router(health_module.router)
    return TestClient(app)


def test_health_returns_status_and_seats(health_client):
    r = health_client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert isinstance(body["seats"], list)


def test_health_seats_filter_out_internal_adapters(health_client):
    body = health_client.get("/api/health").json()
    names = [s["name"] for s in body["seats"]]
    assert "alpha" in names
    assert "beta" in names
    assert "hidden" not in names


def test_health_seat_entry_shape(health_client):
    body = health_client.get("/api/health").json()
    for seat in body["seats"]:
        assert set(seat.keys()) >= {"name", "kind", "available", "reason", "hint"}
        assert isinstance(seat["available"], bool)


def test_health_seat_propagates_reason_and_hint(health_client):
    body = health_client.get("/api/health").json()
    beta = next(s for s in body["seats"] if s["name"] == "beta")
    assert beta["available"] is False
    assert beta["reason"] == "command_not_found"
    assert beta["hint"] == "Install Beta CLI."


def test_health_seat_kind_classification(health_client):
    """Adapters without a `model_slug` attribute are classified as `cli`."""
    body = health_client.get("/api/health").json()
    alpha = next(s for s in body["seats"] if s["name"] == "alpha")
    assert alpha["kind"] == "cli"


def test_health_cache_short_circuits_second_call(health_client, monkeypatch):
    """The seats array is cached; a second call within TTL doesn't re-run readiness."""
    first = health_client.get("/api/health").json()["seats"]

    # Mutate the registry — without cache invalidation, the second call should
    # still see the old payload.
    agent_registry.register(_StubAdapter(
        name="gamma",
        readiness_payload=Readiness(available=True, reason="ok", hint=""),
    ))

    second = health_client.get("/api/health").json()["seats"]
    second_names = [s["name"] for s in second]
    first_names = [s["name"] for s in first]
    assert second_names == first_names
    assert "gamma" not in second_names


def test_health_cache_invalidation_refreshes(health_client):
    """After explicit cache invalidation, the seats payload reflects current registry."""
    health_client.get("/api/health")
    agent_registry.register(_StubAdapter(
        name="delta",
        readiness_payload=Readiness(available=True, reason="ok", hint=""),
    ))
    health_module._invalidate_seats_cache()

    body = health_client.get("/api/health").json()
    names = [s["name"] for s in body["seats"]]
    assert "delta" in names


def test_health_handles_adapter_readiness_exception(health_client):
    class _BoomAdapter(_StubAdapter):
        async def readiness(self):
            raise RuntimeError("simulated readiness explosion")

    agent_registry.register(_BoomAdapter(
        name="boom",
        readiness_payload=Readiness(available=False, reason="x", hint="x"),
    ))
    health_module._invalidate_seats_cache()

    body = health_client.get("/api/health").json()
    boom = next(s for s in body["seats"] if s["name"] == "boom")
    assert boom["available"] is False
    assert boom["reason"] == "readiness_check_failed"
    assert "simulated readiness explosion" in boom["hint"]
