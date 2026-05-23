"""End-to-end coverage for the failure-cause-tag post-finalize hook.

Runs a conclave-like flow with the fake adapter, drives it to a known
agreement_level, then asserts that the orchestrator's terminal-state hook
populated `failure_cause_tags_json` on the final_results row AND that the
JSON-decoded tags surface on the `/api/tasks/{id}` final_result dict.

These tests do NOT exercise every rule — that's `test_trace_analyzer.py`.
They exist to prove the wiring (hook fires after status flip, write goes
through with_retry, API reads the column back).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app.agents.base import AdapterTestResult, BaseAdapter
from app.agents.fake_adapter import FakeAdapter
from app.database import connect, init_database, now_iso
from app.protocol.validators import (
    AgentRole,
    ConclaveConvergence,
    ConclaveTurn,
    Limits,
    MessageType,
    Permissions,
)
from app.services import agent_registry
from app.services.orchestrator import run_task
from app.utils.ids import task_id as new_task_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_database(str(db_path))
        agent_registry.clear()
        yield db_path


def _permissions() -> Permissions:
    return Permissions(
        can_read_files=True, can_write_files=False, can_run_commands=False,
        can_access_network=False, can_install_packages=False,
        can_apply_patches=False, can_read_env_files=False, can_read_secrets=False,
    )


def _create_conclave_task(participants: list[str], max_rounds: int = 2) -> str:
    tid = new_task_id()
    now = now_iso()
    limits = Limits(max_rounds=max_rounds, timeout_seconds=30, max_seconds=60)
    context = {"files": [], "error": None, "git_diff": None, "extra": {}}
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
            (id, created_at, updated_at, status, source, source_agent, mode, task_type,
             user_request, primary_agent, consultants, project_path,
             context_json, permissions_json, limits_json)
            VALUES (?, ?, ?, 'pending', 'api', NULL, 'conclave', 'general_consultation',
                    'Conclave test', NULL, ?, NULL, ?, ?, ?)""",
            (
                tid, now, now,
                json.dumps(participants),
                json.dumps(context, sort_keys=True),
                json.dumps(_permissions().model_dump(), sort_keys=True),
                json.dumps(limits.model_dump(), sort_keys=True),
            ),
        )
    return tid


# ---------------------------------------------------------------------------
# Adapter that always disagrees → drives the conclave to UNRESOLVED.
# ---------------------------------------------------------------------------

class _StubbornParticipant(BaseAdapter):
    """Always reports `still_thinking` with a unique position so convergence
    never happens. The orchestrator's round backstop terminates the task,
    producing an UNRESOLVED final → expect the dissent tag."""

    def __init__(self, agent_name: str, position: str):
        self._agent_name = agent_name
        self._position = position

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._agent_name

    async def is_available(self) -> bool:
        return True

    async def test_connection(self) -> AdapterTestResult:
        return AdapterTestResult(available=True, elapsed_ms=0)

    async def run_primary(self, ctx):  # pragma: no cover
        raise NotImplementedError

    async def run_consultant(self, ctx):  # pragma: no cover
        raise NotImplementedError

    async def run_final(self, ctx):  # pragma: no cover
        raise NotImplementedError

    async def run_peer(self, ctx):  # pragma: no cover
        raise NotImplementedError

    async def run_conclave_turn(self, ctx) -> ConclaveTurn:
        return ConclaveTurn(
            protocol_version="1.0",
            task_id=ctx.task_id,
            agent=self.name,
            role=AgentRole.PARTICIPANT,
            message_type=MessageType.CONCLAVE_TURN,
            summary=f"{self.name} disagrees.",
            analysis="Hard-coded dissent for failure-cause tagging test.",
            position=self._position,
            convergence=ConclaveConvergence.STILL_THINKING,
            confidence=0.6,
        )


async def test_unresolved_conclave_gets_dissent_tag(temp_db):
    """A conclave that exhausts max_rounds without converging must end with
    `failure_cause_tags` containing `unresolved_dissent` (plus the rounds-
    exhausted error tag won't apply because there's no rule for that yet)."""
    agent_registry.register(_StubbornParticipant("alpha", "Pick A."))
    agent_registry.register(_StubbornParticipant("beta", "Pick B."))

    tid = _create_conclave_task(["alpha", "beta"], max_rounds=2)
    await run_task(tid)

    with connect() as conn:
        final = conn.execute(
            "SELECT failure_cause_tags_json, agreement_level FROM final_results "
            "WHERE task_id = ?", (tid,),
        ).fetchone()
    assert final is not None
    tags = json.loads(final["failure_cause_tags_json"])
    assert "unresolved_dissent" in tags, (
        f"expected unresolved_dissent in tags, got {tags!r} "
        f"(agreement_level={final['agreement_level']!r})"
    )


async def test_clean_consensus_yields_empty_tags(temp_db):
    """The happy path: every participant signals i_am_done with the same
    position → strong convergence → no failure-cause tags fire."""
    class _AgreeableParticipant(BaseAdapter):
        def __init__(self, name): self._n = name
        @property
        def name(self) -> str: return self._n  # type: ignore[override]
        async def is_available(self) -> bool: return True
        async def test_connection(self) -> AdapterTestResult:
            return AdapterTestResult(available=True, elapsed_ms=0)
        async def run_primary(self, ctx): raise NotImplementedError  # pragma: no cover
        async def run_consultant(self, ctx): raise NotImplementedError  # pragma: no cover
        async def run_final(self, ctx): raise NotImplementedError  # pragma: no cover
        async def run_peer(self, ctx): raise NotImplementedError  # pragma: no cover
        async def run_conclave_turn(self, ctx) -> ConclaveTurn:
            return ConclaveTurn(
                protocol_version="1.0", task_id=ctx.task_id, agent=self._n,
                role=AgentRole.PARTICIPANT, message_type=MessageType.CONCLAVE_TURN,
                summary="Agreed.", analysis="Same position as everyone.",
                position="Go with Postgres.",
                convergence=ConclaveConvergence.I_AM_DONE,
                confidence=0.9,
            )

    agent_registry.register(_AgreeableParticipant("alpha"))
    agent_registry.register(_AgreeableParticipant("beta"))

    tid = _create_conclave_task(["alpha", "beta"])
    await run_task(tid)

    with connect() as conn:
        final = conn.execute(
            "SELECT failure_cause_tags_json FROM final_results WHERE task_id = ?",
            (tid,),
        ).fetchone()
    assert final is not None
    tags = json.loads(final["failure_cause_tags_json"])
    assert tags == [], f"expected no tags on strong convergence, got {tags!r}"


async def test_api_final_result_surfaces_failure_cause_tags(temp_db):
    """The /api/tasks/{id} envelope must include `failure_cause_tags` in its
    final_result dict (downstream UIs depend on this shape)."""
    from app.api.tasks import _row_to_final_result

    agent_registry.register(_StubbornParticipant("alpha", "A"))
    agent_registry.register(_StubbornParticipant("beta", "B"))
    tid = _create_conclave_task(["alpha", "beta"], max_rounds=2)
    await run_task(tid)

    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM final_results WHERE task_id = ?", (tid,),
        ).fetchone()
    payload = _row_to_final_result(row)
    assert "failure_cause_tags" in payload
    assert isinstance(payload["failure_cause_tags"], list)
    assert "unresolved_dissent" in payload["failure_cause_tags"]
