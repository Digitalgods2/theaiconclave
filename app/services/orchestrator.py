"""Orchestrator — dispatches tasks to the appropriate flow.

Modes:
- resolve: open-ended loop until primary returns RESOLVED/CANNOT_RESOLVE,
  pauses on NEEDS_USER_INPUT, backstopped by max_seconds + max_rounds + repetition.
- consult: bounded primary → consultant(s) → primary final.
- handoff/poll: deferred to v0.2.

Resumption: run_resolve seeds prior_messages from the agent_messages table,
so a task that paused for user input can be re-entered after the user answers.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from app.agents.base import AdapterContext, AdapterError, BaseAdapter
from app.database import connect, now_iso
from app.protocol.validators import (
    AgentRole,
    AgreementLevel,
    ConclaveConvergence,
    ConclaveTurn,
    ConsultantCritique,
    Disagreement,
    ErrorCode,
    FinalResult,
    MessageType,
    PrimaryResponse,
    ProtocolError,
    ResolutionStatus,
    TaskMode,
    TaskRequest,
    TaskStatus,
)
from app.services import agent_registry
from app.services.judge import judge_convergence
from app.services.sandbox import cleanup_sandbox, prepare_sandbox
from app.utils.ids import message_id, result_id, run_id


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _record_message(
    task_id: str,
    agent_run_id: Optional[str],
    agent_name: str,
    role: str,
    message_type: MessageType,
    direction: str,
    content: Optional[str],
    structured: Optional[dict[str, Any]],
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_messages
            (id, task_id, agent_run_id, agent_name, role, message_type,
             direction, content, structured_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id(),
                task_id,
                agent_run_id,
                agent_name,
                role,
                message_type.value,
                direction,
                content,
                json.dumps(structured, sort_keys=True) if structured else None,
                now_iso(),
            ),
        )


def _record_run_start(task_id: str, agent_name: str, role: AgentRole, round_number: int) -> str:
    rid = run_id()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_runs
            (id, task_id, agent_name, role, round_number, started_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'running')
            """,
            (rid, task_id, agent_name, role.value, round_number, now_iso()),
        )
    return rid


def _record_run_end(
    run_id_value: str,
    status: str,
    duration_ms: int,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    cost_usd: Optional[float] = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE agent_runs
            SET finished_at = ?, status = ?, duration_ms = ?,
                error_code = ?, error_message = ?,
                input_tokens = ?, output_tokens = ?, cost_usd = ?
            WHERE id = ?
            """,
            (
                now_iso(), status, duration_ms,
                error_code, error_message,
                input_tokens, output_tokens, cost_usd,
                run_id_value,
            ),
        )


def _save_final_result(task_id: str, result: FinalResult) -> None:
    with connect() as conn:
        # Delete any prior result row to make this safe across resumption / retry.
        conn.execute("DELETE FROM final_results WHERE task_id = ?", (task_id,))
        conn.execute(
            """
            INSERT INTO final_results
            (id, task_id, final_answer, agreement_level, resolution_status,
             disagreements_json, recommended_actions_json, risks_json,
             commands_requiring_approval_json, patches_requiring_approval_json,
             errors_json, confidence_aggregate_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result_id(),
                task_id,
                result.final_answer,
                result.agreement_level.value,
                result.resolution_status.value if result.resolution_status else None,
                json.dumps([d.model_dump() for d in result.disagreements], sort_keys=True),
                json.dumps([a.model_dump() for a in result.recommended_actions], sort_keys=True),
                json.dumps([r.model_dump() for r in result.risks], sort_keys=True),
                json.dumps(result.commands_requiring_approval),
                json.dumps(result.patches_requiring_approval),
                json.dumps([e.model_dump() for e in result.errors], sort_keys=True),
                json.dumps(result.confidence_aggregate, sort_keys=True) if result.confidence_aggregate else None,
                now_iso(),
            ),
        )


def _compute_confidence_aggregate(turns: list) -> Optional[dict]:
    """Aggregate confidence across the last-round participant turns.

    Returns {min, max, mean, count, missing_count} or None if no turn carried
    a confidence score. Phase 2 of post-DR plan on tsk_01KRSW6AS3M66B4RRJE3JFAPRV.
    """
    if not turns:
        return None
    scores = [t.confidence for t in turns if getattr(t, "confidence", None) is not None]
    if not scores:
        return None
    return {
        "min":  round(min(scores), 3),
        "max":  round(max(scores), 3),
        "mean": round(sum(scores) / len(scores), 3),
        "count": len(scores),
        "missing_count": len(turns) - len(scores),
    }


