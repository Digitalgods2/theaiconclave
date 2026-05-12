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
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.protocol.validators import (
    ConclaveTurn,
    ConsultantCritique,
    ErrorCode,
    PeerAnswer,
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
    timeout_seconds: int
    working_directory: str

    model_config = ConfigDict(frozen=True)


class AdapterTestResult(BaseModel):
    available: bool
    version: Optional[str] = None
    error: Optional[str] = None
    elapsed_ms: int


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
        # Per-call usage stash, populated by _invoke and read by the orchestrator
        # so we can persist (input_tokens, output_tokens, cost_usd) on agent_runs
        # without restructuring every run_* method signature.
        self._last_usage: dict[str, Any] = {}

    @abstractmethod
    async def is_available(self) -> bool:
        """Cheap liveness check used by the registry on startup and on demand."""

    @abstractmethod
    async def test_connection(self) -> AdapterTestResult:
        """Side-effect-free probe. Reports version and elapsed time."""

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
    async def run_peer(self, ctx: AdapterContext) -> PeerAnswer:
        """Independent answer in poll mode. Returns message_type=peer_answer."""

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
]
