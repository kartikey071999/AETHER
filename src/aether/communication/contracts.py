"""Typed inter-agent payloads and routing rules: the integration contract between agents.

Every `MessageType` has exactly one payload model in `PAYLOAD_MODELS`. `AgentMessage.payload`
stays a plain JSON dict on the wire and in the database; agents validate it with
`validate_payload` and check who may send/receive it with `check_route`. The message bus itself
stays generic and enforces neither.
"""

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Self
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.security.approvals import DecisionOutcome
from aether.security.errors import BlockReason
from aether.security.operations import Operation, canonical_json
from aether.storage.models import (
    CapabilityStatus,
    FindingClassification,
    ResultOutcome,
    RunStatus,
    TaskStatus,
)

# --------------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------------


class ContractViolation(ValueError):
    """A message does not satisfy the inter-agent contract."""


class UnknownMessageType(ContractViolation):
    def __init__(self, message_type: object) -> None:
        self.message_type = message_type
        super().__init__(f"no payload contract is registered for message type {message_type!r}")


class InvalidPayload(ContractViolation):
    def __init__(self, message_type: MessageType, detail: str) -> None:
        self.message_type = message_type
        self.detail = detail
        super().__init__(f"invalid {message_type.value!r} payload: {detail}")


class RouteViolation(ContractViolation):
    def __init__(self, message_type: MessageType, source: AgentId, target: AgentId, why: str):
        self.message_type = message_type
        self.source_agent = source
        self.target_agent = target
        super().__init__(
            f"{source.value} -> {target.value} is not a permitted route for "
            f"{message_type.value!r}: {why}"
        )


# --------------------------------------------------------------------------------------------
# Field types
# --------------------------------------------------------------------------------------------


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _plain_json(value: dict[str, Any]) -> dict[str, Any]:
    # A JSON round trip rejects non-JSON values and detaches the caller's mutable objects.
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as error:
        raise ValueError("value must be plain JSON data") from error


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
"""Timezone-aware datetime, normalised to UTC; naive datetimes are rejected."""

