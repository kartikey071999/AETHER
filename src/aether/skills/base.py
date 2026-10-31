"""Skills: typed, deterministic building blocks agents compose instead of hand-coding flows.

A skill declares its input and output models, which agents may use it, and whether it has side
effects. It runs through the calling agent's own facades (tools, models), so the policy layer,
agent identity and budgets still apply: a skill can never do more than its caller may do.
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from time import perf_counter
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from pydantic import BaseModel, ValidationError

from aether.communication.messages import AgentId

if TYPE_CHECKING:
    from aether.agents.base import AgentContext


class SkillError(RuntimeError):
    """A skill could not produce a valid result (input, permission, execution or output)."""


class UnknownSkill(SkillError, LookupError):
    pass


class SkillNotPermitted(SkillError, PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class SkillContext:
    """Everything a skill may use; ``agent_ctx`` carries the agent-bound tool/model facades."""

    agent_id: AgentId
    agent_ctx: "AgentContext"
    objective_id: UUID | None = None
    task_id: UUID | None = None
    correlation_id: UUID | None = None
    # Dry runs never cause side effects: side-effecting skills return their fixture instead.
    dry_run: bool = False
    fixtures: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


class Skill(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]
    owners: ClassVar[frozenset[AgentId]]
    # Reads remote data, writes state, or calls a model: affects the outside world or budgets.
    side_effects: ClassVar[bool] = False
    timeout_seconds: ClassVar[float] = 120.0

    @abstractmethod
    async def run(self, inputs: BaseModel, ctx: SkillContext) -> BaseModel: ...

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "owners": sorted(agent.value for agent in self.owners),
            "side_effects": self.side_effects,
            "input_schema": self.input_model.model_json_schema(),
            "output_schema": self.output_model.model_json_schema(),
        }


async def execute_skill(
    skill: Skill, raw_inputs: Mapping[str, Any], ctx: SkillContext
) -> tuple[BaseModel, float]:
    """Validate input, run (or replay a fixture in dry runs), validate output; returns ms."""
    if ctx.agent_id not in skill.owners:
        raise SkillNotPermitted(f"{ctx.agent_id.value} may not use skill {skill.name}")
    try:
        inputs = skill.input_model.model_validate(dict(raw_inputs))
    except ValidationError as error:
        raise SkillError(f"{skill.name}: invalid input: {error.error_count()} error(s)") from None
    started = perf_counter()
    if ctx.dry_run and skill.side_effects:
        fixture = ctx.fixtures.get(skill.name)
        if fixture is None:
            raise SkillError(f"{skill.name}: dry run needs a fixture for this side-effecting skill")
        output: Any = fixture
    else:
        async with asyncio.timeout(skill.timeout_seconds):
            output = await skill.run(inputs, ctx)
    try:
        result = skill.output_model.model_validate(
            output.model_dump() if isinstance(output, BaseModel) else output
        )
    except ValidationError as error:
        raise SkillError(f"{skill.name}: invalid output: {error.error_count()} error(s)") from None
    return result, (perf_counter() - started) * 1000
