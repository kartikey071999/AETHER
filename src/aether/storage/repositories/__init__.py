"""Transactional operations that keep durable state and audit events aligned.

Audit events commit with state changes; frequent host samples use rate-limited events.
"""

from aether.storage.repositories._events import add_event
from aether.storage.repositories.agents import (
    AgentHeartbeatRepository,
    AgentRepository,
    agent_aggregate_id,
)
from aether.storage.repositories.authorization import (
    AuthorizationRequestRepository,
    InvalidAuthorizationResponse,
)
from aether.storage.repositories.capabilities import (
    CapabilityRepository,
    InvalidCapabilityTransition,
    allowed_capability_transitions,
)
from aether.storage.repositories.dependencies import DependencyCycleError, TaskDependencyRepository
from aether.storage.repositories.errors import ErrorRepository
from aether.storage.repositories.execution import ExecutionRunRepository
from aether.storage.repositories.knowledge_graph import KnowledgeGraphRepository
from aether.storage.repositories.objectives import ObjectiveRepository, get_objective
from aether.storage.repositories.personalization import PersonRepository
from aether.storage.repositories.provider_usage import ProviderUsageRepository
from aether.storage.repositories.requirements import (
    ALLOWED_REQUIREMENT_TRANSITIONS,
    InvalidRequirementTransition,
    RequirementConflictError,
    RequirementRepository,
)
from aether.storage.repositories.research import (
    EvidenceInput,
    InvalidFindingTransition,
    ResearchFindingRepository,
)
from aether.storage.repositories.resources import ResourceMeasurementRepository
from aether.storage.repositories.security import (
    OperationAuthorizationRequests,
    SecurityDecisionRepository,
)
from aether.storage.repositories.tasks import (
    _ALLOWED_TRANSITIONS,  # noqa: F401 - compatibility re-export
    ALLOWED_TASK_TRANSITIONS,
    FINISHED_TASK_STATES,
    InvalidTaskTransition,
    TaskRepository,
)
from aether.storage.repositories.test_runs import (
    InvalidTestRunTransition,
    TestResultInput,
    TestRunCounts,
    TestRunRepository,
)

__all__ = [
    "ALLOWED_REQUIREMENT_TRANSITIONS",
    "ALLOWED_TASK_TRANSITIONS",
    "FINISHED_TASK_STATES",
    "AgentHeartbeatRepository",
    "AgentRepository",
    "AuthorizationRequestRepository",
    "CapabilityRepository",
    "DependencyCycleError",
    "ErrorRepository",
    "EvidenceInput",
    "ExecutionRunRepository",
    "InvalidAuthorizationResponse",
    "InvalidCapabilityTransition",
    "InvalidFindingTransition",
    "InvalidRequirementTransition",
    "InvalidTaskTransition",
    "InvalidTestRunTransition",
    "KnowledgeGraphRepository",
    "ObjectiveRepository",
    "OperationAuthorizationRequests",
    "PersonRepository",
    "ProviderUsageRepository",
    "RequirementConflictError",
    "RequirementRepository",
    "ResearchFindingRepository",
    "ResourceMeasurementRepository",
    "SecurityDecisionRepository",
    "TaskDependencyRepository",
    "TaskRepository",
    "TestResultInput",
    "TestRunCounts",
    "TestRunRepository",
    "add_event",
    "agent_aggregate_id",
    "allowed_capability_transitions",
    "get_objective",
]