JsonDict = Annotated[dict[str, Any], AfterValidator(_plain_json)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Priority = Annotated[int, Field(ge=0, le=100)]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


def _utc_now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------------------------
# Enumerations owned by the contract
# --------------------------------------------------------------------------------------------


class ObjectiveState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DEFERRED = "deferred"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class ObjectiveRejectionReason(StrEnum):
    RATE_LIMIT = "rate_limit"
    ACTIVE_LIMIT = "active_limit"
    MISSING_COST = "missing_cost"
    INVALID = "invalid"


class ResourceDecision(StrEnum):
    GRANTED = "granted"
    DEFERRED = "deferred"
    DENIED = "denied"


class ResearchMode(StrEnum):
    CONVENTIONAL = "conventional"
    UNCONVENTIONAL = "unconventional"


class EvidenceTrust(StrEnum):
    UNTRUSTED = "untrusted"
    CORROBORATED = "corroborated"
    VERIFIED = "verified"


class ConsolidationDecision(StrEnum):
    ADOPT = "adopt"
    REJECT = "reject"
    NEEDS_EVIDENCE = "needs_evidence"
    DELEGATE_ROGUE = "delegate_rogue"


class RequirementType(StrEnum):
    CAPABILITY = "capability"
    DEPENDENCY = "dependency"
    IMPLEMENTATION = "implementation"
    SECURITY = "security"
    ACCEPTANCE = "acceptance"


class EvaluationOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_EVIDENCE = "needs_evidence"
    DEFERRED = "deferred"


class TaskOutcome(StrEnum):
    """Mirrors `aether.agents.base.TaskResultStatus` without importing the agent layer."""

    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    WAITING_FOR_APPROVAL = "waiting_for_approval"


class VerificationVerdict(StrEnum):
    VERIFIED = "verified"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class CapabilityVerification(StrEnum):
    UNVERIFIED = "unverified"
    TESTED = "tested"
    VERIFIED = "verified"
    FAILED = "failed"


class BlockerKind(StrEnum):
    RESEARCH = "research"
    IMPLEMENTATION = "implementation"
    SECURITY = "security"
    RESOURCE = "resource"
    AUTHORIZATION = "authorization"


class UserDecision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    NONE = "none"


class Urgency(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class HealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


# --------------------------------------------------------------------------------------------
# Shared building blocks
# --------------------------------------------------------------------------------------------


class ContractModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Payload(ContractModel):
    """Base of every message payload."""

    def check_envelope(self, message: AgentMessage) -> None:
        """Reject payloads whose IDs contradict the envelope; subclasses add role checks."""
        for name in ("objective_id", "task_id", "requirement_id"):
            expected = getattr(message, name)
            actual = getattr(self, name, None)
            if expected is not None and actual is not None and expected != actual:
                raise ValueError(f"payload {name} does not match the message envelope")


class ResourceEstimate(ContractModel):
    ram_mb: int = Field(default=0, ge=0)
    disk_mb: int = Field(default=0, ge=0)
    cpu_percent: float = Field(default=0.0, ge=0.0, le=100.0)
    duration_s: int | None = Field(default=None, ge=0)


class ResearchBudget(ContractModel):
    max_llm_requests: int = Field(ge=0)
    max_sources: int = Field(ge=0)


class EvidenceItem(ContractModel):
    source_uri: Text
    summary: str = ""
    content_hash: str | None = Field(default=None, max_length=128)
    trust: EvidenceTrust = EvidenceTrust.UNTRUSTED


class CandidateApproach(ContractModel):
    name: Text
    description: str = ""
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    resource_needs: ResourceEstimate | None = None
    risks: list[str] = Field(default_factory=list)


class AcceptanceResult(ContractModel):
    criterion: Text
    passed: bool
    evidence: str = ""


class PlanStep(ContractModel):
    tool: Text
    arguments: JsonDict = Field(default_factory=dict)
    purpose: Text


class FindingDecision(ContractModel):
    finding_id: UUID
    decision: ConsolidationDecision
    rationale: Text
    follow_up: str | None = None


class TestCounts(ContractModel):
    __test__ = False

    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)


class TestCaseResult(ContractModel):
    __test__ = False

    name: Text
    outcome: ResultOutcome
    duration_ms: int | None = Field(default=None, ge=0)
    message: str | None = None


class ResourceSnapshot(ContractModel):
    cpu_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    ram_available_mb: int | None = Field(default=None, ge=0)
    disk_free_mb: int | None = Field(default=None, ge=0)
    running_tasks: int | None = Field(default=None, ge=0)


# --------------------------------------------------------------------------------------------
# Objectives
# --------------------------------------------------------------------------------------------


class ObjectiveCreatePayload(Payload):
    title: Title
    description: str = ""
    priority: Priority = 50
    completion_criteria: list[Text] = Field(default_factory=list)
    rationale: str | None = None
    expected_benefit: str | None = None
    estimated_cost: float | None = Field(default=None, ge=0.0)
    stopping_conditions: list[Text] = Field(default_factory=list)
    deadline: UtcDatetime | None = None
    review_at: UtcDatetime | None = None
    generated: bool = False
    parent_objective_id: UUID | None = None

    @model_validator(mode="after")
    def _bound_generated_objectives(self) -> Self:
        # Agent-generated objectives must be bounded so the objective queue cannot grow forever.
        if self.generated:
            if not self.rationale:
                raise ValueError("agent-generated objectives require a rationale")
            if not self.stopping_conditions:
                raise ValueError("agent-generated objectives require stopping conditions")
            if not self.completion_criteria:
                raise ValueError("agent-generated objectives require completion criteria")
        return self


class ObjectiveUpdatePayload(Payload):
    objective_id: UUID
    reason: Text
    status: ObjectiveState | None = None
    priority: Priority | None = None
    description: str | None = None
    completion_criteria: list[Text] | None = None
    stopping_conditions: list[Text] | None = None
    deadline: UtcDatetime | None = None
    review_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _require_change(self) -> Self:
        if self.status is ObjectiveState.COMPLETED:
            raise ValueError("objectives are completed only via objective.completed")
        changes = (
            self.status,
            self.priority,
            self.description,
            self.completion_criteria,
            self.stopping_conditions,
            self.deadline,
            self.review_at,
        )
        if all(value is None for value in changes):
            raise ValueError("objective.update must change at least one field")
        return self


class ObjectiveCompletedPayload(Payload):
    objective_id: UUID
    summary: Text
    criteria_results: list[AcceptanceResult] = Field(min_length=1)
    evidence: list[JsonDict] = Field(default_factory=list)

    @model_validator(mode="after")
    def _all_criteria_met(self) -> Self:
        # An objective is complete only when its defined completion criteria have been met.
        if not all(result.passed for result in self.criteria_results):
            raise ValueError("every completion criterion must pass before completion")
        return self


class ObjectiveRejectedPayload(Payload):
    request_message_id: UUID
    generating_agent: AgentId
    reason: ObjectiveRejectionReason
    detail: Text
    objective_id: UUID | None = None

    def check_envelope(self, message: AgentMessage) -> None:
        super().check_envelope(message)
        if self.generating_agent is not message.target_agent:
            raise ValueError("objective.rejected must be addressed to the generating agent")


# --------------------------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------------------------


class TaskCreatePayload(Payload):
    objective_id: UUID
    title: Title
    description: str = ""
    parent_task_id: UUID | None = None
    requirement_id: UUID | None = None
    suggested_agent: AgentId | None = None
    acceptance_criteria: list[Text] = Field(default_factory=list)
    depends_on: list[UUID] = Field(default_factory=list)
    priority: Priority = 50
    resource_estimate: ResourceEstimate = Field(default_factory=ResourceEstimate)
    interruptible: bool = True
    deadline: UtcDatetime | None = None
    rationale: str | None = None


class TaskAssignPayload(Payload):
    task_id: UUID
    objective_id: UUID
    title: Title
    description: str = ""
    assigned_agent: AgentId
    acceptance_criteria: list[Text] = Field(default_factory=list)
    requirement_ids: list[UUID] = Field(default_factory=list)
    depends_on: list[UUID] = Field(default_factory=list)
    priority: Priority = 50
    resource_estimate: ResourceEstimate = Field(default_factory=ResourceEstimate)
    interruptible: bool = True
    deadline: UtcDatetime | None = None

    def check_envelope(self, message: AgentMessage) -> None:
        super().check_envelope(message)
        if self.assigned_agent is not message.target_agent:
            raise ValueError("task.assign must be addressed to the assigned agent")


class TaskTransitionPayload(Payload):
    task_id: UUID
    from_state: TaskStatus | None = None
    to_state: TaskStatus
    reason: Text


class TaskResultPayload(Payload):
    task_id: UUID
    status: TaskOutcome
    summary: str = ""
    outputs: JsonDict = Field(default_factory=dict)
    evidence: list[JsonDict] = Field(default_factory=list)
    error: str | None = None
    retryable: bool = True

    @model_validator(mode="after")
    def _failures_explain_themselves(self) -> Self:
        if self.status in (TaskOutcome.FAILED, TaskOutcome.BLOCKED) and not self.error:
            raise ValueError(f"{self.status.value} task results require an error")
        return self


# --------------------------------------------------------------------------------------------
# Research and requirements
# --------------------------------------------------------------------------------------------

_RESEARCH_AGENTS = frozenset({AgentId.SCOUT, AgentId.ROGUE})


class ResearchRequestPayload(Payload):
    objective_id: UUID
    question: Text
    scope: str = ""
    constraints: list[str] = Field(default_factory=list)
    budget: ResearchBudget
    review_by: UtcDatetime | None = None
    mode: ResearchMode = ResearchMode.CONVENTIONAL
    task_id: UUID | None = None


class ResearchFindingPayload(Payload):
    finding_id: UUID = Field(default_factory=uuid4)
    objective_id: UUID | None = None
    task_id: UUID | None = None
    question: Text
    summary: Text
    candidate_approaches: list[CandidateApproach] = Field(default_factory=list)
    recommendation: str | None = None
    confidence: Confidence
    classification: FindingClassification
    evidence: list[EvidenceItem] = Field(default_factory=list)
    security_considerations: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    submitted_by: AgentId

    @model_validator(mode="after")
    def _check_finding(self) -> Self:
        if self.submitted_by not in _RESEARCH_AGENTS:
            raise ValueError("findings are submitted by SCOUT or ROGUE")
        if self.classification is FindingClassification.VERIFIED and not self.evidence:
            raise ValueError("verified findings require supporting evidence")
        return self

    def check_envelope(self, message: AgentMessage) -> None:
        super().check_envelope(message)
        if self.submitted_by is not message.source_agent:
            raise ValueError("submitted_by must be the sending agent")


class ResearchConsolidatedPayload(Payload):
    objective_id: UUID | None = None
    summary: str = ""
    decisions: list[FindingDecision] = Field(min_length=1)
    requirement_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_decision_per_finding(self) -> Self:
        finding_ids = [decision.finding_id for decision in self.decisions]
        if len(set(finding_ids)) != len(finding_ids):
            raise ValueError("each finding may be decided only once per consolidation")
        return self


class RequirementSubmitPayload(Payload):
    requirement_id: UUID | None = None
    objective_id: UUID
    task_id: UUID | None = None
    title: Title
    description: str = ""
    requirement_type: RequirementType
    acceptance_criteria: list[Text] = Field(default_factory=list)
    source_finding_id: UUID | None = None
    security_review_required: bool = False
    priority: Priority = 50
    rationale: str | None = None
    expected_benefit: str | None = None


class RequirementEvaluatedPayload(Payload):
    requirement_id: UUID
    objective_id: UUID
    title: Title
    requirement_type: RequirementType
    acceptance_criteria: list[Text] = Field(default_factory=list)
    source_finding_id: UUID | None = None
    security_review_required: bool = False
    priority: Priority | None = None
    outcome: EvaluationOutcome
    rationale: Text

    @model_validator(mode="after")
    def _approved_requirements_are_testable(self) -> Self:
        if self.outcome is EvaluationOutcome.APPROVED and not self.acceptance_criteria:
            raise ValueError("approved requirements must define acceptance criteria")
        return self


# --------------------------------------------------------------------------------------------
# Security and authorization
# --------------------------------------------------------------------------------------------


class SecurityEvaluatePayload(Payload):
    operation: Operation
    input_hash: Sha256Hex
    requirement_id: UUID | None = None
    evidence: list[JsonDict] = Field(default_factory=list)
    requested_by: AgentId | None = None
    task_id: UUID | None = None
    block_reason: BlockReason | None = None

    @model_validator(mode="after")
    def _bind_to_operation(self) -> Self:
        # WARDEN's decision binds to the hash; it must be the hash of exactly this operation.
        if self.input_hash != self.operation.input_hash:
            raise ValueError("input_hash does not match the operation under review")
        if self.requested_by is not None and self.requested_by is not self.operation.requested_by:
            raise ValueError("requested_by does not match the operation")
        if self.requested_by is AgentId.WARDEN or self.operation.requested_by is AgentId.WARDEN:
            raise ValueError("WARDEN cannot request review of its own operation")
        pairs = (
            (self.task_id, self.operation.task_id),
            (self.requirement_id, self.operation.declared_requirement_id),
        )
        if any(a is not None and b is not None and a != b for a, b in pairs):
            raise ValueError("task/requirement IDs do not match the operation")
        return self

    def check_envelope(self, message: AgentMessage) -> None:
        super().check_envelope(message)
        if self.operation.requested_by is not message.source_agent:
            raise ValueError("only the requesting agent may submit its operation for review")


class SecurityDecisionPayload(Payload):
    decision_id: UUID
    decision: DecisionOutcome
    input_hash: Sha256Hex
    operation: str = ""
    scope: JsonDict = Field(default_factory=dict)
    evidence: list[JsonDict] = Field(default_factory=list)
    restrictions: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    version_constraint: str | None = None
    expires_at: UtcDatetime | None = None
    rationale: Text


class AuthorizationRequestPayload(Payload):
    authorization_request_id: UUID
    question: Text
    operation_summary: Text
    expires_at: UtcDatetime | None = None
    security_decision_id: UUID | None = None
    input_hash: Sha256Hex | None = None
    objective_id: UUID | None = None
    task_id: UUID | None = None


class UserResponsePayload(Payload):
    """An explicit user reply. Silence is never authorization: only `approved` grants it."""

    authorization_request_id: UUID | None = None
    objective_id: UUID | None = None
    task_id: UUID | None = None
    text: str = ""
    decision: UserDecision = UserDecision.NONE
    responded_at: UtcDatetime = Field(default_factory=_utc_now)

    @model_validator(mode="after")
    def _associate_response(self) -> Self:
        if (
            self.authorization_request_id is None
            and self.objective_id is None
            and self.task_id is None
        ):
            raise ValueError("a user response must reference an authorization, objective or task")
        if self.decision is not UserDecision.NONE and self.authorization_request_id is None:
            raise ValueError("approve/deny decisions must reference an authorization request")
        return self

    @property
    def grants_authorization(self) -> bool:
        return self.decision is UserDecision.APPROVED and self.authorization_request_id is not None


def authorization_granted(
    response: UserResponsePayload | None, authorization_request_id: UUID
) -> bool:
    """True only for an explicit approval of this request; a missing response never approves."""
    return (
        response is not None
        and response.grants_authorization
        and response.authorization_request_id == authorization_request_id
    )


# --------------------------------------------------------------------------------------------
# Resources, implementation, execution, verification
# --------------------------------------------------------------------------------------------


class ResourceRequestPayload(Payload):
    task_id: UUID | None = None
    resources: ResourceEstimate
    download_mb: int = Field(default=0, ge=0)
    llm_requests: int = Field(default=0, ge=0)
    reason: Text
    deadline: UtcDatetime | None = None


class ResourceResponsePayload(Payload):
    request_message_id: UUID
    requested_by: AgentId
    decision: ResourceDecision
    reason: Text
    retry_after: float | None = Field(default=None, ge=0, description="Retry delay in seconds")
    task_id: UUID | None = None

    @model_validator(mode="after")
    def _retry_only_when_deferred(self) -> Self:
        if self.retry_after is not None and self.decision is not ResourceDecision.DEFERRED:
            raise ValueError("retry_after is only valid for deferred resource requests")
        return self

    def check_envelope(self, message: AgentMessage) -> None:
        super().check_envelope(message)
        if self.requested_by is not message.target_agent:
            raise ValueError("resource.response must be addressed to the requester")


class PromotionRequestPayload(Payload):
    """Evidence for review, not permission to deploy or execute the workspace."""

    task_id: UUID
    objective_id: UUID | None = None
    requirement_id: UUID | None = None
    workspace_path: Text
    artifacts: list[Text] = Field(min_length=1)
    verification_evidence: list[JsonDict] = Field(min_length=1)


class ImplementationPlanPayload(Payload):
    plan_id: UUID = Field(default_factory=uuid4)
    requirement_id: UUID
    task_id: UUID | None = None
    steps: list[PlanStep] = Field(min_length=1)
    rollback_steps: list[PlanStep] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    resource_estimate: ResourceEstimate
    acceptance_criteria: list[Text] = Field(min_length=1)


class ExecutionRequestPayload(Payload):
    """Everything FORGE must hold before executing (spec section 7, execution requirements)."""

    task_id: UUID
    objective_id: UUID | None = None
    requirement_id: UUID | None = None
    plan_id: UUID
    steps: list[PlanStep] = Field(min_length=1)
    rollback_steps: list[PlanStep] = Field(default_factory=list)
    acceptance_criteria: list[Text] = Field(min_length=1)
    depends_on: list[UUID] = Field(default_factory=list)
    resource_estimate: ResourceEstimate
    security_decision_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def _linked_work(self) -> Self:
        if self.objective_id is None and self.requirement_id is None:
            raise ValueError("execution requires a linked objective or requirement")
        return self


_TERMINAL_RUN_STATUSES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})