def _set_task_status(
    task_id: str,
    status: TaskStatus,
    error_message: Optional[str] = None,
) -> None:
    with connect() as conn:
        if error_message is not None:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ?, error_message = ? WHERE id = ?",
                (status.value, now_iso(), error_message, task_id),
            )
        else:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, now_iso(), task_id),
            )


def _record_user_input_request(task_id: str, primary_agent: str, question: str) -> None:
    """Persist the primary's question to the user as a synthetic to_user message."""
    _record_message(
        task_id=task_id,
        agent_run_id=None,
        agent_name=primary_agent,
        role=AgentRole.PRIMARY.value,
        message_type=MessageType.USER_INPUT_REQUEST,
        direction="to_user",
        content=question,
        structured=None,
    )


_SYNTHESIS_INSTRUCTION = (
    "ORCHESTRATOR DIRECTIVE -- Synthesis Round.\n\n"
    "All participants signaled i_am_done, but positions diverge. Per Conclave "
    "Charter Section 'Minor Difference Resolution', run ONE focused synthesis "
    "round. Resolve in priority order: (1) Glen's stated intent and success "
    "criteria, (2) evidence and constraints, (3) safety and permission "
    "boundaries, (4) simplicity and reversibility, (5) taste only after the "
    "above are settled. Engage each other's actual positions and either "
    "converge on a single statement or explicitly preserve unresolved "
    "differences. This is the only synthesis round; the orchestrator will "
    "terminate after this round regardless of convergence."
)


def _synthesis_directive_dict(last_turns: list) -> dict:
    """Build the synthesis directive as a prior-message dict.

    Includes each participant's confidence so the synthesizer can weight
    positions by stated certainty rather than treating all voices as equal-
    confidence. Phase 2 of post-DR plan on tsk_01KRSW6AS3M66B4RRJE3JFAPRV.
    """
    def _conf(t) -> str:
        c = getattr(t, "confidence", None)
        return f" [confidence {c:.2f}]" if isinstance(c, (int, float)) else ""

    positions_summary = "; ".join(
        f"{t.agent}{_conf(t)}: {t.position[:120]}" for t in last_turns
    )
    return {
        "agent": "orchestrator",
        "role": "system",
        "message_type": "synthesis_directive",
        "content": _SYNTHESIS_INSTRUCTION + "\n\nDiverging positions:\n" + positions_summary,
    }


def _record_synthesis_directive(task_id: str, last_turns: list) -> None:
    """Persist the synthesis directive so it shows up in the dashboard and resumes correctly."""
    directive = _synthesis_directive_dict(last_turns)
    _record_message(
        task_id=task_id,
        agent_run_id=None,
        agent_name="orchestrator",
        role="system",
        # synthesis_directive is not in the MessageType enum (intentionally — it's
        # an orchestrator-internal signal, not a protocol message). Pass the
        # string directly via a small wrapper.
        message_type=_SyntheticType("synthesis_directive"),
        direction="system_to_agent",
        content=directive["content"],
        structured=None,
    )


class _SyntheticType:
    """Tiny shim so _record_message can accept a string message_type without bloating the enum."""
    def __init__(self, value: str) -> None:
        self.value = value


# ---------------------------------------------------------------------------
# Adapter call wrapper
# ---------------------------------------------------------------------------

def _make_context(task: TaskRequest, task_id: str, prior: list[dict]) -> AdapterContext:
    return AdapterContext(
        task=task,
        task_id=task_id,
        prior_messages=prior,
        permissions=task.permissions,
        timeout_seconds=task.limits.timeout_seconds,
        working_directory=task.project_path or ".",
    )


async def _call_adapter_method(
    adapter: BaseAdapter,
    method: str,
    ctx: AdapterContext,
    task_id: str,
    role: AgentRole,
    round_number: int,
) -> tuple[Any, Optional[ProtocolError]]:
    rid = _record_run_start(task_id, adapter.name, role, round_number)
    start = time.perf_counter()
    try:
        adapter._last_usage = {}  # reset before call
        result = await getattr(adapter, method)(ctx)
        duration_ms = int((time.perf_counter() - start) * 1000)
        usage = getattr(adapter, "_last_usage", None) or {}
        _record_run_end(
            rid, "completed", duration_ms,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cost_usd=usage.get("cost_usd"),
        )
        _record_message(
            task_id=task_id,
            agent_run_id=rid,
            agent_name=adapter.name,
            role=role.value,
            message_type=result.message_type,
            direction="from_agent",
            content=None,
            structured=result.model_dump(mode="json"),
        )
        return result, None
    except AdapterError as e:
        duration_ms = int((time.perf_counter() - start) * 1000)
        _record_run_end(rid, "failed", duration_ms, e.code.value, e.message)
        return None, ProtocolError(code=e.code, message=e.message, details=e.details)
    except Exception as e:  # noqa: BLE001 — adapters can raise unexpected types
        duration_ms = int((time.perf_counter() - start) * 1000)
        _record_run_end(rid, "failed", duration_ms, ErrorCode.AGENT_ERROR.value, str(e))
        return None, ProtocolError(
            code=ErrorCode.AGENT_ERROR,
            message=f"Unexpected adapter error: {e}",
            details={"exception_type": type(e).__name__},
        )


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------

