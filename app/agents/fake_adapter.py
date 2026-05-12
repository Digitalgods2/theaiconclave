"""Fake adapter — returns canned, deterministic responses for testing.

Behavior is steered by `task.context.extra.fake_behavior`:

Consult-mode behaviors (legacy, still supported):
  - "normal" (default): standard happy-path responses
  - "timeout":   run_primary raises AdapterError(agent_timeout)
  - "loop":      consultant returns "I have nothing to add" with agreement=AGREE

Resolve-mode behaviors:
  - "resolve_immediately":      primary returns RESOLVED on first call
  - "resolve_after_one_round":  primary returns NEEDS_MORE_ROUNDS, then RESOLVED
  - "ask_then_resolve":         primary returns NEEDS_USER_INPUT, then RESOLVED after user answers
  - "cannot_resolve":           primary returns CANNOT_RESOLVE
  - "loop_forever":             primary returns identical content with NEEDS_MORE_ROUNDS (exercises repetition guard)
  - "consultant_blocks":        primary RESOLVED but consultant has wants_continuation=True for one round, then accepts
"""

from __future__ import annotations

import time

from app.agents.base import (
    AdapterContext,
    AdapterError,
    AdapterTestResult,
    BaseAdapter,
)
from app.protocol.validators import (
    Agreement,
    AgentRole,
    ConclaveConvergence,
    ConclaveTurn,
    ConsultantCritique,
    ErrorCode,
    MessageType,
    PeerAnswer,
    PrimaryResponse,
    RecommendedAction,
    ResolutionStatus,
    Risk,
    RiskSeverity,
)


def _count_primary_proposals(prior: list[dict]) -> int:
    return sum(
        1 for m in prior
        if m.get("message_type") == MessageType.PRIMARY_PROPOSAL.value
    )


def _has_user_response(prior: list[dict]) -> bool:
    return any(
        m.get("message_type") == MessageType.USER_INPUT_RESPONSE.value
        for m in prior
    )