class ExecutionResultPayload(Payload):
    task_id: UUID
    requirement_id: UUID | None = None
    execution_run_ids: list[UUID] = Field(default_factory=list)
    status: RunStatus
    outputs: JsonDict = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    evidence: list[JsonDict] = Field(default_factory=list)
    error: str | None = None

    @model_validator(mode="after")
    def _terminal_status(self) -> Self:
        if self.status not in _TERMINAL_RUN_STATUSES:
            raise ValueError("execution results report a terminal status")
        if self.status is RunStatus.FAILED and not self.error:
            raise ValueError("failed executions require an error")
        return self


class VerificationRequestPayload(Payload):
    task_id: UUID | None = None
    capability_id: UUID | None = None
    requirement_id: UUID | None = None
    suite: Text
    acceptance_criteria: list[Text] = Field(min_length=1)
    execution_run_ids: list[UUID] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    evidence: list[JsonDict] = Field(default_factory=list)

    @model_validator(mode="after")
    def _subject(self) -> Self:
        if self.capability_id is None and self.requirement_id is None:
            raise ValueError("verification requires a capability or requirement to verify")
        return self


class TestReportPayload(Payload):
    __test__ = False

    test_run_id: UUID
    task_id: UUID | None = None
    capability_id: UUID | None = None
    requirement_id: UUID | None = None
    suite: Text
    results: TestCounts
    cases: list[TestCaseResult] = Field(default_factory=list)
    acceptance_results: list[AcceptanceResult] = Field(default_factory=list)
    verdict: VerificationVerdict
    summary: str = ""

    @model_validator(mode="after")
    def _verdict_matches_results(self) -> Self:
        failing = self.results.failed + self.results.errors > 0 or any(
            not result.passed for result in self.acceptance_results
        )
        # A capability is never verified by assertion alone: criteria must pass with evidence.
        if self.verdict is VerificationVerdict.VERIFIED and (
            not self.acceptance_results or failing
        ):
            raise ValueError("verified reports need passing acceptance results and no failures")
        if self.verdict is VerificationVerdict.FAILED and not failing:
            raise ValueError("failed reports must contain a failing test or criterion")
        return self


