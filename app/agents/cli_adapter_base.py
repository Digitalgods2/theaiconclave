"""Template-method base for CLI-subprocess-backed seats.

Factors out the four `run_*` methods, which were byte-identical across
`codex`, `claude-code`, `gemini`, and `antigravity` — 0 differing lines out of
92 once each adapter's own name was substituted. Adding a fifth CLI seat used
to mean copying all 92 lines again, and a protocol change meant editing four
files identically, which is exactly the ripple `CLAUDE.md` warns about.

Subclasses supply only:
  - `name` and `max_context_chars` (standard `BaseAdapter` attributes)
  - `_invoke()` — the entire per-CLI surface: argv construction, subprocess
    spawning, stdout parsing, usage extraction

NOT for `OpenRouterAdapter`. Its run_* methods look similar but differ in
every mechanical detail: `_invoke` takes no `image_paths` (no CLI-native
image support), calls route through `_invoke_dispatch` to reach the DR0015
tool-loop rather than calling `_invoke` directly, `include_sandbox_manifest`
stays at its default of True (per DR0018, API seats have no file tools and
need the manifest inlined), and `ceiling_chars` comes from a *method* that
learns a tighter ceiling at runtime, not a class attribute. Forcing it in
would mean adding hooks for "supports images?", "needs a manifest?" and "is
the ceiling a property or a call" — at which point the template method stops
being a template.

NOT for `FakeAdapter` either, which is a different kind of object: no
subprocess, no prompt building, no JSON parsing. It returns hand-built models
keyed off `fake_behavior` to drive orchestrator state-machine tests.
"""

from __future__ import annotations

from abc import abstractmethod
from pathlib import Path
from typing import Optional

from app.agents._response_coercion import parse_and_coerce
from app.agents.base import AdapterContext, BaseAdapter
from app.protocol.validators import (
    ConclaveTurn,
    ConsultantCritique,
    MessageType,
    PrimaryResponse,
)
from app.services.prompt_builder import (
    build_conclave_prompt,
    build_consultant_prompt,
    build_final_prompt,
    build_primary_prompt,
)
from app.utils.attachments import image_attachment_paths


class CliAdapterBase(BaseAdapter):
    """Shared deliberation flow for adapters that shell out to a CLI."""

    @abstractmethod
    async def _invoke(
        self,
        prompt: str,
        timeout_seconds: int,
        image_paths: Optional[list[Path]] = None,
        sandbox_path: Optional[str] = None,
    ) -> str:
        """Run one turn against the CLI and return the agent's raw text.

        Implementations must set `self._last_usage` and must raise
        `AdapterError` on failure — never return partial or empty output for a
        run that did not succeed.
        """

    async def _run(
        self,
        ctx: AdapterContext,
        prompt: str,
        *,
        role: str,
        default_message_type: str,
    ) -> dict:
        text = await self._invoke(
            prompt, ctx.timeout_seconds,
            image_attachment_paths(ctx.task),
            ctx.task.context.extra.get("sandbox_path"),
        )
        return parse_and_coerce(
            text, ctx.task_id, self.name,
            role=role, default_message_type=default_message_type,
        )

    # ------------------------------------------------------------------
    # `include_sandbox_manifest=False` throughout: CLI seats browse the
    # sandbox with their own file tools, so inlining a manifest would burn
    # context to tell them what they can already see (DR0018).
    # ------------------------------------------------------------------

    async def run_primary(self, ctx: AdapterContext) -> PrimaryResponse:
        prompt = build_primary_prompt(
            task=ctx.task,
            task_id=ctx.task_id,
            agent_name=self.name,
            prior_messages=ctx.prior_messages,
            ceiling_chars=self.max_context_chars,
            include_sandbox_manifest=False,
        )
        data = await self._run(
            ctx, prompt,
            role="primary",
            default_message_type=MessageType.PRIMARY_PROPOSAL.value,
        )
        return PrimaryResponse.model_validate(data)

    async def run_consultant(self, ctx: AdapterContext) -> ConsultantCritique:
        prompt = build_consultant_prompt(
            task=ctx.task,
            task_id=ctx.task_id,
            agent_name=self.name,
            prior_messages=ctx.prior_messages,
            ceiling_chars=self.max_context_chars,
            include_sandbox_manifest=False,
        )
        data = await self._run(
            ctx, prompt,
            role="consultant",
            default_message_type=MessageType.CONSULTANT_CRITIQUE.value,
        )
        return ConsultantCritique.model_validate(data)

    async def run_final(self, ctx: AdapterContext) -> PrimaryResponse:
        prompt = build_final_prompt(
            task=ctx.task,
            task_id=ctx.task_id,
            agent_name=self.name,
            prior_messages=ctx.prior_messages,
            ceiling_chars=self.max_context_chars,
            include_sandbox_manifest=False,
        )
        data = await self._run(
            ctx, prompt,
            role="primary",
            default_message_type=MessageType.PRIMARY_FINAL.value,
        )
        return PrimaryResponse.model_validate(data)

    async def run_conclave_turn(self, ctx: AdapterContext) -> ConclaveTurn:
        others = [c for c in ctx.task.consultants if c != self.name]
        prompt = build_conclave_prompt(
            task=ctx.task,
            task_id=ctx.task_id,
            agent_name=self.name,
            prior_messages=ctx.prior_messages,
            other_participants=others,
        )
        data = await self._run(
            ctx, prompt,
            role="participant",
            default_message_type=MessageType.CONCLAVE_TURN.value,
        )
        return ConclaveTurn.model_validate(data)


__all__ = ["CliAdapterBase"]
