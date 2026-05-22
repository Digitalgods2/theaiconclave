"""End-to-end tests for conclave mode.

Covers:
- Strong convergence (all participants i_am_done with same position)
- Multi-round iteration (some still_thinking on round 1, all done on round 2)
- Holdout (one participant blocks for two rounds, finally agrees)
- User-input pause and resume in conclave
- Convergence-threshold fallback (majority not unanimous)
- Round-cap backstop
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


class _BehaviorOverrideFake(FakeAdapter):
    """Fake variant with a fixed name and a hardcoded behavior, ignoring task.context."""

    def __init__(self, agent_name: str, behavior: str):
        self._agent_name = agent_name
        self._fixed_behavior = behavior

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._agent_name

    @staticmethod
    def _behavior(ctx):  # type: ignore[override]
        return _current_behavior_lookup.get(ctx.task_id, "conclave_quick")


class _CancelAfterConclaveTurnFake(_BehaviorOverrideFake):
    async def run_conclave_turn(self, ctx):
        turn = await super().run_conclave_turn(ctx)
        with connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (now_iso(), ctx.task_id),
            )
        return turn


class _QuestionTurnAdapter(BaseAdapter):
    def __init__(self, agent_name: str, question: str):
        self._agent_name = agent_name
        self._question = question

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._agent_name

    async def is_available(self) -> bool:
        return True

    async def test_connection(self) -> AdapterTestResult:
        return AdapterTestResult(available=True, elapsed_ms=0)

    async def run_primary(self, ctx):  # pragma: no cover - not used
        raise NotImplementedError

    async def run_consultant(self, ctx):  # pragma: no cover - not used
        raise NotImplementedError

    async def run_final(self, ctx):  # pragma: no cover - not used
        raise NotImplementedError

    async def run_peer(self, ctx):  # pragma: no cover - not used
        raise NotImplementedError

    async def run_conclave_turn(self, ctx) -> ConclaveTurn:
        return ConclaveTurn(
            protocol_version="1.0",
            task_id=ctx.task_id,
            agent=self.name,
            role=AgentRole.PARTICIPANT,
            message_type=MessageType.CONCLAVE_TURN,
            summary=f"{self.name} needs clarification.",
            analysis="A user answer is required before final synthesis.",
            position="Cannot finalize without clarification.",
            convergence=ConclaveConvergence.NEED_USER_INPUT,
            user_input_question=self._question,
            confidence=0.5,
        )


# Per-test behavior lookup keyed by task_id, used by the fake adapters above.
_current_behavior_lookup: dict[str, str] = {}


def _register_three_fakes():
    agent_registry.clear()
    agent_registry.register(_BehaviorOverrideFake("alpha", "conclave_quick"))
    agent_registry.register(_BehaviorOverrideFake("beta", "conclave_quick"))
    agent_registry.register(_BehaviorOverrideFake("gamma", "conclave_quick"))


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_database(str(db_path))
        _register_three_fakes()
        yield db_path
        _current_behavior_lookup.clear()


def _create_conclave_task(
    behavior: str,
    *,
    max_rounds: int = 10,
    max_seconds: int = 60,
    convergence_threshold: float = 1.0,
    participants: list[str] | None = None,
) -> str:
    tid = new_task_id()
    now = now_iso()
    permissions = Permissions(
        can_read_files=True, can_write_files=False, can_run_commands=False,
        can_access_network=False, can_install_packages=False,
        can_apply_patches=False, can_read_env_files=False, can_read_secrets=False,
    )
    limits = Limits(
        max_rounds=max_rounds, timeout_seconds=30,
        max_seconds=max_seconds, convergence_threshold=convergence_threshold,
    )
    context = {"files": [], "error": None, "git_diff": None, "extra": {}}
    parts = participants or ["alpha", "beta", "gamma"]

    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks
            (id, created_at, updated_at, status, source, source_agent, mode, task_type,
             user_request, primary_agent, consultants, project_path,
             context_json, permissions_json, limits_json)
            VALUES (?, ?, ?, 'pending', 'api', NULL, 'conclave', 'general_consultation',
                    'Conclave test request', NULL, ?, NULL, ?, ?, ?)""",
            (
                tid, now, now,
                json.dumps(parts),
                json.dumps(context, sort_keys=True),
                json.dumps(permissions.model_dump(), sort_keys=True),
                json.dumps(limits.model_dump(), sort_keys=True),
            ),
        )
    _current_behavior_lookup[tid] = behavior
    return tid


def _task_status(tid: str) -> str:
    with connect() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    return row["status"]


def _final_result(tid: str):
    with connect() as conn:
        row = conn.execute("SELECT * FROM final_results WHERE task_id = ?", (tid,)).fetchone()
    return row