class CapabilityRegisterPayload(Payload):
    capability_id: UUID = Field(default_factory=uuid4)
    name: Title
    description: str = ""
    lifecycle_status: CapabilityStatus
    provider: str | None = None
    version: str | None = Field(default=None, max_length=64)
    entrypoint: str | None = Field(default=None, max_length=500)
    input_schema: JsonDict = Field(default_factory=dict)
    output_schema: JsonDict = Field(default_factory=dict)
    required_permissions: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    resource_estimate: ResourceEstimate = Field(default_factory=ResourceEstimate)
    verification_status: CapabilityVerification = CapabilityVerification.UNVERIFIED
    verification_evidence: list[JsonDict] = Field(default_factory=list)
    test_run_ids: list[UUID] = Field(default_factory=list)
    installation: JsonDict = Field(default_factory=dict)
    known_limitations: list[str] = Field(default_factory=list)
    requirement_id: UUID | None = None
    objective_id: UUID | None = None

    @model_validator(mode="after")
    def _never_register_untested_as_available(self) -> Self:
        tested = (CapabilityVerification.TESTED, CapabilityVerification.VERIFIED)
        if self.verification_status in tested and not (
            self.verification_evidence or self.test_run_ids
        ):
            raise ValueError("tested or verified capabilities require verification evidence")
        if (
            self.lifecycle_status is CapabilityStatus.VERIFIED
            and self.verification_status is not CapabilityVerification.VERIFIED
        ):
            raise ValueError("a capability is registered verified only with verified status")
        if self.lifecycle_status is CapabilityStatus.TESTED and (
            self.verification_status not in tested
        ):
            raise ValueError("a tested capability requires tested verification status")
        return self


