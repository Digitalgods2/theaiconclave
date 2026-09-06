"""Cross-boundary regressions for the repository improvement milestones."""
from __future__ import annotations

import hashlib
import io
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from docx import Document
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.api import metrics, projects, task_feedback, tasks
from app.database import connect, init_database, now_iso
from app.protocol.validators import ConclaveTurn
from app.services import agent_registry, orchestrator, public_http
from app.services.doc_export import render_docx, render_pdf
from app.services.evidence_views import evidence_manifest, evidence_views
from app.services.exporter import export_to_markdown
from app.services.orphan_reaper import reap_orphans
from app.services.retention import find_trimmable_tier2_tasks
from app.services.synthesis import ConclaveSynthesis
from app.services.task_metrics import compute_summary
from app.services.trajectory_exporter import export_trajectory
from tests.test_core_regressions import _request
from tests.test_synthesis import _task


@pytest.fixture
def client(tmp_path):
    init_database(tmp_path / "test.db")
    agent_registry.clear()
    agent_registry.init_registry()
    application = FastAPI()
    for router in (tasks.router, projects.router, task_feedback.router, metrics.router):
        application.include_router(router)
    with TestClient(application, raise_server_exceptions=False) as c:
        yield c
    agent_registry.clear()


def create_task(client, status="completed", **extra):
    request = _request(mode="resolve", primary_agent="fake", consultants=[], **extra)
    response = client.post("/api/tasks", json=request.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    tid = response.json()["task_id"]
    with connect() as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
    return tid


@pytest.mark.parametrize("body", [{"name": None}, {"description": None}, {"instructions": None},
                                  {"default_evidence_urls": None}, {"name": "   "}])
def test_project_invalid_patch_is_validation_error(client, body):
    pid = client.post("/api/projects", json={"name": "Project"}).json()["id"]
    assert client.patch(f"/api/projects/{pid}", json=body).status_code == 422
    assert client.get(f"/api/projects/{pid}").json()["project"]["name"] == "Project"


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_feedback_is_upsert_without_inference(client, status):
    tid = create_task(client, status)
    url = f"/api/tasks/{tid}/feedback"
    first = client.put(url, json={"decision_changed": True, "note": "private note"}).json()["feedback"]
    second = client.put(url, json={"decision_changed": False, "extra_review_worth_it": True}).json()["feedback"]
    assert first["created_at"] == second["created_at"]
    assert second["decision_changed"] is False
    detail = client.get(f"/api/tasks/{tid}").json()
    assert detail["feedback"] == second
    assert detail["task"]["status"] == status
    assert detail["compute_summary"]["agent_runs"] == 0
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_feedback").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
    assert "private note" not in client.get("/api/metrics").text


def test_feedback_rejects_active_missing_and_invalid_values(client):
    tid = create_task(client, "running")
    assert client.put(f"/api/tasks/{tid}/feedback", json={}).status_code == 409
    assert client.put("/api/tasks/missing/feedback", json={}).status_code == 404
    assert client.put(f"/api/tasks/{tid}/feedback", json={"decision_changed": "yes"}).status_code == 422
    assert client.put(f"/api/tasks/{tid}/feedback", json={"note": "x" * 4001}).status_code == 422


def test_metrics_empty_partial_and_mixed_outcomes(client):
    empty = client.get("/api/metrics").json()
    assert all(m["completion_rate"] is None for m in empty["modes"])
    tids = [create_task(client, status) for status in ("completed", "failed", "cancelled", "pending")]
    client.put(f"/api/tasks/{tids[0]}/feedback", json={"material_risk_found": True})
    with connect() as conn:
        for i, cost in enumerate((None, 0.0, 1.5)):
            conn.execute("""INSERT INTO agent_runs (id, task_id, agent_name, role, round_number,
                         started_at, status, input_tokens, output_tokens, cost_usd)
                         VALUES (?, ?, 'fake', 'primary', 1, ?, 'completed', ?, ?, ?)""",
                         (f"r{i}", tids[i], now_iso(), 10 if i else None, 20 if i else None, cost))
    result = client.get("/api/metrics").json()["modes"][0]
    assert result["completion_rate"] == pytest.approx(1 / 3)
    assert result["feedback_coverage"] == pytest.approx(1 / 3)
    assert result["runs_per_task"] == .75
    assert result["value_rate"] == 1
    assert result["compute_summary"]["known_cost_usd"] == 1.5
    assert result["compute_summary"]["cost_coverage"] == pytest.approx(2 / 3)
    assert compute_summary([{}])["known_cost_usd"] is None
    assert compute_summary([{"cost_usd": 0}])["known_cost_usd"] == 0


def test_restart_reaps_recent_claims_and_retry_preserves_parent(client):
    tid = create_task(client, "running")
    assert reap_orphans() == 0
    assert reap_orphans(previous_instance=True) == 1
    assert reap_orphans(previous_instance=True) == 0
    retry = client.post(f"/api/tasks/{tid}/retry")
    assert retry.status_code == 200, retry.text
    assert retry.json()["parent_task_id"] == tid
    assert retry.json()["task_id"] != tid
    assert client.get(f"/api/tasks/{tid}").json()["task"]["status"] == "failed"
    assert client.post(f"/api/tasks/{retry.json()['task_id']}/retry").status_code == 409


def test_explicit_empty_evidence_overrides_project_defaults(client):
    from app.services.evidence import save_snapshot
    pid = client.post("/api/projects", json={"name": "Sources"}).json()["id"]
    save_snapshot({"url": "https://example.com", "content_text": "source", "content_sha256": "a" * 64,
                   "quality": {}}, task_id=None, project_id=pid)
    tid = create_task(client, decision_project_id=pid, context={"extra": {"evidence_snapshot_ids": []}})
    assert client.get(f"/api/tasks/{tid}").json()["task"]["context"]["extra"]["evidence_snapshot_ids"] == []


def test_excerpts_report_exact_presented_bytes_and_omissions():
    snapshots = [{"id": str(i), "content_text": "é" * 20_000, "content_sha256": "a" * 64} for i in range(5)]
    views = evidence_views(snapshots)
    assert sum(v["presented_chars"] for v in views) == 60_000
    assert views[-1]["omitted"]
    assert views[0]["presented_sha256"] == hashlib.sha256(("é" * 15_000).encode()).hexdigest()
    assert all("presented_text" not in v for v in evidence_manifest(snapshots))


async def test_transport_connects_to_validated_ip_with_original_tls_name(monkeypatch):
    monkeypatch.setattr(public_http, "public_addresses", lambda *a: ["93.184.216.34"])
    transport = public_http.PublicAddressTransport()
    seen = []
    async def capture(request):
        seen.append(request)
        return httpx.Response(200, content=b"ok")
    await transport.transport.aclose()
    transport.transport = httpx.MockTransport(capture)
    async with httpx.AsyncClient(transport=transport) as c:
        assert (await c.get("https://example.com/source")).status_code == 200
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "example.com"
    assert seen[0].extensions["sni_hostname"] == "example.com"


async def test_redirect_revalidation_rejects_private_address(monkeypatch):
    def resolve(host, port):
        if host == "private.example":
            raise ValueError("non-public address")
        return ["93.184.216.34"]
    monkeypatch.setattr(public_http, "public_addresses", resolve)
    transport = public_http.PublicAddressTransport()
    await transport.transport.aclose()
    transport.transport = httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "https://private.example/"}))
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as c:
        with pytest.raises(ValueError, match="non-public"):
            await c.get("https://example.com/")