def _participant_turn_count(tid: str) -> int:
    with connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM agent_messages
               WHERE task_id = ? AND message_type = 'conclave_turn'""",
            (tid,),
        ).fetchone()
    return row["n"]


# ---------------------------------------------------------------------------
# Strong convergence on round 1
# ---------------------------------------------------------------------------

async def test_conclave_quick_convergence(temp_db):
    tid = _create_conclave_task("conclave_quick")
    await run_task(tid)

    assert _task_status(tid) == "completed"
    # 3 participants, 1 round, 3 turns
    assert _participant_turn_count(tid) == 3
    result = _final_result(tid)
    assert result["agreement_level"] == "consensus"


# ---------------------------------------------------------------------------
# Multi-round: still_thinking on round 1, i_am_done on round 2
# ---------------------------------------------------------------------------

async def test_conclave_iterates_two_rounds(temp_db):
    tid = _create_conclave_task("conclave_iterate")
    await run_task(tid)

    assert _task_status(tid) == "completed"
    # 3 participants × 2 rounds = 6 turns
    assert _participant_turn_count(tid) == 6


# ---------------------------------------------------------------------------
# Holdout: still_thinking on rounds 1-2, i_am_done on round 3
# ---------------------------------------------------------------------------

async def test_conclave_holdout(temp_db):
    tid = _create_conclave_task("conclave_holdout")
    await run_task(tid)

    assert _task_status(tid) == "completed"
    # 3 participants × 3 rounds = 9 turns
    assert _participant_turn_count(tid) == 9


# ---------------------------------------------------------------------------
# Round-cap backstop fires
# ---------------------------------------------------------------------------

async def test_conclave_rounds_exhausted(temp_db):
    tid = _create_conclave_task("conclave_holdout", max_rounds=2)
    await run_task(tid)

    result = _final_result(tid)
    assert result is not None
    errors = json.loads(result["errors_json"])
    assert any(e["code"] == "rounds_exhausted" for e in errors)


# ---------------------------------------------------------------------------
# User-input pause + resume in conclave
# ---------------------------------------------------------------------------

async def test_conclave_user_input_pause_resume(temp_db):
    tid = _create_conclave_task("conclave_ask")
    await run_task(tid)

    # First run pauses for user input.
    assert _task_status(tid) == "awaiting_user_input"

    # Provide an answer and resume.
    from app.utils.ids import message_id
    with connect() as conn:
        conn.execute(
            """INSERT INTO agent_messages
               (id, task_id, agent_run_id, agent_name, role, message_type,
                direction, content, structured_json, created_at)
               VALUES (?, ?, NULL, 'user', 'user', 'user_input_response',
                       'from_user', ?, NULL, ?)""",
            (message_id(), tid, "Python 3.13 only.", now_iso()),
        )
        conn.execute(
            "UPDATE tasks SET status = 'pending', updated_at = ? WHERE id = ?",
            (now_iso(), tid),
        )

    await run_task(tid)
    assert _task_status(tid) == "completed"


async def test_conclave_aggregates_all_user_input_questions(temp_db):
    agent_registry.clear()
    agent_registry.register(_QuestionTurnAdapter("alpha", "What visual direction should guide the design?"))
    agent_registry.register(_QuestionTurnAdapter("beta", "Which pages are in scope?"))
    agent_registry.register(_QuestionTurnAdapter("gamma", "Should existing CSS remain as a compatibility layer?"))
    tid = _create_conclave_task(
        "unused",
        participants=["alpha", "beta", "gamma"],
    )

    await run_task(tid)

    assert _task_status(tid) == "awaiting_user_input"
    with connect() as conn:
        req = conn.execute(
            """SELECT agent_name, content FROM agent_messages
               WHERE task_id = ? AND message_type = 'user_input_request'""",
            (tid,),
        ).fetchone()
        result = conn.execute(
            "SELECT * FROM final_results WHERE task_id = ?",
            (tid,),
        ).fetchone()
    assert result is None
    assert req is not None
    assert req["agent_name"] == "alpha, beta, gamma"
    assert "1. What visual direction should guide the design?" in req["content"]
    assert "2. Which pages are in scope?" in req["content"]
    assert "3. Should existing CSS remain as a compatibility layer?" in req["content"]


# ---------------------------------------------------------------------------
# Convergence threshold below 1.0: majority terminates
# ---------------------------------------------------------------------------

async def test_synthesis_round_triggers_on_weak_convergence(temp_db):
    """
    Conclave Charter §Minor Difference Resolution: when participants all signal
    i_am_done but their positions diverge, the orchestrator must run one focused
    synthesis round before terminating.
    """
    tid = _create_conclave_task("conclave_diverge")
    await run_task(tid)

    assert _task_status(tid) == "completed"

    # The orchestrator should have injected a synthesis_directive message.
    with connect() as conn:
        directive = conn.execute(
            """SELECT * FROM agent_messages
               WHERE task_id = ? AND message_type = 'synthesis_directive'""",
            (tid,),
        ).fetchone()
    assert directive is not None, "expected exactly one synthesis_directive message"
    assert "ORCHESTRATOR DIRECTIVE" in directive["content"]
    assert "Diverging positions" in directive["content"]

    # Two rounds × 3 participants = 6 conclave_turn messages.
    assert _participant_turn_count(tid) == 6


async def test_synthesis_round_runs_at_most_once(temp_db):
    """The charter says 'one focused synthesis round' — even if the synthesis
    round itself produces weak convergence, the orchestrator must terminate."""
    tid = _create_conclave_task("conclave_diverge", max_rounds=10)
    await run_task(tid)

    with connect() as conn:
        directives = conn.execute(
            """SELECT COUNT(*) AS n FROM agent_messages
               WHERE task_id = ? AND message_type = 'synthesis_directive'""",
            (tid,),
        ).fetchone()
    assert directives["n"] == 1, "synthesis directive should appear exactly once"

    # Task must NOT have looped past the synthesis round.
    assert _participant_turn_count(tid) == 6  # 2 rounds total


async def test_conclave_majority_threshold(temp_db):
    """With threshold=0.66 and 3 participants, 2 of 3 agreeing terminates."""
    # Mix participants: alpha quick, beta quick, gamma holdout (still_thinking).
    # With threshold 0.66, after round 1: 2 of 3 done → terminate.
    agent_registry.clear()
    agent_registry.register(_BehaviorOverrideFake("alpha", "conclave_quick"))
    agent_registry.register(_BehaviorOverrideFake("beta", "conclave_quick"))
    agent_registry.register(_BehaviorOverrideFake("gamma", "conclave_holdout"))

    tid = _create_conclave_task("conclave_quick", convergence_threshold=0.66)
    # Override the per-task behavior — leave the global lookup empty so each
    # adapter falls back to its constructor-bound behavior. The adapter's
    # _behavior reads from the global lookup; we set defaults per-agent here.
    _current_behavior_lookup.clear()

    # _BehaviorOverrideFake._behavior reads from _current_behavior_lookup keyed by task_id.
    # Since we left it empty, default is "conclave_quick". But we need gamma to act differently.
    # Re-define the gamma adapter inline to ignore the lookup and use a fixed behavior.
    class _GammaHoldout(_BehaviorOverrideFake):
        @staticmethod
        def _behavior(ctx):
            return "conclave_holdout"

    agent_registry.clear()
    agent_registry.register(_BehaviorOverrideFake("alpha", "conclave_quick"))
    agent_registry.register(_BehaviorOverrideFake("beta", "conclave_quick"))
    agent_registry.register(_GammaHoldout("gamma", "conclave_holdout"))
    _current_behavior_lookup[tid] = "conclave_quick"

    await run_task(tid)

    assert _task_status(tid) == "completed"
    # Round 1: alpha+beta done (2/3), threshold met BUT positions differ
    # (alpha+beta say "Default fake position" while gamma says holdout text).
    # Per Charter, synthesis round fires once before terminating.
    # 2 rounds × 3 participants = 6 turns, plus 1 synthesis_directive.
    assert _participant_turn_count(tid) == 6
    with connect() as conn:
        directive = conn.execute(
            """SELECT COUNT(*) AS n FROM agent_messages
               WHERE task_id = ? AND message_type = 'synthesis_directive'""",
            (tid,),
        ).fetchone()
    assert directive["n"] == 1


async def test_cancelled_conclave_does_not_start_another_round(temp_db):
    agent_registry.clear()
    agent_registry.register(_CancelAfterConclaveTurnFake("alpha", "conclave_quick"))
    agent_registry.register(_BehaviorOverrideFake("beta", "conclave_quick"))
    agent_registry.register(_BehaviorOverrideFake("gamma", "conclave_quick"))

    tid = _create_conclave_task("conclave_diverge")
    await run_task(tid)

    assert _task_status(tid) == "cancelled"
    assert _final_result(tid) is None
    assert _participant_turn_count(tid) == 3
    with connect() as conn:
        directives = conn.execute(
            """SELECT COUNT(*) AS n FROM agent_messages
               WHERE task_id = ? AND message_type = 'synthesis_directive'""",
            (tid,),
        ).fetchone()
    assert directives["n"] == 0