def _serialize_messages(*messages) -> list[dict]:
    return [m.model_dump(mode="json") for m in messages if m is not None]


def _agreement_to_level(critiques: list[ConsultantCritique]) -> AgreementLevel:
    if not critiques:
        return AgreementLevel.CONSENSUS
    if any(c.agreement.value == "disagree" for c in critiques):
        return AgreementLevel.MAJOR_DISAGREEMENT
    if any(c.agreement.value == "partial" for c in critiques):
        return AgreementLevel.MINOR_DISAGREEMENT
    return AgreementLevel.CONSENSUS


def _build_disagreements(
    critiques: list[ConsultantCritique],
    primary_final: Optional[PrimaryResponse],
) -> list[Disagreement]:
    out: list[Disagreement] = []
    primary_pos = primary_final.summary if primary_final else "(no final answer)"
    for c in critiques:
        if c.agreement.value == "agree":
            continue
        out.append(Disagreement(
            topic=f"Critique from {c.agent}",
            primary_position=primary_pos,
            consultant_position=c.critique,
        ))
    return out


def _too_similar(a: str, b: str, threshold: float = 0.85) -> bool:
    """Token-set Jaccard for a quick repetition guard. Cheap, not robust."""
    if not a or not b:
        return False
    a_set = set(a.lower().split())
    b_set = set(b.lower().split())
    if not a_set or not b_set:
        return False
    union = a_set | b_set
    intersection = a_set & b_set
    return (len(intersection) / len(union)) >= threshold


# ---------------------------------------------------------------------------
# Consult flow (bounded second opinion)
# ---------------------------------------------------------------------------

async def run_consult(task: TaskRequest, task_id: str) -> FinalResult:
    errors: list[ProtocolError] = []
    primary = agent_registry.get(task.primary_agent or "")

    proposal, err = await _call_adapter_method(
        primary, "run_primary",
        _make_context(task, task_id, []),
        task_id, AgentRole.PRIMARY, 1,
    )
    if err:
        errors.append(err)

    critiques: list[ConsultantCritique] = []
    if proposal:
        prior = _serialize_messages(proposal)
        for consultant_name in task.consultants:
            try:
                consultant = agent_registry.get(consultant_name)
            except KeyError:
                errors.append(ProtocolError(
                    code=ErrorCode.AGENT_UNAVAILABLE,
                    message=f"Consultant {consultant_name} not registered.",
                    details={"agent": consultant_name},
                ))
                continue
            critique, err = await _call_adapter_method(
                consultant, "run_consultant",
                _make_context(task, task_id, prior),
                task_id, AgentRole.CONSULTANT, 2,
            )
            if err:
                errors.append(err)
            if critique:
                critiques.append(critique)

    primary_final: Optional[PrimaryResponse] = None
    if proposal and critiques:
        prior = _serialize_messages(proposal, *critiques)
        primary_final, err = await _call_adapter_method(
            primary, "run_final",
            _make_context(task, task_id, prior),
            task_id, AgentRole.PRIMARY, 3,
        )
        if err:
            errors.append(err)
    elif proposal:
        primary_final = proposal

    return _assemble_final(
        task=task, task_id=task_id,
        primary_resp=primary_final, critiques=critiques,
        errors=errors, resolution_status=None,
    )


# ---------------------------------------------------------------------------
# Resolve flow (open-ended, goal-based termination)
# ---------------------------------------------------------------------------

