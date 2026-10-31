"""Runtime knobs for agent hooks: periodic ticks, cooperative task control and agent limits.

Loaded from ``AETHER_*`` environment variables next to ``AetherSettings``. Nested agent limits
accept JSON (``AETHER_ORCHESTR='{"max_active_objectives": 5}'``) or ``__``-delimited keys
(``AETHER_ORCHESTR__MAX_ACTIVE_OBJECTIVES=5``); per-agent tick intervals are a JSON object
(``AETHER_AGENT_TICK_OVERRIDES='{"WARDEN": 5}'``).
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aether.agents.orchestr.limits import OrchestrLimits
from aether.agents.scout.schemas import ResearchLimits
from aether.communication.messages import AgentId


class AgentRuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AETHER_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    agent_tick_seconds: float = Field(default=30.0, gt=0)
    agent_tick_overrides: dict[AgentId, float] = Field(default_factory=dict)
    agent_tick_timeout_seconds: float = Field(default=120.0, gt=0)
    task_control_poll_seconds: float = Field(default=5.0, gt=0)
    task_interrupt_grace_seconds: float = Field(default=10.0, ge=0)
    orchestr: OrchestrLimits = Field(default_factory=OrchestrLimits)
    research: ResearchLimits = Field(default_factory=ResearchLimits)

    @field_validator("agent_tick_overrides")
    @classmethod
    def _positive_intervals(cls, value: dict[AgentId, float]) -> dict[AgentId, float]:
        for agent_id, seconds in value.items():
            if not seconds > 0:
                raise ValueError(f"tick interval for {agent_id.value} must be positive")
        return value

    def tick_seconds_for(self, agent_id: AgentId) -> float:
        return self.agent_tick_overrides.get(agent_id, self.agent_tick_seconds)


__all__ = ["AgentRuntimeSettings"]
