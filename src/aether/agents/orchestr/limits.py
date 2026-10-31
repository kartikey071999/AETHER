"""Configurable ORCHESTR limits; defaults are conservative and overridable via ``ctx.services``."""

from pydantic import BaseModel, ConfigDict, Field

from aether.agents.orchestr.priority import PriorityPolicy

LIMITS_SERVICE = "orchestr_limits"


class OrchestrLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_active_objectives: int = Field(default=10, ge=1)
    max_generated_objectives_per_interval: int = Field(default=3, ge=0)
    generated_interval_hours: float = Field(default=24.0, gt=0)
    max_generated_priority: int = Field(default=60, ge=0, le=100)
    limit_deferral_hours: float = Field(default=1.0, gt=0)
    max_tasks_per_objective: int = Field(default=30, ge=1)
    max_subtask_depth: int = Field(default=3, ge=0)
    max_plan_depth: int = Field(default=8, ge=1)
    max_refinement_tasks: int = Field(default=5, ge=0)
    max_task_attempts: int = Field(default=3, ge=1)
    max_blocker_attempts: int = Field(default=3, ge=1)
    max_replans_per_objective: int = Field(default=2, ge=0)
    max_agent_boost_points: float = Field(default=15.0, ge=0.0)
    llm_refinement: bool = True
    deadline_escalation_hours: float = Field(default=24.0, gt=0)
    priority: PriorityPolicy = Field(default_factory=PriorityPolicy)