async def run_resolve(
    task: TaskRequest,
    task_id: str,
    prior_messages: Optional[list[dict]] = None,
) -> Optional[FinalResult]:
    """
    Resolve loop. Returns None when paused for user input (caller does not finalize).
    Returns a FinalResult on terminal completion.
    """
    errors: list[ProtocolError] = []
    primary = agent_registry.get(task.primary_agent or "")

    start_time = time.time()
    max_seconds = task.limits.max_seconds or 600
    max_rounds_backstop = task.limits.max_rounds

    prior: list[dict] = list(prior_messages or [])
    last_primary_text: Optional[str] = None
    last_primary_resp: Optional[PrimaryResponse] = None
    last_critiques: list[ConsultantCritique] = []

    round_num = sum(
        1 for m in prior if m.get("message_type") == MessageType.PRIMARY_PROPOSAL.value
    )

    while True:
        round_num += 1

        if (time.time() - start_time) > max_seconds:
            errors.append(ProtocolError(
                code=ErrorCode.RESOLVE_TIMEOUT,
                message=f"Resolve loop exceeded max_seconds ({max_seconds}).",
            ))
            return _assemble_final(
                task=task, task_id=task_id,
                primary_resp=last_primary_resp, critiques=last_critiques,
                errors=errors,
                resolution_status=ResolutionStatus.CANNOT_RESOLVE,
            )

        if round_num > max_rounds_backstop:
            errors.append(ProtocolError(
                code=ErrorCode.ROUNDS_EXHAUSTED,
                message=f"Resolve loop exceeded max_rounds backstop ({max_rounds_backstop}).",
            ))
            return _assemble_final(
                task=task, task_id=task_id,
                primary_resp=last_primary_resp, critiques=last_critiques,
                errors=errors,
                resolution_status=ResolutionStatus.CANNOT_RESOLVE,
            )

        primary_resp, err = await _call_adapter_method(
            primary, "run_primary",
            _make_context(task, task_id, prior),
            task_id, AgentRole.PRIMARY, round_num,
        )
        if err:
            errors.append(err)
            return _assemble_final(
                task=task, task_id=task_id,
                primary_resp=last_primary_resp, critiques=last_critiques,
                errors=errors,
                resolution_status=ResolutionStatus.CANNOT_RESOLVE,
            )

        cur_text = f"{primary_resp.summary} {primary_resp.analysis}"
        if last_primary_text and _too_similar(cur_text, last_primary_text):
            errors.append(ProtocolError(
                code=ErrorCode.LOOP_DETECTED,
                message="Primary response too similar to prior round.",
            ))
            return _assemble_final(
                task=task, task_id=task_id,
                primary_resp=primary_resp, critiques=last_critiques,
                errors=errors,
                resolution_status=ResolutionStatus.CANNOT_RESOLVE,
            )
        last_primary_text = cur_text
        last_primary_resp = primary_resp

        prior = prior + [primary_resp.model_dump(mode="json")]

        rs = primary_resp.resolution_status
        if rs is None:
            errors.append(ProtocolError(
                code=ErrorCode.AGENT_ERROR,
                message="Primary did not return resolution_status in resolve mode.",
            ))
            return _assemble_final(
                task=task, task_id=task_id,
                primary_resp=primary_resp, critiques=last_critiques,
                errors=errors,
                resolution_status=ResolutionStatus.CANNOT_RESOLVE,
            )

        if rs == ResolutionStatus.CANNOT_RESOLVE:
            return _assemble_final(
                task=task, task_id=task_id,
                primary_resp=primary_resp, critiques=last_critiques,
                errors=errors,
                resolution_status=ResolutionStatus.CANNOT_RESOLVE,
            )

        if rs == ResolutionStatus.NEEDS_USER_INPUT:
            _record_user_input_request(
                task_id=task_id,
                primary_agent=primary.name,
                question=primary_resp.user_input_question or "(question not provided)",
            )
            _set_task_status(task_id, TaskStatus.AWAITING_USER_INPUT)
            return None  # caller will not finalize; resumption happens via /answer endpoint

        # RESOLVED or NEEDS_MORE_ROUNDS — give consultants a turn (if any)
        critiques: list[ConsultantCritique] = []
        for consultant_name in task.consultants:
            try:
                consultant = agent_registry.get(consultant_name)
            except KeyError:
                errors.append(ProtocolError(
                    code=ErrorCode.AGENT_UNAVAILABLE,
                    message=f"Consultant {consultant_name} not registered.",
                ))
                continue
            critique, err = await _call_adapter_method(
                consultant, "run_consultant",
                _make_context(task, task_id, prior),
                task_id, AgentRole.CONSULTANT, round_num,
            )
            if err:
                errors.append(err)
            if critique:
                critiques.append(critique)
                prior = prior + [critique.model_dump(mode="json")]
        last_critiques = critiques

        if rs == ResolutionStatus.RESOLVED:
            if any(c.wants_continuation for c in critiques):
                continue  # consultant pushback — keep iterating
            return _assemble_final(
                task=task, task_id=task_id,
                primary_resp=primary_resp, critiques=critiques,
                errors=errors,
                resolution_status=ResolutionStatus.RESOLVED,
            )
        # NEEDS_MORE_ROUNDS — primary explicitly wants another turn
        continue