# --------------------------------------------------------------------------------------------
# Blockers, progress, health, communication
# --------------------------------------------------------------------------------------------

BLOCKER_ROUTES: dict[BlockerKind, frozenset[AgentId]] = {
    BlockerKind.RESEARCH: frozenset({AgentId.SCOUT, AgentId.ROGUE}),
    BlockerKind.IMPLEMENTATION: frozenset({AgentId.FORGE}),
    BlockerKind.SECURITY: frozenset({AgentId.WARDEN}),
    BlockerKind.RESOURCE: frozenset(),
    BlockerKind.AUTHORIZATION: frozenset({AgentId.MUSE}),
}
"""Which agent may resolve each blocker kind; ORCHESTR may always take an escalation."""


class BlockerPayload(Payload):
    blocker_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    objective_id: UUID | None = None
    kind: BlockerKind
    description: Text
    route_to: AgentId
    attempts: int = Field(default=0, ge=0)
    evidence: list[JsonDict] = Field(default_factory=list)

    @model_validator(mode="after")
    def _route_matches_kind(self) -> Self:
        allowed = BLOCKER_ROUTES[self.kind] | {AgentId.ORCHESTR}
        if self.route_to not in allowed:
            raise ValueError(f"{self.kind.value} blockers are routed to {sorted(allowed)}")
        return self

    def check_envelope(self, message: AgentMessage) -> None:
        super().check_envelope(message)
        if (
            message.target_agent is not AgentId.ORCHESTR
            and message.target_agent is not self.route_to
        ):
            raise ValueError("blockers must target their resolver or escalate to ORCHESTR")


