import sys
import types

import pytest

from aether.agents.base import AgentContext, BaseAgent, TaskResult, TaskResultStatus
from aether.agents.factory import (
    AgentFactoryError,
    agent_module_name,
    build_agents,
    resolve_agent_class,
)
from aether.agents.placeholder import PlaceholderAgent, build_placeholder_agents
from aether.agents.registry import AGENT_DEFINITIONS
from aether.communication.messages import AgentId, AgentMessage
from aether.storage.models import TaskRecord


class FakeScout(BaseAgent):
    agent_id = AgentId.SCOUT

    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None:
        return None

    async def execute_task(self, task: TaskRecord, ctx: AgentContext) -> TaskResult:
        return TaskResult(status=TaskResultStatus.COMPLETED)


class FakeForge(FakeScout):
    agent_id = AgentId.FORGE


@pytest.fixture
def install_agent_module(monkeypatch):
    def install(agent_id: AgentId, **attributes) -> str:
        name = agent_module_name(agent_id)
        module = types.ModuleType(name)
        for key, value in attributes.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)
        return name

    return install


def ids(agents: list[BaseAgent]) -> list[AgentId]:
    return [agent.agent_id for agent in agents]


def test_module_name_is_the_lowercase_agent_id() -> None:
    assert agent_module_name(AgentId.ORCHESTR) == "aether.agents.orchestr"


def test_missing_agent_packages_fall_back_to_placeholders(monkeypatch) -> None:
    def missing(name: str):
        error = ModuleNotFoundError(f"No module named {name!r}")
        error.name = name
        raise error

    # Independent of which agent packages have landed in the source tree.
    monkeypatch.setattr("aether.agents.factory.importlib.import_module", missing)
    agents = build_agents()

    assert ids(agents) == [definition.agent_id for definition in AGENT_DEFINITIONS]
    assert all(isinstance(agent, PlaceholderAgent) for agent in agents)
    assert len({id(agent) for agent in agents}) == len(agents)
    assert ids(build_placeholder_agents()) == ids(agents)


def test_build_agents_without_injection_returns_every_agent() -> None:
    assert ids(build_agents()) == [definition.agent_id for definition in AGENT_DEFINITIONS]


def test_agent_package_is_picked_up_via_agent_class(install_agent_module) -> None:
    install_agent_module(AgentId.SCOUT, AGENT_CLASS=FakeScout)

    agents = {agent.agent_id: agent for agent in build_agents()}

    assert type(agents[AgentId.SCOUT]) is FakeScout
    assert resolve_agent_class(AgentId.SCOUT) is FakeScout
    assert len(agents) == len(AGENT_DEFINITIONS)


def test_package_without_agent_class_falls_back(install_agent_module) -> None:
    install_agent_module(AgentId.MUSE)
    assert issubclass(resolve_agent_class(AgentId.MUSE), PlaceholderAgent)


@pytest.mark.parametrize("bad", [object, "FakeScout", FakeForge], ids=["type", "str", "wrong-id"])
def test_bad_agent_class_is_rejected(install_agent_module, bad) -> None:
    install_agent_module(AgentId.SCOUT, AGENT_CLASS=bad)
    with pytest.raises(AgentFactoryError):
        build_agents()


def test_missing_dependency_inside_an_agent_package_surfaces(monkeypatch) -> None:
    def broken_import(name: str):
        error = ModuleNotFoundError("No module named 'not_installed'")
        error.name = "not_installed"
        raise error

    monkeypatch.setattr("aether.agents.factory.importlib.import_module", broken_import)
    with pytest.raises(ModuleNotFoundError):
        build_agents()


def test_overrides_replace_specific_agents() -> None:
    agents = {agent.agent_id: agent for agent in build_agents({AgentId.FORGE: FakeForge})}
    assert type(agents[AgentId.FORGE]) is FakeForge
    assert len(agents) == len(AGENT_DEFINITIONS)


def test_overrides_take_precedence_over_packages(install_agent_module) -> None:
    install_agent_module(AgentId.SCOUT, AGENT_CLASS=object)
    agents = {agent.agent_id: agent for agent in build_agents({AgentId.SCOUT: FakeScout})}
    assert type(agents[AgentId.SCOUT]) is FakeScout


def test_mismatched_override_is_rejected() -> None:
    with pytest.raises(AgentFactoryError):
        build_agents({AgentId.SCOUT: FakeForge})
