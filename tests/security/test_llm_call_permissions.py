import pytest

from aether.communication.messages import AgentId
from aether.models import default_model_assignments
from aether.security import Operation, OperationKind, PermissionService
from aether.security.policy import default_agent_permissions

LLM_AGENTS = (AgentId.ORCHESTR, AgentId.SCOUT, AgentId.ROGUE, AgentId.FORGE, AgentId.WARDEN)


def llm_operation(agent: AgentId) -> Operation:
    return Operation(
        kind=OperationKind.LLM_CALL,
        target="https://api.example.com",
        hosts=("api.example.com",),
        requested_by=agent,
    )


def test_every_agent_with_a_model_assignment_may_make_llm_calls() -> None:
    permitted = default_agent_permissions()
    for name in default_model_assignments():
        assert OperationKind.LLM_CALL in permitted[AgentId(name)], name


@pytest.mark.parametrize("agent", LLM_AGENTS)
async def test_agents_that_call_llms_are_authorized(
    permissions: PermissionService, agent: AgentId
) -> None:
    assert await permissions.authorize(llm_operation(agent)) is not None


def test_warden_llm_permission_does_not_widen_its_other_privileges() -> None:
    kinds = default_agent_permissions()[AgentId.WARDEN]
    assert OperationKind.LLM_CALL in kinds
    assert not kinds & {
        OperationKind.SUBPROCESS,
        OperationKind.FILE_WRITE,
        OperationKind.PACKAGE_INSTALL,
        OperationKind.REPO_CLONE,
    }
