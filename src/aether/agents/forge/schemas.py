"""FORGE data types: configuration, plans, refusals, failure classes and LLM draft schemas."""

import sys
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aether.communication.contracts import BlockerKind, PlanStep, ResourceEstimate
from aether.communication.messages import AgentId
from aether.security.operations import Operation
from aether.storage.models import TaskRecord

RUNTIME_COPY_DIR = "runtime-copy"
ARTIFACTS_DIR = "artifacts"
MANIFEST_PATH = ".forge/manifest.json"
PROMOTION_PATH = f"{ARTIFACTS_DIR}/promotion-request.json"


@dataclass(frozen=True, slots=True)
class ForgeConfig:
    """Limits and knobs; every default is safe so the factory can build FORGE with no arguments."""

    python_executable: str = sys.executable
    max_steps: int = 20
    max_step_retries: int = 2
    backoff_base_seconds: float = 1.0
    backoff_cap_seconds: float = 30.0
    step_timeout_seconds: float | None = None
    max_ram_mb: int = 4096
    max_disk_mb: int = 8192
    max_cpu_percent: float = 100.0
    max_duration_s: int = 7200
    evidence_chars: int = 500
    llm_drafting: bool = True
    llm_max_tokens: int = 1500

    def backoff(self, failures: int) -> float:
        return min(self.backoff_base_seconds * (2 ** max(failures, 0)), self.backoff_cap_seconds)


class PlanDefect(ValueError):
    """A plan step is malformed or escapes the task workspace."""


class RuntimeScopeDefect(PlanDefect):
    """A runtime self-change step reaches outside the separate workspace copy."""


class RefusalCode(StrEnum):
    TASK_MISSING = "task_missing"
    NOT_ASSIGNED = "not_assigned_to_forge"
    NOT_RUNNING = "scheduler_state_not_running"
    NO_LEASE = "no_live_lease"
    NO_REQUIREMENT = "requirement_missing"
    REQUIREMENT_INACTIVE = "requirement_inactive"
    REQUIREMENT_NOT_APPROVED = "requirement_not_approved"
    OBJECTIVE_MISMATCH = "objective_mismatch"
    NO_ACCEPTANCE_CRITERIA = "acceptance_criteria_missing"
    NO_PLAN = "implementation_plan_missing"
    PLAN_INVALID = "implementation_plan_invalid"
    TOO_MANY_STEPS = "too_many_steps"
    UNKNOWN_TOOL = "unregistered_tool"
    RUNTIME_SCOPE = "runtime_change_outside_copy"
    RUNTIME_NEEDS_REQUIREMENT = "runtime_change_needs_requirement"
    DEPENDENCY_INCOMPLETE = "dependency_incomplete"
    NO_RESOURCE_ESTIMATE = "resource_estimate_missing"
    RESOURCE_EXCEEDED = "resource_estimate_exceeds_limits"
    SECURITY_REJECTED = "security_decision_rejected"
    SECURITY_PENDING = "security_clearance_missing"


@dataclass(frozen=True, slots=True)
class Refusal:
    code: RefusalCode
    detail: str
    kind: BlockerKind = BlockerKind.IMPLEMENTATION
    route_to: AgentId = AgentId.ORCHESTR

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class PendingReview:
    """An operation that still needs an approved, unexpired WARDEN decision."""

    operation: Operation
    reason: str


@dataclass(frozen=True, slots=True)
class ForgePlan:
    """A plan as FORGE executes it. Steps are task-relative until `scope_plan` rewrites them."""

    plan_id: UUID
    task_id: UUID
    objective_id: UUID | None
    requirement_id: UUID | None
    steps: tuple[PlanStep, ...]
    rollback_steps: tuple[PlanStep, ...]
    acceptance_criteria: tuple[str, ...]
    resource_estimate: ResourceEstimate
    depends_on: tuple[UUID, ...] = ()
    source: str = "unknown"
    runtime_change: bool = False


@dataclass(slots=True)
class PreflightReport:
    """Everything preflight learned: refusals, operations awaiting WARDEN, and the scoped plan."""

    refusals: list[Refusal] = field(default_factory=list)
    pending: list[PendingReview] = field(default_factory=list)
    task: TaskRecord | None = None
    plan: ForgePlan | None = None
    criteria: tuple[str, ...] = ()
    requirement_id: UUID | None = None

    @property
    def cleared(self) -> bool:
        return not self.refusals and not self.pending


class FailureClass(StrEnum):
    ENVIRONMENT = "environment"
    MISSING_DEPENDENCY = "missing_dependency"
    CODE_DEFECT = "code_defect"
    PLAN_DEFECT = "plan_defect"
    SECURITY = "security"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FailureRoute:
    kind: BlockerKind
    route_to: AgentId


class ForgeSpec(BaseModel):
    """Optional structured request embedded in a task or requirement description.

    It is written as a fenced block labelled ``forge-spec`` containing JSON.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["create_files", "python_test", "pip_install", "runtime_change"]
    files: dict[str, str] = Field(default_factory=dict)
    command: list[str] = Field(default_factory=list)
    packages: list[str] = Field(default_factory=list)


class DraftStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str = Field(min_length=1)
    arguments: dict[str, object] = Field(default_factory=dict)
    purpose: str = Field(min_length=1)


class DraftPlan(BaseModel):
    """The only shape accepted from the LLM; anything else falls back to no plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: list[DraftStep] = Field(min_length=1, max_length=50)
    rollback_steps: list[DraftStep] = Field(default_factory=list, max_length=50)
