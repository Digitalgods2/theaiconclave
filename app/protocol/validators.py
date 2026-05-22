"""
Switchboard Protocol v1.0 schema validators.

Pydantic models defining the wire format for every message that flows through
Switchboard. FastAPI uses these for request validation; the orchestrator uses
them to validate agent responses before persistence.

Source of truth for field semantics: docs/SWITCHBOARD_PROTOCOL.md.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


PROTOCOL_VERSION = "1.1"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"          # action approval
    AWAITING_USER_INPUT = "awaiting_user_input"    # info needed from user (resolve mode)
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskMode(str, Enum):
    RESOLVE = "resolve"     # open-ended, primary-driven, goal-based termination
    CONSULT = "consult"     # bounded second opinion
    CONCLAVE = "conclave"   # N equal participants, full-mesh visibility, convergence termination
    HANDOFF = "handoff"     # named agent becomes primary
    POLL = "poll"           # parallel independent answers, no iteration


class AgentRole(str, Enum):
    PRIMARY = "primary"
    CONSULTANT = "consultant"
    PEER = "peer"
    PARTICIPANT = "participant"   # conclave mode — equal voice, no primary/consultant asymmetry


class MessageType(str, Enum):
    PRIMARY_PROPOSAL = "primary_proposal"
    CONSULTANT_CRITIQUE = "consultant_critique"
    PRIMARY_FINAL = "primary_final"
    PEER_ANSWER = "peer_answer"
    CONCLAVE_TURN = "conclave_turn"               # one participant's contribution per round
    USER_INPUT_REQUEST = "user_input_request"
    USER_INPUT_RESPONSE = "user_input_response"
    ERROR = "error"
    # DR0015: tool-loop architecture for API-based council seats. The OpenRouter
    # adapter emits these inside a single agent_run to record the agent's
    # iterative file-reading. Direction: TOOL_CALL is from_agent, TOOL_RESULT
    # is to_agent.
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"


class TaskType(str, Enum):
    DEBUG = "debug"
    CODE_REVIEW = "code_review"
    ARCHITECTURE_REVIEW = "architecture_review"
    SECURITY_REVIEW = "security_review"
    DEPLOYMENT_HELP = "deployment_help"
    DOCUMENTATION = "documentation"
    GENERAL_CONSULTATION = "general_consultation"


class TaskSource(str, Enum):
    DASHBOARD = "dashboard"
    API = "api"
    WEBHOOK = "webhook"
    CLI = "cli"
    WATCHER = "watcher"


class Agreement(str, Enum):
    AGREE = "agree"
    PARTIAL = "partial"
    DISAGREE = "disagree"


class AgreementLevel(str, Enum):
    CONSENSUS = "consensus"
    MINOR_DISAGREEMENT = "minor_disagreement"
    MAJOR_DISAGREEMENT = "major_disagreement"
    UNRESOLVED = "unresolved"


class ResolutionStatus(str, Enum):
    """Primary's signal in resolve mode about whether the task is done."""
    RESOLVED = "resolved"                      # I have a solid final answer
    NEEDS_MORE_ROUNDS = "needs_more_rounds"    # I want to keep iterating
    NEEDS_USER_INPUT = "needs_user_input"      # I cannot proceed without info from the user
    CANNOT_RESOLVE = "cannot_resolve"          # I cannot solve this with what's available


class ConclaveConvergence(str, Enum):
    """Participant's signal in conclave mode about whether the deliberation is done."""
    I_AM_DONE = "i_am_done"                    # I have nothing more to add; happy with the group's direction
    STILL_THINKING = "still_thinking"          # I want another round
    NEED_USER_INPUT = "need_user_input"        # I cannot continue without info from the user


class RiskSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ErrorCode(str, Enum):
    PROTOCOL_VERSION_MISMATCH = "protocol_version_mismatch"
    AGENT_UNAVAILABLE = "agent_unavailable"
    AGENT_TIMEOUT = "agent_timeout"
    AGENT_ERROR = "agent_error"
    PERMISSION_DENIED = "permission_denied"
    APPROVAL_REQUIRED = "approval_required"
    ROUNDS_EXHAUSTED = "rounds_exhausted"
    LOOP_DETECTED = "loop_detected"
    INVALID_REQUEST = "invalid_request"
    RESOLVE_TIMEOUT = "resolve_timeout"        # max_seconds ceiling hit


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ActionType(str, Enum):
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    RUN_COMMAND = "run_command"
    INSTALL_PACKAGE = "install_package"
    APPLY_PATCH = "apply_patch"
    NETWORK_ACCESS = "network_access"
    DEPLOYMENT_CHANGE = "deployment_change"
    SECRET_ACCESS = "secret_access"
    HUMAN_DECISION = "human_decision"
    UNKNOWN = "unknown"