class ProgressUpdatePayload(Payload):
    task_id: UUID | None = None
    objective_id: UUID | None = None
    summary: Text
    percent_complete: float | None = Field(default=None, ge=0.0, le=100.0)
    state: TaskStatus | None = None
    details: JsonDict = Field(default_factory=dict)


class HealthEventPayload(Payload):
    component: Text
    agent_id: AgentId | None = None
    status: HealthState
    detail: str = ""
    resources: ResourceSnapshot | None = None
    observed_at: UtcDatetime = Field(default_factory=_utc_now)


class CommunicationRequestPayload(Payload):
    """ORCHESTR's structured handoff to MUSE (spec section 10, communication handoff)."""

    what_happened: Text
    results: list[JsonDict] = Field(default_factory=list)
    verified: list[str] = Field(default_factory=list)
    uncertain: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    decisions_required: list[str] = Field(default_factory=list)
    authorization_required: bool = False
    authorization_request_id: UUID | None = None
    audience: UUID | None = None
    urgency: Urgency = Urgency.NORMAL
    personalization_context: JsonDict = Field(default_factory=dict)
    objective_id: UUID | None = None
    task_id: UUID | None = None

    @model_validator(mode="after")
    def _authorization_is_traceable(self) -> Self:
        if self.authorization_required and self.authorization_request_id is None:
            raise ValueError("authorization requests must reference an authorization_request_id")
        return self


class CommunicationDeliveredPayload(Payload):
    interaction_id: UUID
    channel: Text
    rendered_text_hash: Sha256Hex
    audience: UUID | None = None
    request_message_id: UUID | None = None
    delivered_at: UtcDatetime = Field(default_factory=_utc_now)


# --------------------------------------------------------------------------------------------
# Registries
# --------------------------------------------------------------------------------------------

PAYLOAD_MODELS: dict[MessageType, type[Payload]] = {
    MessageType.OBJECTIVE_CREATE: ObjectiveCreatePayload,
    MessageType.OBJECTIVE_UPDATE: ObjectiveUpdatePayload,
    MessageType.OBJECTIVE_COMPLETED: ObjectiveCompletedPayload,
    MessageType.OBJECTIVE_REJECTED: ObjectiveRejectedPayload,
    MessageType.TASK_CREATE: TaskCreatePayload,
    MessageType.TASK_ASSIGN: TaskAssignPayload,
    MessageType.TASK_TRANSITION: TaskTransitionPayload,
    MessageType.TASK_RESULT: TaskResultPayload,
    MessageType.RESEARCH_REQUEST: ResearchRequestPayload,
    MessageType.RESEARCH_FINDING: ResearchFindingPayload,
    MessageType.RESEARCH_CONSOLIDATED: ResearchConsolidatedPayload,
    MessageType.REQUIREMENT_SUBMIT: RequirementSubmitPayload,
    MessageType.REQUIREMENT_EVALUATED: RequirementEvaluatedPayload,
    MessageType.SECURITY_EVALUATE: SecurityEvaluatePayload,
    MessageType.SECURITY_DECISION: SecurityDecisionPayload,
    MessageType.AUTHORIZATION_REQUEST: AuthorizationRequestPayload,
    MessageType.USER_RESPONSE: UserResponsePayload,
    MessageType.RESOURCE_REQUEST: ResourceRequestPayload,
    MessageType.RESOURCE_RESPONSE: ResourceResponsePayload,
    MessageType.PROMOTION_REQUEST: PromotionRequestPayload,
    MessageType.IMPLEMENTATION_PLAN: ImplementationPlanPayload,
    MessageType.EXECUTION_REQUEST: ExecutionRequestPayload,
    MessageType.EXECUTION_RESULT: ExecutionResultPayload,
    MessageType.VERIFICATION_REQUEST: VerificationRequestPayload,
    MessageType.TEST_REPORT: TestReportPayload,
    MessageType.CAPABILITY_REGISTER: CapabilityRegisterPayload,
    MessageType.BLOCKER: BlockerPayload,
    MessageType.PROGRESS_UPDATE: ProgressUpdatePayload,
    MessageType.HEALTH_EVENT: HealthEventPayload,
    MessageType.COMMUNICATION_REQUEST: CommunicationRequestPayload,
    MessageType.COMMUNICATION_DELIVERED: CommunicationDeliveredPayload,
}

MESSAGE_TYPE_BY_MODEL: dict[type[Payload], MessageType] = {
    model: message_type for message_type, model in PAYLOAD_MODELS.items()
}