# ---------------------------------------------------------------------------
# Conclave flow (N participants, full mesh, convergence-based termination)
# ---------------------------------------------------------------------------

async def run_conclave(
    task: TaskRequest,
    task_id: str,
    prior_messages: Optional[list[dict]] = None,
) -> Optional[FinalResult]:
    """
    Conclave loop. Each round, every participant contributes one ConclaveTurn
    in parallel (they see all prior rounds, but not each other's same-round turns).
    Terminates when at least `convergence_threshold` fraction of participants
    signal i_am_done, or on user-input pause / backstop.

    Returns None when paused for user input. Returns a FinalResult on terminal completion.
    """
    import asyncio

    errors: list[ProtocolError] = []
    participant_names = list(task.consultants)
    participants = []
    for name in participant_names:
        try:
            participants.append(agent_registry.get(name))
        except KeyError:
            errors.append(ProtocolError(
                code=ErrorCode.AGENT_UNAVAILABLE,
                message=f"Participant {name} not registered.",
                details={"agent": name},
            ))
    if len(participants) < 2:
        errors.append(ProtocolError(
            code=ErrorCode.INVALID_REQUEST,
            message="Conclave requires at least 2 available participants.",
        ))
        return await _assemble_conclave_final(task, task_id, [], errors, "failed")

    start_time = time.time()
    max_seconds = task.limits.max_seconds or 600
    max_rounds_backstop = task.limits.max_rounds
    threshold = task.limits.convergence_threshold

    prior: list[dict] = list(prior_messages or [])
    last_turns: list[ConclaveTurn] = []
    last_round_signature: Optional[str] = None
    synthesis_attempted = any(
        m.get("message_type") == "synthesis_directive" for m in prior
    )

    # Determine starting round number from prior messages
    round_num = 0
    if prior:
        rounds_seen = {
            (m.get("agent"), int(_estimate_round_index(prior, m)))
            for m in prior
            if m.get("message_type") == MessageType.CONCLAVE_TURN.value
        }
        if rounds_seen:
            round_num = max(r for _, r in rounds_seen)

    while True:
        round_num += 1

        if (time.time() - start_time) > max_seconds:
            errors.append(ProtocolError(
                code=ErrorCode.RESOLVE_TIMEOUT,
                message=f"Conclave exceeded max_seconds ({max_seconds}).",
            ))
            return await _assemble_conclave_final(task, task_id, last_turns, errors, "completed")

        if round_num > max_rounds_backstop:
            errors.append(ProtocolError(
                code=ErrorCode.ROUNDS_EXHAUSTED,
                message=f"Conclave exceeded max_rounds backstop ({max_rounds_backstop}).",
            ))
            return await _assemble_conclave_final(task, task_id, last_turns, errors, "completed")

        # Run all participants for this round in parallel.
        async def call_one(p):
            return await _call_adapter_method(
                p, "run_conclave_turn",
                _make_context(task, task_id, prior),
                task_id, AgentRole.PARTICIPANT, round_num,
            )

        results = await asyncio.gather(*(call_one(p) for p in participants))
        round_turns: list[ConclaveTurn] = []
        for turn, err in results:
            if err:
                errors.append(err)
            if turn is not None:
                round_turns.append(turn)
                prior = prior + [turn.model_dump(mode="json")]

        if not round_turns:
            errors.append(ProtocolError(
                code=ErrorCode.AGENT_ERROR,
                message="No participant produced a valid turn this round.",
            ))
            return await _assemble_conclave_final(task, task_id, last_turns, errors, "failed")

        last_turns = round_turns

        # Check user-input pause: if any participant asked, pause for the user.
        for turn in round_turns:
            if turn.convergence == ConclaveConvergence.NEED_USER_INPUT:
                _record_user_input_request(
                    task_id=task_id,
                    primary_agent=turn.agent,
                    question=turn.user_input_question or "(question not provided)",
                )
                _set_task_status(task_id, TaskStatus.AWAITING_USER_INPUT)
                return None

        # Repetition check: if this round's positions are identical to the prior round, stop.
        round_sig = "|".join(sorted(f"{t.agent}:{t.position}" for t in round_turns))
        if last_round_signature is not None and _too_similar(round_sig, last_round_signature):
            errors.append(ProtocolError(
                code=ErrorCode.LOOP_DETECTED,
                message="Conclave round positions are too similar to the prior round.",
            ))
            return await _assemble_conclave_final(task, task_id, round_turns, errors, "completed")
        last_round_signature = round_sig

        # Convergence check
        done_count = sum(1 for t in round_turns if t.convergence == ConclaveConvergence.I_AM_DONE)
        fraction_done = done_count / len(participants)
        if fraction_done >= threshold:
            # Charter §Minor Difference Resolution: when convergence threshold is
            # met but positions diverge, run ONE focused synthesis round before
            # terminating with weak convergence.
            positions_set = {_normalize(t.position) for t in round_turns}
            if len(positions_set) > 1 and not synthesis_attempted:
                synthesis_attempted = True
                _record_synthesis_directive(task_id, round_turns)
                prior = prior + [_synthesis_directive_dict(round_turns)]
                continue
            return await _assemble_conclave_final(task, task_id, round_turns, errors, "completed")
        elif synthesis_attempted:
            # Synthesis round didn't produce threshold convergence either — accept
            # the current state per "one focused synthesis round" rule.
            return await _assemble_conclave_final(task, task_id, round_turns, errors, "completed")
        # Else continue to next round.