class PolicyStatus(str, Enum):
    ALLOWED = "allowed"
    NEEDS_APPROVAL = "needs_approval"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Composable types
# ---------------------------------------------------------------------------

class Permissions(BaseModel):
    can_read_files: bool
    can_write_files: bool
    can_run_commands: bool
    can_access_network: bool
    can_install_packages: bool
    can_apply_patches: bool
    can_read_env_files: bool
    can_read_secrets: bool


class Limits(BaseModel):
    max_rounds: int = Field(ge=1, le=200)               # backstop in resolve/conclave; primary cap in consult
    timeout_seconds: int = Field(ge=10, le=3600)        # per agent call
    max_seconds: Optional[int] = Field(default=None, ge=10, le=86400)   # total task time (resolve/conclave)
    max_context_tokens: Optional[int] = Field(default=None, ge=100)
    convergence_threshold: float = Field(default=1.0, ge=0.5, le=1.0)   # conclave: fraction of participants who must signal i_am_done


class Risk(BaseModel):
    severity: RiskSeverity
    description: str


class RecommendedAction(BaseModel):
    kind: str
    description: str
    requires_approval: bool
    payload: dict[str, Any] = Field(default_factory=dict)


class ActionPlanStep(BaseModel):
    step_number: int = Field(ge=1)
    action_type: ActionType
    summary: str
    target: Optional[str] = None
    source_action_kind: Optional[str] = None
    required_permissions: list[str] = Field(default_factory=list)
    policy_status: PolicyStatus
    policy_reasons: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskContext(BaseModel):
    files: list[str] = Field(default_factory=list)
    error: Optional[str] = None
    git_diff: Optional[str] = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ProtocolError(BaseModel):
    code: ErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class Disagreement(BaseModel):
    topic: str
    primary_position: str
    consultant_position: str


# ---------------------------------------------------------------------------
# Top-level messages
# ---------------------------------------------------------------------------

class TaskRequest(BaseModel):
    protocol_version: str
    source: TaskSource
    source_agent: Optional[str] = None
    mode: TaskMode
    task_type: TaskType
    user_request: str = Field(min_length=1)
    primary_agent: Optional[str] = None
    consultants: list[str] = Field(default_factory=list)
    project_path: Optional[str] = None
    context: TaskContext = Field(default_factory=TaskContext)
    permissions: Permissions
    limits: Limits
    parent_task_id: Optional[str] = None

    @model_validator(mode="after")
    def _check_version(self) -> "TaskRequest":
        client_major = self.protocol_version.split(".", 1)[0]
        server_major = PROTOCOL_VERSION.split(".", 1)[0]
        if client_major != server_major:
            raise ValueError(
                f"protocol_version_mismatch: client sent {self.protocol_version}, "
                f"server requires {server_major}.x"
            )
        return self

    @model_validator(mode="after")
    def _check_mode_requirements(self) -> "TaskRequest":
        # primary_agent required for resolve, consult, handoff
        if self.mode in (TaskMode.RESOLVE, TaskMode.CONSULT, TaskMode.HANDOFF):
            if not self.primary_agent:
                raise ValueError(f"primary_agent is required when mode={self.mode.value}")
        # consult requires non-empty consultants
        if self.mode == TaskMode.CONSULT and not self.consultants:
            raise ValueError("consultants must be non-empty when mode=consult")
        # poll requires no primary and at least 2 consultants
        if self.mode == TaskMode.POLL:
            if self.primary_agent is not None:
                raise ValueError("primary_agent must be omitted when mode=poll")
            if len(self.consultants) < 2:
                raise ValueError("poll mode requires at least 2 consultants")
        # conclave requires no primary and at least 2 participants (in `consultants` list)
        if self.mode == TaskMode.CONCLAVE:
            if self.primary_agent is not None:
                raise ValueError("primary_agent must be omitted when mode=conclave")
            if len(self.consultants) < 2:
                raise ValueError("conclave mode requires at least 2 participants (in consultants[])")
        return self

    @model_validator(mode="after")
    def _check_install_implies_others(self) -> "TaskRequest":
        # SAFETY_MODEL.md §1: can_install_packages implies can_run_commands and can_access_network.
        p = self.permissions
        if p.can_install_packages and not (p.can_run_commands and p.can_access_network):
            raise ValueError(
                "can_install_packages requires can_run_commands and can_access_network"
            )
        return self


