"""Tests for the GET /api/tasks/{id}/download endpoint and the doc_export module.

Covers:
- pdf / docx / md / txt formats each produce a non-empty body with the right
  content-type and a Content-Disposition: attachment header + filename
- magic bytes (PDF: %PDF, DOCX: PK zip header)
- unsupported format -> 400
- nonexistent task -> 404
- doc_export.filename_stem produces a filesystem-safe stem
- non-terminal task can still be downloaded (unlike POST /export)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.database import init_database, connect, now_iso
from app.services import agent_registry, doc_export
from app.utils.ids import task_id as new_task_id, result_id, message_id


@pytest.fixture
def client(tmp_path: Path):
    db_path = tmp_path / "test.db"
    init_database(str(db_path))
    agent_registry.clear()
    agent_registry.init_registry()
    from app.api import tasks as tasks_module
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(tasks_module.router)
    return TestClient(app)


def _insert_task_with_content(status: str = "completed") -> str:
    tid = new_task_id()
    now = now_iso()
    perms = {"can_read_files": True, "can_write_files": False, "can_run_commands": False,
             "can_access_network": False, "can_install_packages": False,
             "can_apply_patches": False, "can_read_env_files": False, "can_read_secrets": False}
    limits = {"max_rounds": 5, "timeout_seconds": 180}
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
            (id, created_at, updated_at, status, source, source_agent, mode, task_type,
             user_request, primary_agent, consultants, project_path,
             context_json, permissions_json, limits_json)
            VALUES (?, ?, ?, ?, 'cli', 'claude-code', 'conclave', 'general_consultation',
                    'Is there a god?', NULL, '["codex","gemini","claude-code"]', NULL,
                    '{}', ?, ?)""",
            (tid, now, now, status, json.dumps(perms), json.dumps(limits)),
        )
        conn.execute(
            """INSERT INTO final_results
            (id, task_id, final_answer, agreement_level, created_at)
            VALUES (?, ?, ?, 'consensus', ?)""",
            (result_id(), tid, "No - the question is underdetermined by human writings.", now),
        )
        conn.execute(
            """INSERT INTO agent_messages
            (id, task_id, agent_name, role, message_type, direction, content, structured_json, created_at)
            VALUES (?, ?, 'codex', 'participant', 'conclave_turn', 'inbound', NULL, ?, ?)""",
            (message_id(), tid, json.dumps({
                "agent": "codex", "role": "participant", "message_type": "conclave_turn",
                "convergence": "i_am_done", "summary": "Agnosticism is the defensible answer.",
                "position": "No. We cannot determine whether there is a god.",
                "analysis": "Human writings underdetermine the conclusion.",
            }), now),
        )
    return tid


# ---------------------------------------------------------------------------
# Endpoint: format coverage
# ---------------------------------------------------------------------------

def test_download_pdf(client):
    tid = _insert_task_with_content()
    resp = client.get(f"/api/tasks/{tid}/download?format=pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment" in resp.headers["content-disposition"]
    assert ".pdf" in resp.headers["content-disposition"]
    assert resp.content[:4] == b"%PDF"
    assert len(resp.content) > 500


def test_download_docx(client):
    tid = _insert_task_with_content()
    resp = client.get(f"/api/tasks/{tid}/download?format=docx")
    assert resp.status_code == 200
    assert "wordprocessingml" in resp.headers["content-type"]
    assert ".docx" in resp.headers["content-disposition"]
    # DOCX is a zip archive -> starts with PK\x03\x04
    assert resp.content[:2] == b"PK"
    assert len(resp.content) > 500


def test_download_markdown(client):
    tid = _insert_task_with_content()
    resp = client.get(f"/api/tasks/{tid}/download?format=md")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert ".md" in resp.headers["content-disposition"]
    body = resp.content.decode("utf-8")
    assert f"# Task {tid}" in body
    assert "Is there a god?" in body


def test_download_text(client):
    tid = _insert_task_with_content()
    resp = client.get(f"/api/tasks/{tid}/download?format=txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert ".txt" in resp.headers["content-disposition"]
    assert b"Is there a god?" in resp.content


def test_download_default_is_pdf(client):
    tid = _insert_task_with_content()
    resp = client.get(f"/api/tasks/{tid}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"


# ---------------------------------------------------------------------------
# Endpoint: error paths
# ---------------------------------------------------------------------------

def test_download_unsupported_format(client):
    tid = _insert_task_with_content()
    resp = client.get(f"/api/tasks/{tid}/download?format=xls")
    assert resp.status_code == 400
    assert "unsupported format" in resp.json()["detail"]


def test_download_nonexistent_task(client):
    resp = client.get("/api/tasks/tsk_does_not_exist/download?format=pdf")
    assert resp.status_code == 404


def test_download_non_terminal_task_allowed(client):
    """Unlike POST /export (which requires a terminal task), download works on
    any task - it's a read-only snapshot."""
    tid = _insert_task_with_content(status="running")
    resp = client.get(f"/api/tasks/{tid}/download?format=pdf")
    assert resp.status_code == 200
    assert resp.content[:4] == b"%PDF"


# ---------------------------------------------------------------------------
# doc_export helpers
# ---------------------------------------------------------------------------

def test_filename_stem_is_filesystem_safe():
    task = {"id": "tsk_01ABC", "mode": "conclave", "user_request": "Is there a god? / really??"}
    stem = doc_export.filename_stem(task)
    assert stem.startswith("conclave-")
    assert stem.endswith("tsk_01ABC")
    # No characters that would be illegal in a Windows filename
    for ch in '<>:"/\\|?*':
        assert ch not in stem


def test_filename_stem_handles_empty_question():
    task = {"id": "tsk_01XYZ", "mode": "resolve", "user_request": ""}
    stem = doc_export.filename_stem(task)
    assert stem == "resolve-tsk_01XYZ"


def test_render_pdf_returns_bytes_without_final_result():
    """A task with no final_result and no messages should still render a PDF."""
    task = {"id": "tsk_0", "mode": "conclave", "status": "failed", "user_request": "Q",
            "consultants": ["codex"], "primary_agent": None}
    data = doc_export.render_pdf(task, [], None, [])
    assert data[:4] == b"%PDF"


def test_render_docx_returns_bytes_without_final_result():
    task = {"id": "tsk_0", "mode": "conclave", "status": "failed", "user_request": "Q",
            "consultants": ["codex"], "primary_agent": None}
    data = doc_export.render_docx(task, [], None, [])
    assert data[:2] == b"PK"