def _estimate_round_index(prior: list[dict], target: dict) -> int:
    """Best-effort round index for a conclave turn message based on its position in the transcript."""
    same_agent_count = 0
    for m in prior:
        if m.get("message_type") != MessageType.CONCLAVE_TURN.value:
            continue
        if m.get("agent") == target.get("agent"):
            same_agent_count += 1
            if m is target or (
                m.get("summary") == target.get("summary") and m.get("position") == target.get("position")
            ):
                return same_agent_count
    return same_agent_count


async def _assemble_conclave_final(
    task: TaskRequest,
    task_id: str,
    last_turns: list[ConclaveTurn],
    errors: list[ProtocolError],
    status_label: str,
) -> FinalResult:
    """Build a FinalResult from the last round of a conclave."""
    judge_verdict: Optional[dict] = None
    if not last_turns:
        final_answer = "(conclave produced no participant turns)"
        agreement_level = AgreementLevel.UNRESOLVED
        status = TaskStatus.FAILED
    else:
        # Strong vs weak convergence: are all positions substantively the same?
        positions = [t.position for t in last_turns]
        all_done = all(t.convergence == ConclaveConvergence.I_AM_DONE for t in last_turns)
        unique_positions = len({_normalize(p) for p in positions})
        if all_done and unique_positions == 1:
            agreement_level = AgreementLevel.CONSENSUS
            final_answer = (
                f"Strong convergence across {len(last_turns)} participants:\n\n"
                f"{positions[0]}"
            )
        elif all_done:
            agreement_level = AgreementLevel.MINOR_DISAGREEMENT
            # Convergence judge pass: ask one participant whether the divergent
            # positions are substantively equivalent despite wording differences.
            # If yes, upgrade to CONSENSUS. If no / inconclusive, MINOR_DISAGREEMENT stands.
            judge_verdict = await _run_convergence_judge(task, task_id, last_turns)
            if judge_verdict and judge_verdict.get("equivalent") is True:
                agreement_level = AgreementLevel.CONSENSUS
                final_answer = (
                    f"Convergence across {len(last_turns)} participants "
                    f"(wording differed; judge {judge_verdict.get('judge', '?')} ruled "
                    f"positions substantively equivalent):\n\n"
                    + "\n\n".join(f"- {t.agent}: {t.position}" for t in last_turns)
                )
            else:
                final_answer = (
                    f"Weak convergence across {len(last_turns)} participants. "
                    "All signaled done; positions diverge:\n\n"
                    + "\n\n".join(f"- {t.agent}: {t.position}" for t in last_turns)
                )
        else:
            agreement_level = AgreementLevel.UNRESOLVED
            final_answer = (
                "Conclave terminated without full convergence. Last-round positions:\n\n"
                + "\n\n".join(
                    f"- {t.agent} ({t.convergence.value}): {t.position}" for t in last_turns
                )
            )
        status = TaskStatus.COMPLETED if status_label == "completed" else TaskStatus.FAILED

    # Conclave produces no commands/patches in MVP (participants don't have recommended_actions).
    return FinalResult(
        protocol_version="1.0",
        task_id=task_id,
        status=status,
        mode=task.mode,
        primary_agent=None,
        consultants=task.consultants,
        final_answer=final_answer,
        agreement_level=agreement_level,
        resolution_status=None,
        disagreements=[],
        recommended_actions=[],
        commands_requiring_approval=[],
        patches_requiring_approval=[],
        risks=[],
        errors=errors,
        confidence_aggregate=_compute_confidence_aggregate(last_turns),
    )