_ALL = frozenset(AgentId)
_WORKERS = _ALL - {AgentId.ORCHESTR}

ROUTES: dict[MessageType, frozenset[AgentId]] = {
    MessageType.OBJECTIVE_CREATE: frozenset({AgentId.ORCHESTR}),
    MessageType.OBJECTIVE_UPDATE: frozenset(
        {AgentId.ORCHESTR, AgentId.SCOUT, AgentId.ROGUE, AgentId.LEDGER}
    ),
    MessageType.OBJECTIVE_COMPLETED: frozenset(
        {AgentId.LEDGER, AgentId.SCOUT, AgentId.ROGUE, AgentId.MUSE}
    ),
    MessageType.OBJECTIVE_REJECTED: _ALL,
    MessageType.TASK_CREATE: frozenset({AgentId.ORCHESTR}),
    MessageType.TASK_ASSIGN: _WORKERS,
    MessageType.TASK_TRANSITION: frozenset({AgentId.ORCHESTR, AgentId.LEDGER}),
    MessageType.TASK_RESULT: frozenset({AgentId.ORCHESTR, AgentId.LEDGER}),
    MessageType.RESEARCH_REQUEST: frozenset({AgentId.SCOUT, AgentId.ROGUE}),
    MessageType.RESEARCH_FINDING: frozenset({AgentId.SCOUT}),
    MessageType.RESEARCH_CONSOLIDATED: frozenset({AgentId.ORCHESTR, AgentId.ROGUE, AgentId.LEDGER}),
    MessageType.REQUIREMENT_SUBMIT: frozenset({AgentId.SCOUT, AgentId.LEDGER}),
    MessageType.REQUIREMENT_EVALUATED: frozenset(
        {AgentId.ORCHESTR, AgentId.ROGUE, AgentId.LEDGER, AgentId.WARDEN}
    ),
    MessageType.SECURITY_EVALUATE: frozenset({AgentId.WARDEN}),
    MessageType.SECURITY_DECISION: _ALL - {AgentId.WARDEN},
    MessageType.AUTHORIZATION_REQUEST: frozenset({AgentId.ORCHESTR, AgentId.MUSE}),
    MessageType.USER_RESPONSE: frozenset({AgentId.ORCHESTR, AgentId.WARDEN}),
    MessageType.RESOURCE_REQUEST: frozenset({AgentId.ORCHESTR}),
    MessageType.RESOURCE_RESPONSE: _WORKERS,
    MessageType.PROMOTION_REQUEST: frozenset({AgentId.ORCHESTR, AgentId.LEDGER}),
    MessageType.IMPLEMENTATION_PLAN: frozenset(
        {AgentId.ORCHESTR, AgentId.FORGE, AgentId.LEDGER, AgentId.WARDEN}
    ),
    MessageType.EXECUTION_REQUEST: frozenset({AgentId.FORGE}),
    MessageType.EXECUTION_RESULT: frozenset({AgentId.ORCHESTR, AgentId.LEDGER}),
    MessageType.VERIFICATION_REQUEST: frozenset({AgentId.LEDGER}),
    MessageType.TEST_REPORT: frozenset(
        {AgentId.ORCHESTR, AgentId.FORGE, AgentId.SCOUT, AgentId.ROGUE}
    ),
    MessageType.CAPABILITY_REGISTER: frozenset({AgentId.ORCHESTR, AgentId.SCOUT}),
    MessageType.BLOCKER: frozenset(
        {AgentId.ORCHESTR, AgentId.SCOUT, AgentId.ROGUE, AgentId.FORGE, AgentId.WARDEN}
    ),
    MessageType.PROGRESS_UPDATE: frozenset({AgentId.ORCHESTR, AgentId.LEDGER}),
    MessageType.HEALTH_EVENT: frozenset({AgentId.ORCHESTR, AgentId.LEDGER}),
    MessageType.COMMUNICATION_REQUEST: frozenset({AgentId.MUSE}),
    MessageType.COMMUNICATION_DELIVERED: frozenset({AgentId.ORCHESTR, AgentId.LEDGER}),
}
"""Which agents may RECEIVE each message type."""