class PrimaryResponse(BaseModel):
    protocol_version: str
    task_id: str
    agent: str
    role: Literal[AgentRole.PRIMARY]
    message_type: Literal[MessageType.PRIMARY_PROPOSAL, MessageType.PRIMARY_FINAL]
    summary: str
    analysis: str
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    # Resolve mode additions; optional in consult mode.
    resolution_status: Optional[ResolutionStatus] = None
    user_input_question: Optional[str] = None

    @model_validator(mode="after")
    def _check_user_input_question(self) -> "PrimaryResponse":
        if self.resolution_status == ResolutionStatus.NEEDS_USER_INPUT:
            if not self.user_input_question:
                raise ValueError(
                    "user_input_question is required when resolution_status=needs_user_input"
                )
        return self


class ConsultantCritique(BaseModel):
    protocol_version: str
    task_id: str
    agent: str
    role: Literal[AgentRole.CONSULTANT]
    message_type: Literal[MessageType.CONSULTANT_CRITIQUE]
    agreement: Agreement
    critique: str
    missed_risks: list[str] = Field(default_factory=list)
    suggested_questions: list[str] = Field(default_factory=list)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    # Resolve mode addition: does this consultant believe another round would help?
    wants_continuation: bool = False


class PeerAnswer(BaseModel):
    protocol_version: str
    task_id: str
    agent: str
    role: Literal[AgentRole.PEER]
    message_type: Literal[MessageType.PEER_ANSWER]
    summary: str
    analysis: str
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class ConclaveTurn(BaseModel):
    """A single participant's contribution in one conclave round."""
    protocol_version: str
    task_id: str
    agent: str
    role: Literal[AgentRole.PARTICIPANT]
    message_type: Literal[MessageType.CONCLAVE_TURN]
    summary: str
    analysis: str
    position: str  # what this participant would tell the user RIGHT NOW if forced to commit
    convergence: ConclaveConvergence
    user_input_question: Optional[str] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_user_input_question(self) -> "ConclaveTurn":
        if self.convergence == ConclaveConvergence.NEED_USER_INPUT:
            if not self.user_input_question:
                raise ValueError(
                    "user_input_question is required when convergence=need_user_input"
                )
        return self


class FinalResult(BaseModel):
    protocol_version: str
    task_id: str
    status: TaskStatus
    mode: TaskMode
    primary_agent: Optional[str] = None
    consultants: list[str] = Field(default_factory=list)
    final_answer: str
    agreement_level: AgreementLevel
    resolution_status: Optional[ResolutionStatus] = None  # populated in resolve mode
    disagreements: list[Disagreement] = Field(default_factory=list)
    action_plan: list[ActionPlanStep] = Field(default_factory=list)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    commands_requiring_approval: list[str] = Field(default_factory=list)
    patches_requiring_approval: list[str] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    errors: list[ProtocolError] = Field(default_factory=list)
    # Confidence aggregate across the last-round participants (conclave mode only):
    # {"min": 0.65, "max": 0.95, "mean": 0.81, "count": 4, "missing_count": 0}.
    # None for non-conclave modes or when no participant emitted a confidence score.
    confidence_aggregate: Optional[dict[str, Any]] = None


class Approval(BaseModel):
    id: str
    task_id: str
    approval_type: str
    description: str
    payload: dict[str, Any]
    status: ApprovalStatus
    created_at: str
    resolved_at: Optional[str] = None
    resolution_note: Optional[str] = None


__all__ = [
    "PROTOCOL_VERSION",
    "TaskStatus",
    "TaskMode",
    "AgentRole",
    "MessageType",
    "TaskType",
    "TaskSource",
    "Agreement",
    "AgreementLevel",
    "ResolutionStatus",
    "ConclaveConvergence",
    "RiskSeverity",
    "ErrorCode",
    "ApprovalStatus",
    "ActionType",
    "PolicyStatus",
    "Permissions",
    "Limits",
    "Risk",
    "RecommendedAction",
    "ActionPlanStep",
    "TaskContext",
    "ProtocolError",
    "Disagreement",
    "TaskRequest",
    "PrimaryResponse",
    "ConsultantCritique",
    "PeerAnswer",
    "ConclaveTurn",
    "FinalResult",
    "Approval",
]