async def _run_convergence_judge(
    task: TaskRequest,
    task_id: str,
    last_turns: list[ConclaveTurn],
) -> Optional[dict]:
    """Invoke one available participant as a semantic-equivalence judge.

    Returns the judge verdict dict, or None if no judge could be picked /
    nothing to judge. The verdict is also persisted as a synthetic
    'judge_verdict' message so it's visible in the transcript and the
    dashboard, separate from the regular conclave turns.
    """
    positions = [{"agent": t.agent, "position": t.position} for t in last_turns]
    if len(positions) < 2:
        return None

    # Pick the first available participant as judge.
    judge_adapter = None
    for name in task.consultants or []:
        try:
            judge_adapter = agent_registry.get(name)
            break
        except KeyError:
            continue
    if judge_adapter is None:
        return None

    verdict = await judge_convergence(positions, task, task_id, judge_adapter)
    _record_message(
        task_id=task_id,
        agent_run_id=None,
        agent_name=judge_adapter.name,
        role="judge",
        message_type=_SyntheticType("judge_verdict"),
        direction="from_agent",
        content=(
            f"Judge {judge_adapter.name} ruled positions "
            f"{'equivalent' if verdict.get('equivalent') else 'not equivalent'}: "
            f"{verdict.get('reasoning', '')}"
        ),
        structured=verdict,
    )
    return verdict


def _normalize(s: str) -> str:
    return " ".join(s.lower().split())


# ---------------------------------------------------------------------------
# Final-result assembly (shared by consult and resolve)
# ---------------------------------------------------------------------------