async def test_synthesis_cannot_erase_verbatim_dissent_from_results_and_exports(client, monkeypatch):
    tid = create_task(client)
    task = _task()
    turns = [ConclaveTurn(protocol_version="1.0", task_id=tid, agent=agent, role="participant",
                         message_type="conclave_turn", summary=position, analysis=position,
                         position=position, convergence="still_thinking")
             for agent, position in [("a", "Choose A"), ("b", "Choose B")]]
    monkeypatch.setattr(orchestrator, "_run_independent_synthesis", AsyncMock(return_value=ConclaveSynthesis(
        final_answer="Choose A", preserved_disagreements=["B scales better"])))
    result = await orchestrator._assemble_conclave_final(task, tid, turns, [], "completed")
    assert "Choose B" in result.final_answer
    assert any(d.primary_position == "B scales better" for d in result.disagreements)
    record = result.model_dump(mode="json")
    record["citations"] = [{"evidence_id": "evd_SOURCE", "url": "https://example.com", "content_sha256": "a" * 64}]
    result_payload = result.model_dump(mode="json")
    result_payload["citations"] = [{**record["citations"][0], "retrieved_at": now_iso()}]
    from app.protocol.validators import FinalResult
    orchestrator._save_final_result(tid, FinalResult.model_validate(result_payload))
    exported = json.loads(export_trajectory(tid).read_text(encoding="utf-8"))
    assert exported["final_result"]["citations"][0]["evidence_id"] == "evd_SOURCE"
    task_data = {"id": tid, "user_request": "Choose", "mode": "conclave"}
    md = export_to_markdown(task_data, [], record, [])
    assert "evd_SOURCE" in md and "Choose B" in md
    pdf = PdfReader(io.BytesIO(render_pdf(task_data, [], record, [])))
    assert "evd_SOURCE" in "".join(page.extract_text() for page in pdf.pages)
    doc = Document(io.BytesIO(render_docx(task_data, [], record, [])))
    assert "evd_SOURCE" in "\n".join(p.text for p in doc.paragraphs)


