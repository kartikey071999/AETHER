"""Transactional operations that keep durable state and audit events aligned.

Every state-changing repository method writes an ``EventRecord`` in the same transaction.
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
from aether.storage.repositories.knowledge_graph import KnowledgeGraphRepository
from aether.storage.repositories.objectives import ObjectiveRepository, get_objective
from aether.storage.repositories.personalization import PersonRepository
from aether.storage.repositories.provider_usage import ProviderUsageRepository
from aether.storage.repositories.requirements import RequirementRepository
from aether.storage.repositories.research import (
    EvidenceInput,
    InvalidFindingTransition,
    ResearchFindingRepository,
)
from aether.storage.repositories.tasks import InvalidTaskTransition, TaskRepository

__all__ = [
    "AgentHeartbeatRepository",
    "AgentRepository",
    "AuthorizationRequestRepository",
    "CapabilityRepository",
    "DependencyCycleError",
    "ErrorRepository",
    "EvidenceInput",
    "InvalidAuthorizationResponse",
    "InvalidCapabilityTransition",
    "InvalidFindingTransition",
    "InvalidTaskTransition",
    "KnowledgeGraphRepository",
    "ObjectiveRepository",
    "PersonRepository",
    "ProviderUsageRepository",
    "RequirementRepository",
    "ResearchFindingRepository",
    "TaskDependencyRepository",
    "TaskRepository",
    "add_event",
    "agent_aggregate_id",
    "allowed_capability_transitions",
    "get_objective",
]