def _assemble_final(
    *,
    task: TaskRequest,
    task_id: str,
    primary_resp: Optional[PrimaryResponse],
    critiques: list[ConsultantCritique],
    errors: list[ProtocolError],
    resolution_status: Optional[ResolutionStatus],
) -> FinalResult:
    if primary_resp:
        final_answer = f"{primary_resp.summary}\n\n{primary_resp.analysis}"
        actions = primary_resp.recommended_actions
        risks = primary_resp.risks
    else:
        final_answer = "(no final answer; see errors)"
        actions = []
        risks = []

    if resolution_status == ResolutionStatus.CANNOT_RESOLVE and primary_resp:
        final_answer = "Cannot resolve.\n\n" + final_answer
        agreement_level = AgreementLevel.UNRESOLVED
        status = TaskStatus.COMPLETED
    elif resolution_status == ResolutionStatus.RESOLVED or (resolution_status is None and primary_resp):
        agreement_level = _agreement_to_level(critiques)
        status = TaskStatus.COMPLETED
    else:
        agreement_level = AgreementLevel.UNRESOLVED
        status = TaskStatus.FAILED

    commands: list[str] = []
    patches: list[str] = []
    for action in actions:
        if action.kind == "run_command" and isinstance(action.payload.get("command"), str):
            commands.append(action.payload["command"])
        if action.kind == "apply_patch" and isinstance(action.payload.get("patch"), str):
            patches.append(action.payload["patch"])

    return FinalResult(
        protocol_version="1.0",
        task_id=task_id,
        status=status,
        mode=task.mode,
        primary_agent=task.primary_agent,
        consultants=task.consultants,
        final_answer=final_answer,
        agreement_level=agreement_level,
        resolution_status=resolution_status,
        disagreements=_build_disagreements(critiques, primary_resp),
        recommended_actions=actions,
        commands_requiring_approval=commands,
        patches_requiring_approval=patches,
        risks=risks,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------

def _load_task(task_id: str) -> Optional[TaskRequest]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    # parent_task_id column was added as a migration; older rows may not have it.
    try:
        parent_id = row["parent_task_id"]
    except (IndexError, KeyError):
        parent_id = None
    return TaskRequest(
        protocol_version="1.0",
        source=row["source"],
        source_agent=row["source_agent"],
        mode=row["mode"],
        task_type=row["task_type"],
        user_request=row["user_request"],
        primary_agent=row["primary_agent"],
        consultants=json.loads(row["consultants"]),
        project_path=row["project_path"],
        context=json.loads(row["context_json"]),
        permissions=json.loads(row["permissions_json"]),
        limits=json.loads(row["limits_json"]),
        parent_task_id=parent_id,
    )


_MAX_ANCESTRY_DEPTH = 5


def _load_thread_ancestors(task_id: str) -> list[dict]:
    """
    Walk up the parent_task_id chain starting from task_id's parent.
    Returns ancestor summaries (oldest first), capped at _MAX_ANCESTRY_DEPTH,
    cycle-safe.
    """
    chain: list[dict] = []
    visited: set[str] = set()
    with connect() as conn:
        # Start at the task's parent (the task itself is not its own ancestor).
        row = conn.execute(
            "SELECT parent_task_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return []
        current = row["parent_task_id"]
        while current and len(chain) < _MAX_ANCESTRY_DEPTH:
            if current in visited:
                break
            visited.add(current)
            anc = conn.execute(
                """SELECT t.id, t.mode, t.user_request, t.user_decision, t.user_decided_at,
                          t.parent_task_id, fr.final_answer, fr.agreement_level
                   FROM tasks t LEFT JOIN final_results fr ON t.id = fr.task_id
                   WHERE t.id = ?""",
                (current,),
            ).fetchone()
            if anc is None:
                break
            chain.append({
                "id": anc["id"],
                "mode": anc["mode"],
                "user_request": anc["user_request"],
                "final_answer": anc["final_answer"],
                "agreement_level": anc["agreement_level"],
                "user_decision": anc["user_decision"],
                "user_decided_at": anc["user_decided_at"],
            })
            current = anc["parent_task_id"]
    chain.reverse()
    return chain


def _load_prior_messages(task_id: str) -> list[dict]:
    """Reconstruct the message history for a task. Used to resume resolve tasks."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT message_type, role, agent_name, content, structured_json
               FROM agent_messages WHERE task_id = ? ORDER BY created_at""",
            (task_id,),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        if r["structured_json"]:
            out.append(json.loads(r["structured_json"]))
        else:
            out.append({
                "agent": r["agent_name"],
                "role": r["role"],
                "message_type": r["message_type"],
                "content": r["content"],
            })
    return out


async def run_task(task_id: str) -> None:
    """Top-level entry. Loads the task, dispatches to mode-specific flow, persists result."""
    task = _load_task(task_id)
    if task is None:
        return

    # If this task is part of a thread, attach the ancestry to context.extra so
    # the prompt builder can surface it. The orchestrator does the DB walk so
    # the prompt builder stays DB-unaware.
    if task.parent_task_id:
        ancestors = _load_thread_ancestors(task_id)
        if ancestors:
            task.context.extra["thread_ancestors"] = ancestors

    # If the task requested a project sandbox, prepare it (or reuse an existing
    # one for a resumed task). Path is stashed on context.extra so adapters can
    # find it and so the prompt builder can render the manifest.
    if (task.context.extra.get("include_sandbox")
            and task.project_path
            and task.permissions.can_read_files):
        sandbox = prepare_sandbox(task.project_path, task_id, task.permissions)
        if sandbox is not None:
            task.context.extra["sandbox_path"] = str(sandbox.resolve())

    _set_task_status(task_id, TaskStatus.RUNNING)
    try:
        if task.mode == TaskMode.RESOLVE:
            prior = _load_prior_messages(task_id)
            result = await run_resolve(task, task_id, prior_messages=prior)
            if result is None:
                # paused for user input; status already set to AWAITING_USER_INPUT
                return
        elif task.mode == TaskMode.CONCLAVE:
            prior = _load_prior_messages(task_id)
            result = await run_conclave(task, task_id, prior_messages=prior)
            if result is None:
                return  # paused for user input
        elif task.mode == TaskMode.CONSULT:
            result = await run_consult(task, task_id)
        else:
            # handoff and poll deferred to v0.2 per MVP_PLAN.md
            result = FinalResult(
                protocol_version="1.0",
                task_id=task_id,
                status=TaskStatus.FAILED,
                mode=task.mode,
                primary_agent=task.primary_agent,
                consultants=task.consultants,
                final_answer="(mode not implemented in MVP)",
                agreement_level=AgreementLevel.UNRESOLVED,
                errors=[ProtocolError(
                    code=ErrorCode.INVALID_REQUEST,
                    message=f"Mode {task.mode.value} is not implemented in MVP.",
                )],
            )
        _save_final_result(task_id, result)
        _set_task_status(task_id, result.status)
        # If the task carried a sandbox and reached a terminal status, clean up.
        if result.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            if task.context.extra.get("sandbox_path"):
                cleanup_sandbox(task_id)
    except Exception as e:  # noqa: BLE001
        _set_task_status(task_id, TaskStatus.FAILED, error_message=str(e))
        # Best-effort cleanup on unexpected failure
        if task.context.extra.get("sandbox_path"):
            cleanup_sandbox(task_id)
        raise
