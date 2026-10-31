"""Canonical responsibilities and permissions for the seven AETHER agents."""

from dataclasses import dataclass

from aether.communication.messages import AgentId


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    agent_id: AgentId
    responsibility: str
    allowed_actions: tuple[str, ...]


AGENT_DEFINITIONS: tuple[AgentDefinition, ...] = (
    AgentDefinition(
        AgentId.ORCHESTR,
        "Coordinates objectives, task delegation, priorities, and resource-aware scheduling.",
        ("objective.manage", "task.schedule", "task.delegate", "resource.read"),
    ),
    AgentDefinition(
        AgentId.SCOUT,
        "Researches capabilities, evaluates evidence, and owns the consolidated roadmap.",
        ("research.create", "finding.submit", "requirement.submit", "roadmap.manage"),
    ),
    AgentDefinition(
        AgentId.ROGUE,
        "Explores unconventional approaches and submits evidence-backed findings to SCOUT.",
        ("research.create", "finding.submit"),
    ),
    AgentDefinition(
        AgentId.FORGE,
        "Implements approved requirements through explicitly authorized execution tools.",
        ("implementation.propose", "execution.request", "progress.report"),
    ),
    AgentDefinition(
        AgentId.LEDGER,
        "Maintains operational records, tracks requirements, and independently verifies outcomes.",
        ("records.read", "records.summarize", "verification.run", "verification.report"),
    ),
    AgentDefinition(
        AgentId.WARDEN,
        "Evaluates security risks and records scoped authorization decisions.",
        ("security.evaluate", "security.advise", "authorization.decide"),
    ),
    AgentDefinition(
        AgentId.MUSE,
        "Personalizes final user-facing communication from ORCHESTR's structured handoff.",
        ("preferences.read", "communication.prepare", "communication.deliver"),
    ),
)

_DEFINITIONS_BY_ID = {definition.agent_id: definition for definition in AGENT_DEFINITIONS}


def get_agent(agent_id: AgentId) -> AgentDefinition:
    return _DEFINITIONS_BY_ID[agent_id]
