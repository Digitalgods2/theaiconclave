from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app.agents.fake_adapter import FakeAdapter
from app.database import connect, init_database, now_iso
from app.protocol.validators import (
    AgentRole,
    Agreement,
    ConsultantCritique,
    Limits,
    MessageType,
    Permissions,
    PrimaryResponse,
    RecommendedAction,
    Risk,
    RiskSeverity,
)
from app.services import agent_registry
from app.services.artifacts import apply_artifact_to_project, list_artifacts
from app.services.orchestrator import run_task
from app.utils.ids import message_id, task_id as new_task_id


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_database(str(db_path))
        agent_registry.clear()
        agent_registry.init_registry()
        yield db_path


def _permissions() -> Permissions:
    return Permissions(
        can_read_files=True,
        can_write_files=False,
        can_run_commands=False,
        can_access_network=False,
        can_install_packages=False,
        can_apply_patches=False,
        can_read_env_files=False,
        can_read_secrets=False,
    )


def _create_consult_task(primary: str, consultant: str, project_path: str | None = None) -> str:
    tid = new_task_id()
    now = now_iso()
    context = {"files": [], "error": None, "git_diff": None, "extra": {}}
    limits = Limits(max_rounds=3, timeout_seconds=30)
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
            (id, created_at, updated_at, status, source, source_agent, mode, task_type,
             user_request, primary_agent, consultants, project_path,
             context_json, permissions_json, limits_json)
            VALUES (?, ?, ?, 'pending', 'api', NULL, 'consult', 'general_consultation',
                    'Build a small page', ?, ?, ?, ?, ?, ?)""",
            (
                tid,
                now,
                now,
                primary,
                json.dumps([consultant]),
                project_path,
                json.dumps(context, sort_keys=True),
                json.dumps(_permissions().model_dump(), sort_keys=True),
                json.dumps(limits.model_dump(), sort_keys=True),
            ),
        )
    return tid


class ClarifyingPrimary(FakeAdapter):
    name = "clarifying-primary"

    async def run_primary(self, ctx):
        return PrimaryResponse(
            protocol_version="1.0",
            task_id=ctx.task_id,
            agent=self.name,
            role=AgentRole.PRIMARY,
            message_type=MessageType.PRIMARY_PROPOSAL,
            summary="Need one product detail.",
            analysis="The final answer depends on user intent.",
            recommended_actions=[],
            risks=[],
            confidence=0.5,
            user_input_question="What tone should the page use?",
        )

    async def run_final(self, ctx):
        assert any(m.get("message_type") == "user_input_response" for m in ctx.prior_messages)
        return PrimaryResponse(
            protocol_version="1.0",
            task_id=ctx.task_id,
            agent=self.name,
            role=AgentRole.PRIMARY,
            message_type=MessageType.PRIMARY_FINAL,
            summary="Final with clarification.",
            analysis="User clarified the missing details.",
            recommended_actions=[],
            risks=[],
            confidence=0.9,
        )


class QuestioningConsultant(FakeAdapter):
    name = "questioning-consultant"

    async def run_consultant(self, ctx):
        return ConsultantCritique(
            protocol_version="1.0",
            task_id=ctx.task_id,
            agent=self.name,
            role=AgentRole.CONSULTANT,
            message_type=MessageType.CONSULTANT_CRITIQUE,
            agreement=Agreement.PARTIAL,
            critique="Missing concrete implementation constraints.",
            missed_risks=[],
            suggested_questions=["Which browser size should be prioritized?"],
            confidence=0.7,
            wants_continuation=False,
        )


async def test_consult_collects_numbered_questions_and_resumes(temp_db):
    agent_registry.register(ClarifyingPrimary())
    agent_registry.register(QuestioningConsultant())
    tid = _create_consult_task("clarifying-primary", "questioning-consultant")

    await run_task(tid)

    with connect() as conn:
        task = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
        req = conn.execute(
            """SELECT content FROM agent_messages
               WHERE task_id = ? AND message_type = 'user_input_request'""",
            (tid,),
        ).fetchone()
        result = conn.execute("SELECT * FROM final_results WHERE task_id = ?", (tid,)).fetchone()
    assert task["status"] == "awaiting_user_input"
    assert result is None
    assert "1. What tone should the page use?" in req["content"]
    assert "2. Which browser size should be prioritized?" in req["content"]

    with connect() as conn:
        conn.execute(
            """INSERT INTO agent_messages
               (id, task_id, agent_run_id, agent_name, role, message_type,
                direction, content, structured_json, created_at)
               VALUES (?, ?, NULL, 'user', 'user', 'user_input_response',
                       'from_user', ?, NULL, ?)""",
            (message_id(), tid, "1. Crisp.\n2. Mobile first.", now_iso()),
        )
        conn.execute(
            "UPDATE tasks SET status = 'pending', updated_at = ? WHERE id = ?",
            (now_iso(), tid),
        )

    await run_task(tid)

    with connect() as conn:
        task = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
        result = conn.execute("SELECT * FROM final_results WHERE task_id = ?", (tid,)).fetchone()
    assert task["status"] == "completed"
    assert result is not None
    assert "Final with clarification" in result["final_answer"]


class ArtifactPrimary(FakeAdapter):
    name = "artifact-primary"

    async def run_final(self, ctx):
        return PrimaryResponse(
            protocol_version="1.0",
            task_id=ctx.task_id,
            agent=self.name,
            role=AgentRole.PRIMARY,
            message_type=MessageType.PRIMARY_FINAL,
            summary="Draft artifacts ready.",
            analysis="Produce one file and one search/replace edit.",
            recommended_actions=[
                RecommendedAction(
                    kind="create_file",
                    description="Create stylesheet draft.",
                    requires_approval=True,
                    payload={"path": "crisp.css", "content": "body { color: #111; }\n"},
                ),
                RecommendedAction(
                    kind="edit_file",
                    description="Link stylesheet.",
                    requires_approval=True,
                    payload={
                        "path": "index.html",
                        "search": "</head>",
                        "replace": '<link rel="stylesheet" href="crisp.css">\n</head>',
                    },
                ),
            ],
            risks=[
                Risk(
                    severity=RiskSeverity.LOW,
                    description="Draft artifact test.",
                )
            ],
            confidence=0.8,
        )


async def test_final_recommendations_create_applyable_artifacts(temp_db, tmp_path):
    project = tmp_path / "site"
    project.mkdir()
    (project / "index.html").write_text("<html><head></head><body></body></html>", encoding="utf-8")
    agent_registry.register(ArtifactPrimary())
    tid = _create_consult_task("artifact-primary", "fake", str(project))

    await run_task(tid)

    artifacts = list_artifacts(tid, include_content=True)
    assert {a["kind"] for a in artifacts} == {"file", "edit"}
    css = next(a for a in artifacts if a["kind"] == "file")
    edit = next(a for a in artifacts if a["kind"] == "edit")
    assert css["metadata"]["target_path"] == "crisp.css"
    assert "body { color" in css["content"]

    apply_artifact_to_project(tid, css["id"])
    apply_artifact_to_project(tid, edit["id"])

    assert (project / "crisp.css").read_text(encoding="utf-8") == "body { color: #111; }\n"
    assert 'href="crisp.css"' in (project / "index.html").read_text(encoding="utf-8")