SENDERS: dict[MessageType, frozenset[AgentId]] = {
    MessageType.OBJECTIVE_CREATE: _ALL,
    MessageType.OBJECTIVE_UPDATE: frozenset(
        {AgentId.ORCHESTR, AgentId.SCOUT, AgentId.ROGUE, AgentId.LEDGER, AgentId.MUSE}
    ),
    MessageType.OBJECTIVE_COMPLETED: frozenset({AgentId.ORCHESTR}),
    MessageType.OBJECTIVE_REJECTED: frozenset({AgentId.ORCHESTR}),
    MessageType.TASK_CREATE: _ALL,
    MessageType.TASK_ASSIGN: frozenset({AgentId.ORCHESTR}),
    MessageType.TASK_TRANSITION: _ALL,
    MessageType.TASK_RESULT: _WORKERS,
    MessageType.RESEARCH_REQUEST: frozenset(
        {AgentId.ORCHESTR, AgentId.SCOUT, AgentId.FORGE, AgentId.LEDGER}
    ),
    MessageType.RESEARCH_FINDING: frozenset({AgentId.SCOUT, AgentId.ROGUE}),
    MessageType.RESEARCH_CONSOLIDATED: frozenset({AgentId.SCOUT}),
    # ROGUE explores and submits findings to SCOUT; it never creates requirements (spec section 6).
    MessageType.REQUIREMENT_SUBMIT: _ALL - {AgentId.ROGUE},
    MessageType.REQUIREMENT_EVALUATED: frozenset({AgentId.SCOUT}),
    # WARDEN cannot approve its own operations.
    MessageType.SECURITY_EVALUATE: _ALL - {AgentId.WARDEN},
    MessageType.SECURITY_DECISION: frozenset({AgentId.WARDEN}),
    MessageType.AUTHORIZATION_REQUEST: frozenset({AgentId.ORCHESTR, AgentId.WARDEN}),
    MessageType.USER_RESPONSE: frozenset({AgentId.MUSE}),
    MessageType.RESOURCE_REQUEST: _WORKERS,
    MessageType.RESOURCE_RESPONSE: frozenset({AgentId.ORCHESTR}),
    MessageType.PROMOTION_REQUEST: frozenset({AgentId.FORGE}),
    MessageType.IMPLEMENTATION_PLAN: frozenset({AgentId.FORGE, AgentId.SCOUT}),
    MessageType.EXECUTION_REQUEST: frozenset({AgentId.ORCHESTR}),
    MessageType.EXECUTION_RESULT: frozenset({AgentId.FORGE}),
    MessageType.VERIFICATION_REQUEST: frozenset({AgentId.ORCHESTR, AgentId.FORGE}),
    MessageType.TEST_REPORT: frozenset({AgentId.LEDGER}),
    # Only LEDGER registers, and only after verification.
    MessageType.CAPABILITY_REGISTER: frozenset({AgentId.LEDGER}),
    MessageType.BLOCKER: _ALL,
    MessageType.PROGRESS_UPDATE: _ALL,
    MessageType.HEALTH_EVENT: _ALL,
    MessageType.COMMUNICATION_REQUEST: frozenset({AgentId.ORCHESTR}),
    MessageType.COMMUNICATION_DELIVERED: frozenset({AgentId.MUSE}),
}
"""Which agents may SEND each message type."""


def receivable_types(agent_id: AgentId) -> frozenset[MessageType]:
    """Message types `agent_id` may receive; suitable for `BaseAgent.accepted_message_types`."""
    return frozenset(kind for kind, receivers in ROUTES.items() if agent_id in receivers)


def sendable_types(agent_id: AgentId) -> frozenset[MessageType]:
    return frozenset(kind for kind, senders in SENDERS.items() if agent_id in senders)


def check_route(message: AgentMessage) -> None:
    """Raise `RouteViolation` unless the sender may send and the target may receive this type."""
    kind = message.message_type
    if kind not in ROUTES or kind not in SENDERS:
        raise UnknownMessageType(kind)
    source, target = message.source_agent, message.target_agent
    if source not in SENDERS[kind]:
        raise RouteViolation(kind, source, target, f"{source.value} may not send it")
    if target not in ROUTES[kind]:
        raise RouteViolation(kind, source, target, f"{target.value} may not receive it")


def _describe(error: ValidationError) -> str:
    # Never echo input values: payloads may carry sensitive data.
    parts = []
    for item in error.errors(include_url=False, include_input=False, include_context=False):
        location = ".".join(str(part) for part in item["loc"]) or "payload"
        parts.append(f"{location}: {item['msg']}")
    return "; ".join(parts)


def validate_payload(message: AgentMessage) -> Payload:
    """Parse `message.payload` with the contract for its type and check it against the envelope."""
    model = PAYLOAD_MODELS.get(message.message_type)
    if model is None:
        raise UnknownMessageType(message.message_type)
    try:
        payload = model.model_validate(message.payload)
    except ValidationError as error:
        raise InvalidPayload(message.message_type, _describe(error)) from None
    try:
        payload.check_envelope(message)
    except ValueError as error:
        raise InvalidPayload(message.message_type, str(error)) from None
    return payload


def validate_message(message: AgentMessage) -> Payload:
    """`check_route` plus `validate_payload`: the full contract check for one message."""
    check_route(message)
    return validate_payload(message)


def build_message(
    source_agent: AgentId, target_agent: AgentId, payload: Payload, **envelope: Any
) -> AgentMessage:
    """Wrap a typed payload in a contract-checked `AgentMessage` (type inferred from the model)."""
    message_type = MESSAGE_TYPE_BY_MODEL.get(type(payload))
    if message_type is None:
        raise UnknownMessageType(type(payload).__name__)
    message = AgentMessage(
        source_agent=source_agent,
        target_agent=target_agent,
        message_type=message_type,
        payload=payload.model_dump(mode="json"),
        **envelope,
    )
    validate_message(message)
    return message