def test_retention_refuses_missing_archive(client):
    tid = create_task(client)
    with connect() as conn:
        conn.execute("UPDATE tasks SET created_at='2000-01-01', exported_at=?, export_path=? WHERE id=?",
                     (now_iso(), "missing-archive.md", tid))
        conn.execute("INSERT INTO final_results(id,task_id,final_answer,agreement_level,created_at) "
                     "VALUES ('fr',?,'keep me','consensus',?)", (tid, now_iso()))
    assert tid not in find_trimmable_tier2_tasks(90)


def test_retention_stops_after_enough_pages_are_reclaimed(client, monkeypatch):
    from app.services import retention
    monkeypatch.setattr(retention, "db_size_bytes", lambda path: 1000)
    monkeypatch.setattr(retention, "completed_task_count", lambda: 0)
    monkeypatch.setattr(retention, "find_trimmable_tasks", lambda age: ["first", "second", "third"])
    sizes = iter([1000, 100, 100])
    monkeypatch.setattr(retention, "retained_db_bytes", lambda: next(sizes))
    removed = []
    monkeypatch.setattr(retention, "_delete_messages_for", lambda tid: removed.append(tid) or 1)
    monkeypatch.setattr(retention, "_vacuum", lambda: True)
    result = retention.trim_to_budget(max_db_size_bytes=500, max_task_count=100, min_age_days=90, db_path=None)
    assert removed == ["first"]
    assert result["vacuumed"]


def test_cli_auth_and_followup_context(monkeypatch):
    from clients import switchboard_conclave as cli
    monkeypatch.setenv("CONCLAVE_API_TOKEN", "fixture-token")
    captured = []
    def open_fixture(request, timeout):
        captured.append(request)
        return io.BytesIO(b'{"status":"ok"}')
    monkeypatch.setattr(cli.urllib.request, "urlopen", open_fixture)
    assert cli.health()
    assert captured[0].get_header("X-conclave-token") == "fixture-token"
    parent = _request(mode="resolve", primary_agent="fake", consultants=[]).model_dump(mode="json")
    parent.update({"id": "parent", "decision_project_id": "project", "project_path": "source",
                   "context": {"extra": {"evidence_snapshot_ids": ["evd"], "include_sandbox": True,
                                         "sandbox_path": "never-reuse-an-old-sandbox"}}})
    monkeypatch.setattr(cli, "get_task", lambda tid: {"task": parent})
    payloads = []
    monkeypatch.setattr(cli, "_post", lambda path, body: payloads.append(body) or {"task_id": "child"})
    assert cli.continue_thread("parent", "next question") == "child"
    assert payloads[0]["decision_project_id"] == "project"
    assert payloads[0]["context"]["extra"] == {"evidence_snapshot_ids": ["evd"], "include_sandbox": True}


def test_feedback_migration_rolls_back_on_failure(tmp_path, monkeypatch):
    from app import schema_migrations
    init_database(tmp_path / "migration.db")
    def fail_after_write(conn):
        conn.execute("CREATE TABLE must_rollback (value TEXT)")
        raise RuntimeError("fixture failure")
    monkeypatch.setattr(schema_migrations, "MIGRATIONS", (*schema_migrations.MIGRATIONS,
                                                          (5, "fixture_failure", fail_after_write)))
    with connect() as conn:
        with pytest.raises(RuntimeError, match="fixture failure"):
            schema_migrations.apply_migrations(conn)
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='must_rollback'").fetchone() is None
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 4


def test_token_middleware_rejects_missing_and_accepts_configured_token(client, monkeypatch):
    from app.main import require_api_token
    monkeypatch.setenv("CONCLAVE_API_TOKEN", "isolated-auth-fixture")
    application = FastAPI()
    application.middleware("http")(require_api_token)
    application.include_router(projects.router)
    with TestClient(application) as c:
        assert c.get("/api/projects").status_code == 401
        assert c.get("/api/projects", headers={"X-Conclave-Token": "wrong"}).status_code == 401
        assert c.get("/api/projects", headers={"X-Conclave-Token": "isolated-auth-fixture"}).status_code == 200


def test_invalid_citation_without_sources_is_audited():
    from types import SimpleNamespace
    citations, coverage = orchestrator._citation_bundle(_task(), [SimpleNamespace(citation_ids=["invented"])])
    assert citations == []
    assert coverage["invalid_citation_ids"] == ["invented"]


async def test_retention_shutdown_waits_for_database_writer(monkeypatch):
    import asyncio
    import threading
    from app.config import Config
    from app.services import retention
    started, release = threading.Event(), threading.Event()
    def slow_pass(**kwargs):
        started.set()
        release.wait(timeout=5)
        return {"ran": False}
    monkeypatch.setattr(retention, "trim_to_budget", slow_pass)
    worker = asyncio.create_task(retention.retention_loop(Config()))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        worker.cancel()
        await asyncio.sleep(.02)
        assert not worker.done()
    finally:
        release.set()
        await asyncio.wait_for(worker, timeout=3)
