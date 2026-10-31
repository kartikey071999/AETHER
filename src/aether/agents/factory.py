"""Build the seven agents, letting each agent package plug itself in without shared edits.

An agent worker adds `src/aether/agents/<lowercase agent id>/__init__.py` exposing
``AGENT_CLASS = <BaseAgent subclass>``. `build_agents` imports that package when it exists and
falls back to an inert placeholder otherwise, so no shared file changes when an agent lands.
"""

import importlib
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aether.agents.base import BaseAgent
from aether.agents.placeholder import PlaceholderAgent, placeholder_class
from aether.agents.registry import AGENT_DEFINITIONS
from aether.communication.messages import AgentId

if TYPE_CHECKING:
    from aether.agents.scout.schemas import ResearchLimits

_LOGGER = logging.getLogger("aether.agents.factory")

AGENT_CLASS_ATTRIBUTE = "AGENT_CLASS"


class AgentFactoryError(TypeError):
    """An agent package or override does not provide a usable agent class."""


def agent_module_name(agent_id: AgentId) -> str:
    return f"{__package__}.{agent_id.value.lower()}"


def _check_agent_class(agent_id: AgentId, candidate: object, origin: str) -> type[BaseAgent]:
    if not (isinstance(candidate, type) and issubclass(candidate, BaseAgent)):
        raise AgentFactoryError(f"{origin} must be a BaseAgent subclass, got {candidate!r}")
    declared = getattr(candidate, "agent_id", None)
    if declared is not agent_id:
        raise AgentFactoryError(
            f"{origin} declares agent_id {declared!r}, expected {agent_id.value!r}"
        )
    return candidate


def resolve_agent_class(agent_id: AgentId) -> type[BaseAgent]:
    """The implemented class for `agent_id`, or its placeholder when no package exists yet."""
    module_name = agent_module_name(agent_id)
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        # Only the agent package itself being absent means "not implemented yet"; a missing
        # import inside an existing package is a real defect and must surface.
        if error.name != module_name:
            raise
        return placeholder_class(agent_id)
    candidate = getattr(module, AGENT_CLASS_ATTRIBUTE, None)
    if candidate is None:
        _LOGGER.warning(
            "Agent package has no AGENT_CLASS; using placeholder",
            extra={"agent_id": agent_id.value, "agent_module": module_name},
        )
        return placeholder_class(agent_id)
    return _check_agent_class(agent_id, candidate, f"{module_name}.{AGENT_CLASS_ATTRIBUTE}")


def build_agents(
    overrides: Mapping[AgentId, type[BaseAgent]] | None = None,
    *,
    options: Mapping[AgentId, Mapping[str, Any]] | None = None,
) -> list[BaseAgent]:
    """One agent per canonical definition, in registry order.

    `overrides` replaces the class used for specific agents (e.g. test doubles); each override
    must be a `BaseAgent` subclass declaring the matching `agent_id`. `options` are constructor
    keyword arguments for implemented agent packages (see `runtime_agent_options`); they are
    not passed to overrides or placeholders, whose constructors take no arguments.
    """
    overrides = dict(overrides or {})
    options = dict(options or {})
    known = {definition.agent_id for definition in AGENT_DEFINITIONS}
    unknown = (set(overrides) | set(options)) - known
    if unknown:
        raise AgentFactoryError(
            f"overrides or options for unknown agents: {sorted(map(str, unknown))}"
        )
    agents: list[BaseAgent] = []
    for definition in AGENT_DEFINITIONS:
        agent_id = definition.agent_id
        if agent_id in overrides:
            cls = _check_agent_class(agent_id, overrides[agent_id], f"override for {agent_id}")
            agents.append(cls())
            continue
        cls = resolve_agent_class(agent_id)
        if issubclass(cls, PlaceholderAgent):
            agents.append(cls())
        else:
            agents.append(cls(**dict(options.get(agent_id, {}))))
    return agents


def runtime_agent_options(
    *,
    data_dir: Path,
    research_limits: "ResearchLimits | None" = None,
) -> dict[AgentId, dict[str, Any]]:
    """Constructor options derived from runtime settings for the agents that take any.

    MUSE gets its data directory (outbox location); SCOUT and ROGUE get the research limits.
    """
    from aether.agents.muse import MuseSettings
    from aether.agents.scout.schemas import ResearchLimits

    limits = research_limits or ResearchLimits()
    return {
        AgentId.MUSE: {"settings": MuseSettings(data_dir=Path(data_dir))},
        AgentId.SCOUT: {"limits": limits},
        AgentId.ROGUE: {"limits": limits},
    }