class FakeAdapter(BaseAdapter):
    name = "fake"
    internal = True  # hidden from /api/agents; reachable only via direct registry use (tests)

    def __init__(self) -> None:
        super().__init__()

    async def is_available(self) -> bool:
        return True

    async def test_connection(self) -> AdapterTestResult:
        start = time.perf_counter()
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return AdapterTestResult(
            available=True,
            version="0.0.0-fake",
            elapsed_ms=elapsed_ms,
        )

    @staticmethod
    def _behavior(ctx: AdapterContext) -> str:
        return ctx.task.context.extra.get("fake_behavior", "normal")

    async def run_primary(self, ctx: AdapterContext) -> PrimaryResponse:
        behavior = self._behavior(ctx)
        n_prior = _count_primary_proposals(ctx.prior_messages)

        if behavior == "timeout":
            raise AdapterError(
                ErrorCode.AGENT_TIMEOUT,
                "Fake adapter simulated timeout.",
            )

        # ----- Resolve-mode behaviors -----

        if behavior == "resolve_immediately":
            return self._build_primary(
                ctx,
                summary="Fake primary: resolved immediately.",
                analysis="Resolved on first attempt without consultant input.",
                resolution_status=ResolutionStatus.RESOLVED,
                confidence=0.85,
            )

        if behavior == "resolve_after_one_round":
            if n_prior == 0:
                return self._build_primary(
                    ctx,
                    summary="Fake primary: working on it.",
                    analysis="Initial proposal. I want one more round to refine.",
                    resolution_status=ResolutionStatus.NEEDS_MORE_ROUNDS,
                    confidence=0.5,
                )
            return self._build_primary(
                ctx,
                summary="Fake primary: resolved after one critique cycle.",
                analysis="Refined the proposal after consultant feedback. Done.",
                resolution_status=ResolutionStatus.RESOLVED,
                confidence=0.85,
            )

        if behavior == "ask_then_resolve":
            if not _has_user_response(ctx.prior_messages):
                return self._build_primary(
                    ctx,
                    summary="Fake primary: I need more information.",
                    analysis="Pausing to ask the user a clarifying question.",
                    resolution_status=ResolutionStatus.NEEDS_USER_INPUT,
                    user_input_question="What is the exact error message you're seeing?",
                    confidence=0.4,
                )
            return self._build_primary(
                ctx,
                summary="Fake primary: resolved with user input.",
                analysis="Got the user's answer. Proposing the resolution.",
                resolution_status=ResolutionStatus.RESOLVED,
                confidence=0.9,
            )

        if behavior == "cannot_resolve":
            return self._build_primary(
                ctx,
                summary="Fake primary: cannot resolve.",
                analysis=(
                    "I do not have enough information or the right tools to solve this. "
                    "Specifically: (this is a fake reason)."
                ),
                resolution_status=ResolutionStatus.CANNOT_RESOLVE,
                confidence=0.2,
            )

        if behavior == "loop_forever":
            return self._build_primary(
                ctx,
                summary="The same summary every round.",
                analysis="The same analysis every round. The repetition guard should fire.",
                resolution_status=ResolutionStatus.NEEDS_MORE_ROUNDS,
                confidence=0.5,
            )

        if behavior == "consultant_blocks":
            # Primary always says resolved; consultant decides via wants_continuation
            return self._build_primary(
                ctx,
                summary=f"Fake primary round {n_prior + 1}: I think we're done.",
                analysis="Primary believes the answer is complete; consultant may disagree.",
                resolution_status=ResolutionStatus.RESOLVED,
                confidence=0.7,
            )

        if behavior == "conclave_diverge":
            # Used only by conclave-mode tests of the synthesis-round trigger.
            # In primary/resolve mode, behave as resolve_immediately.
            return self._build_primary(
                ctx,
                summary="Fake primary (diverge fallback in resolve mode).",
                analysis="Diverge behavior maps to immediate resolution in non-conclave modes.",
                resolution_status=ResolutionStatus.RESOLVED,
                confidence=0.7,
            )

        # ----- Default consult-mode behavior -----

        return self._build_primary(
            ctx,
            summary=f"Fake primary proposal for: {ctx.task.user_request[:80]}",
            analysis=(
                "This is a deterministic fake response. A real primary agent would analyze "
                "the request and propose a concrete approach grounded in the supplied context."
            ),
            resolution_status=None,
            confidence=0.5,
        )

    async def run_consultant(self, ctx: AdapterContext) -> ConsultantCritique:
        behavior = self._behavior(ctx)

        if behavior == "loop":
            return self._build_critique(
                ctx,
                agreement=Agreement.AGREE,
                critique="I have nothing to add.",
                wants_continuation=False,
            )

        if behavior == "consultant_blocks":
            # First round: block. Subsequent rounds: accept.
            n_prior = _count_primary_proposals(ctx.prior_messages)
            if n_prior <= 1:
                return self._build_critique(
                    ctx,
                    agreement=Agreement.PARTIAL,
                    critique="I want one more round before we declare this resolved.",
                    wants_continuation=True,
                )
            return self._build_critique(
                ctx,
                agreement=Agreement.AGREE,
                critique="Acceptable now.",
                wants_continuation=False,
            )

        # Default: partial agreement, no continuation request
        return self._build_critique(
            ctx,
            agreement=Agreement.PARTIAL,
            critique=(
                "The fake primary's proposal is too generic. It should name specific "
                "files or commands rather than recommending unspecified verification."
            ),
            wants_continuation=False,
        )

    async def run_final(self, ctx: AdapterContext) -> PrimaryResponse:
        return PrimaryResponse(
            protocol_version="1.0",
            task_id=ctx.task_id,
            agent=self.name,
            role=AgentRole.PRIMARY,
            message_type=MessageType.PRIMARY_FINAL,
            summary=(
                f"Fake final answer for: {ctx.task.user_request[:80]}. "
                "Refining the approach after consultant review."
            ),
            analysis=(
                "Accepting the consultant's point about specificity. A real primary would "
                "now propose concrete file paths and commands grounded in the supplied context."
            ),
            recommended_actions=[
                RecommendedAction(
                    kind="verify",
                    description="Inspect the error message and identify the failing component.",
                    requires_approval=False,
                    payload={"step": 1},
                )
            ],
            risks=[
                Risk(
                    severity=RiskSeverity.LOW,
                    description="Fake adapter — answer not derived from real reasoning.",
                )
            ],
            confidence=0.7,
        )

    async def run_conclave_turn(self, ctx: AdapterContext) -> ConclaveTurn:
        """
        Conclave behavior modes:
          - "conclave_quick"     — converge immediately with the SAME position from every agent
          - "conclave_iterate"   — still_thinking on round 1, i_am_done on round 2
          - "conclave_holdout"   — still_thinking on rounds 1-2, i_am_done on round 3
          - "conclave_ask"       — need_user_input on round 1, i_am_done after answer
          - "conclave_diverge"   — i_am_done on round 1 with agent-specific positions (exercises synthesis trigger)
          - default              — i_am_done on round 1
        """
        behavior = self._behavior(ctx)
        n_prior_turns_by_self = sum(
            1 for m in ctx.prior_messages
            if m.get("message_type") == MessageType.CONCLAVE_TURN.value
            and m.get("agent") == self.name
        )

        convergence = ConclaveConvergence.I_AM_DONE
        position = "Default fake position: pathlib for new code."
        question = None

        if behavior == "conclave_diverge":
            convergence = ConclaveConvergence.I_AM_DONE
            position = f"Position-{self.name}: my distinct answer (round {n_prior_turns_by_self + 1})."

        if behavior == "conclave_iterate":
            # Identical position across agents so iteration-mode tests don't
            # spuriously trigger the synthesis round.
            if n_prior_turns_by_self == 0:
                convergence = ConclaveConvergence.STILL_THINKING
                position = "Round-1 position: leaning toward pathlib."
            else:
                convergence = ConclaveConvergence.I_AM_DONE
                position = "Round-2 position: pathlib confirmed."
        elif behavior == "conclave_holdout":
            if n_prior_turns_by_self < 2:
                convergence = ConclaveConvergence.STILL_THINKING
                position = f"Round-{n_prior_turns_by_self + 1} position: still considering."
            else:
                convergence = ConclaveConvergence.I_AM_DONE
                position = "Final position: pathlib."
        elif behavior == "conclave_ask":
            has_answer = any(
                m.get("message_type") == MessageType.USER_INPUT_RESPONSE.value
                for m in ctx.prior_messages
            )
            if not has_answer:
                convergence = ConclaveConvergence.NEED_USER_INPUT
                question = "Will this code run on Python 3.6 or older?"
                position = "Cannot commit without knowing the Python version target."
            else:
                convergence = ConclaveConvergence.I_AM_DONE
                position = "Given the user's clarification, pathlib is correct."

        return ConclaveTurn(
            protocol_version="1.0",
            task_id=ctx.task_id,
            agent=self.name,
            role=AgentRole.PARTICIPANT,
            message_type=MessageType.CONCLAVE_TURN,
            summary=f"{self.name} round-{n_prior_turns_by_self + 1} contribution.",
            analysis=f"Fake conclave reasoning from {self.name}, behavior={behavior}.",
            position=position,
            convergence=convergence,
            user_input_question=question,
            confidence=0.7,
        )

    async def run_peer(self, ctx: AdapterContext) -> PeerAnswer:
        return PeerAnswer(
            protocol_version="1.0",
            task_id=ctx.task_id,
            agent=self.name,
            role=AgentRole.PEER,
            message_type=MessageType.PEER_ANSWER,
            summary=f"Fake peer answer for: {ctx.task.user_request[:80]}",
            analysis="Independent fake response, no critique loop.",
            recommended_actions=[],
            risks=[],
            confidence=0.5,
        )

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    def _build_primary(
        self,
        ctx: AdapterContext,
        *,
        summary: str,
        analysis: str,
        resolution_status,
        user_input_question: str | None = None,
        confidence: float | None = None,
    ) -> PrimaryResponse:
        return PrimaryResponse(
            protocol_version="1.0",
            task_id=ctx.task_id,
            agent=self.name,
            role=AgentRole.PRIMARY,
            message_type=MessageType.PRIMARY_PROPOSAL,
            summary=summary,
            analysis=analysis,
            recommended_actions=[
                RecommendedAction(
                    kind="verify",
                    description="Manually verify the symptom described in the user request.",
                    requires_approval=False,
                    payload={"step": 1},
                )
            ],
            risks=[
                Risk(
                    severity=RiskSeverity.LOW,
                    description="The fake adapter does not perform real analysis.",
                )
            ],
            confidence=confidence,
            resolution_status=resolution_status,
            user_input_question=user_input_question,
        )

    def _build_critique(
        self,
        ctx: AdapterContext,
        *,
        agreement: Agreement,
        critique: str,
        wants_continuation: bool,
    ) -> ConsultantCritique:
        return ConsultantCritique(
            protocol_version="1.0",
            task_id=ctx.task_id,
            agent=self.name,
            role=AgentRole.CONSULTANT,
            message_type=MessageType.CONSULTANT_CRITIQUE,
            agreement=agreement,
            critique=critique,
            missed_risks=[],
            suggested_questions=[],
            confidence=0.6,
            wants_continuation=wants_continuation,
        )
