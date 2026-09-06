"""
Base adapter interface.

Every AI tool adapter inherits from BaseAdapter. The orchestrator and the
agent registry never call adapters by name — they always go through this
interface. Per-tool quirks live inside the adapter; nothing else needs to
know about them.

See docs/AGENT_ADAPTERS.md for the full contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextvars import ContextVar
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.protocol.validators import (
    ConclaveTurn,
    ConsultantCritique,
    ErrorCode,
    Permissions,
    PrimaryResponse,
    TaskRequest,
)


class AdapterContext(BaseModel):
    """Everything an adapter needs for one call. Read-only by convention."""

    task: TaskRequest
    task_id: str
    prior_messages: list[dict[str, Any]] = Field(default_factory=list)
    permissions: Permissions
    # Legacy requests may carry a timeout threshold for UI notification, but
    # active calls are user-cancelled rather than failed on elapsed wall time.
    timeout_seconds: Optional[int] = None
    working_directory: str

    model_config = ConfigDict(frozen=True)


class AdapterTestResult(BaseModel):
    available: bool
    version: Optional[str] = None
    error: Optional[str] = None
    elapsed_ms: int


class Readiness(BaseModel):
    """Structured readiness signal for one seat. Surfaced via `/api/health` so
    the dashboard can show *why* a seat is unavailable and how to fix it.

    `reason` is a short machine-stable token (e.g. "ok", "command_not_found",
    "configured_path_missing", "api_key_missing"). `hint` is user-facing
    remediation text — what to install, where to authenticate, etc.
    """
    available: bool
    reason: str
    hint: str = ""


class AdapterError(Exception):
    """
    Raised by adapters when an agent call fails. The orchestrator converts
    this to a ProtocolError on the task. Adapters do not retry.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def __repr__(self) -> str:
        return f"AdapterError(code={self.code.value}, message={self.message!r})"


class BaseAdapter(ABC):
    """
    Adapter interface. Subclasses implement per-tool behavior.

    Instances may be long-lived (e.g. holding an HTTP client pool) but must
    be safe for concurrent use across tasks.
    """

    name: str = ""  # canonical agent name; subclass MUST set
    internal: bool = False  # True = hidden from user-facing endpoints (tests-only adapters)
    max_context_chars: int = 800_000  # conservative ~200K-token equivalent; subclasses override

    def __init__(self) -> None:
        # Context-local telemetry preserves the existing adapter contract while
        # making a long-lived adapter safe when conclave turns run in parallel.
        self._usage_var: ContextVar[Optional[dict[str, Any]]] = ContextVar(
            f"{type(self).__name__}.usage.{id(self)}", default=None
        )
        self._tool_events_var: ContextVar[Optional[list[dict[str, Any]]]] = ContextVar(
            f"{type(self).__name__}.tool_events.{id(self)}", default=None
        )
        self._prompt_var: ContextVar[Optional[str]] = ContextVar(
            f"{type(self).__name__}.prompt.{id(self)}", default=None
        )
        self._raw_response_var: ContextVar[Optional[str]] = ContextVar(
            f"{type(self).__name__}.raw_response.{id(self)}", default=None
        )

    @property
    def _last_usage(self) -> dict[str, Any]:
        var = getattr(self, "_usage_var", None)
        if var is None:
            var = ContextVar(f"{type(self).__name__}.usage.{id(self)}", default=None)
            self._usage_var = var
        value = var.get()
        if value is None:
            value = {}
            var.set(value)
        return value

    @_last_usage.setter
    def _last_usage(self, value: dict[str, Any]) -> None:
        var = getattr(self, "_usage_var", None)
        if var is None:
            var = ContextVar(f"{type(self).__name__}.usage.{id(self)}", default=None)
            self._usage_var = var
        var.set(dict(value or {}))

    @property
    def _last_tool_events(self) -> list[dict[str, Any]]:
        var = getattr(self, "_tool_events_var", None)
        if var is None:
            var = ContextVar(f"{type(self).__name__}.tool_events.{id(self)}", default=None)
            self._tool_events_var = var
        value = var.get()
        if value is None:
            value = []
            var.set(value)
        return value

    @_last_tool_events.setter
    def _last_tool_events(self, value: list[dict[str, Any]]) -> None:
        var = getattr(self, "_tool_events_var", None)
        if var is None:
            var = ContextVar(f"{type(self).__name__}.tool_events.{id(self)}", default=None)
            self._tool_events_var = var
        var.set(list(value or []))

    @property
    def _last_prompt(self) -> Optional[str]:
        var = getattr(self, "_prompt_var", None)
        return var.get() if var is not None else None

    @_last_prompt.setter
    def _last_prompt(self, value: Optional[str]) -> None:
        var = getattr(self, "_prompt_var", None)
        if var is None:
            var = ContextVar(f"{type(self).__name__}.prompt.{id(self)}", default=None)
            self._prompt_var = var
        var.set(value)

    @property
    def _last_raw_response(self) -> Optional[str]:
        var = getattr(self, "_raw_response_var", None)
        return var.get() if var is not None else None

    @_last_raw_response.setter
    def _last_raw_response(self, value: Optional[str]) -> None:
        var = getattr(self, "_raw_response_var", None)
        if var is None:
            var = ContextVar(f"{type(self).__name__}.raw_response.{id(self)}", default=None)
            self._raw_response_var = var
        var.set(value)

    @abstractmethod
    async def is_available(self) -> bool:
        """Cheap liveness check used by the registry on startup and on demand."""

    async def readiness(self) -> Readiness:
        """Structured readiness — reason + hint as well as the boolean.

        Default implementation wraps `is_available()` with generic text so
        subclasses get useful behavior without an override. Subclasses that
        can distinguish "not on PATH" from "configured path doesn't exist"
        from "API key missing" should override this for actionable hints.
        """
        ok = await self.is_available()
        return Readiness(
            available=ok,
            reason="ok" if ok else "unavailable",
            hint="" if ok else f"Seat '{self.name}' is not available.",
        )

    @abstractmethod
    async def test_connection(self) -> AdapterTestResult:
        """Side-effect-free probe. Reports version and elapsed time."""

    async def _invoke(self, prompt: str, timeout_seconds: Optional[int]) -> str:
        """Low-level text call used only by neutral judge/synthesis roles.

        Production adapters override this. The default keeps test-only adapters
        source-compatible while failing clearly if selected as a neutral seat.
        """
        raise NotImplementedError(f"adapter {self.name!r} does not support neutral text invocation")

    @abstractmethod
    async def run_primary(self, ctx: AdapterContext) -> PrimaryResponse:
        """Initial proposal as primary. Returns message_type=primary_proposal."""

    @abstractmethod
    async def run_consultant(self, ctx: AdapterContext) -> ConsultantCritique:
        """Critique a prior primary proposal. Returns message_type=consultant_critique."""

    @abstractmethod
    async def run_final(self, ctx: AdapterContext) -> PrimaryResponse:
        """Final response after consultant critique. Returns message_type=primary_final."""

    @abstractmethod
    async def run_conclave_turn(self, ctx: AdapterContext) -> ConclaveTurn:
        """One participant's contribution in a single conclave round. Returns message_type=conclave_turn."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"


__all__ = [
    "AdapterContext",
    "AdapterTestResult",
    "AdapterError",
    "BaseAdapter",
    "Readiness",
]
