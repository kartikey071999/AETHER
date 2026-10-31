"""Skill registry and the per-agent ``AgentSkills`` facade."""

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel

from aether.communication.messages import AgentId
from aether.observability.events import build_event
from aether.observability.recorder import EventRecorder
from aether.skills.base import (
    Skill,
    SkillContext,
    SkillError,
    SkillNotPermitted,
    UnknownSkill,
    execute_skill,
)

_NIL = UUID(int=0)


class SkillRegistry:
    def __init__(self, skills: Iterable[Skill] = ()) -> None:
        self._skills: dict[str, Skill] = {}
        for skill in skills:
            self.register(skill)

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            raise ValueError(f"skill already registered: {skill.name}")
        if not skill.owners:
            raise ValueError(f"skill {skill.name} has no owners")
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError:
            raise UnknownSkill(f"unknown skill: {name}") from None

    @property
    def skills(self) -> Mapping[str, Skill]:
        return MappingProxyType(self._skills)

    def skills_for(self, agent: AgentId) -> tuple[Skill, ...]:
        return tuple(skill for skill in self._skills.values() if agent in skill.owners)


class AgentSkills:
    """Bound to one agent: lists and runs only that agent's skills, recording each direct run."""

    __slots__ = ("_agent", "_recorder", "_registry")

    def __init__(
        self, agent: AgentId, registry: SkillRegistry, recorder: EventRecorder | None = None
    ) -> None:
        self._agent = agent
        self._registry = registry
        self._recorder = recorder or EventRecorder()

    @property
    def agent_id(self) -> AgentId:
        return self._agent

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(skill.name for skill in self._registry.skills_for(self._agent)))

    def describe(self) -> list[dict[str, Any]]:
        return [skill.describe() for skill in self._registry.skills_for(self._agent)]

    def get(self, name: str) -> Skill:
        skill = self._registry.get(name)
        if self._agent not in skill.owners:
            raise SkillNotPermitted(f"{self._agent.value} may not use skill {name}")
        return skill

    async def run(self, name: str, inputs: Mapping[str, Any], ctx: SkillContext) -> BaseModel:
        if ctx.agent_id is not self._agent:
            raise SkillNotPermitted("skill context belongs to a different agent")
        skill = self.get(name)
        status, error_text, duration = "succeeded", None, 0.0
        try:
            result, duration = await execute_skill(skill, inputs, ctx)
            return result
        except SkillError as error:
            status, error_text = "failed", str(error)[:500]
            raise
        except Exception as error:
            status, error_text = "failed", f"{type(error).__name__}"
            raise SkillError(f"{name}: {type(error).__name__}") from error
        finally:
            await self._record(skill.name, ctx, status, duration, error_text)

    async def _record(
        self, skill: str, ctx: SkillContext, status: str, duration: float, error: str | None
    ) -> None:
        event = build_event(
            "skill.completed",
            source="aether.skills",
            actor=self._agent.value,
            aggregate_type="skill",
            aggregate_id=ctx.correlation_id or ctx.task_id or _NIL,
            objective_id=ctx.objective_id,
            task_id=ctx.task_id,
            correlation_id=ctx.correlation_id or uuid4(),
            payload={
                "skill": skill,
                "agent": self._agent.value,
                "status": status,
                "duration_ms": round(duration, 3),
                "error": error,
            },
        )
        await self._recorder.record(ctx.agent_ctx.sessions, event)
