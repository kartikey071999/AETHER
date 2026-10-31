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
    # At most one escalation (and user update) per (objective, kind) within this window; later
    # triggers of the same kind are aggregated into the open escalation.
    escalation_window_hours: float = Field(default=1.0, gt=0)
    # A task held for an objective's requirement escalates after waiting this long.
    requirement_wait_minutes: float = Field(default=10.0, gt=0)
    # A task waiting for its independent test run or for an approval escalates (once, without
    # blocking the objective) after this long.
    verification_wait_minutes: float = Field(default=30.0, gt=0)
    approval_wait_minutes: float = Field(default=60.0, gt=0)
    # A PENDING task the scheduler keeps deferring for resource reasons escalates after this long.
    resource_deferral_minutes: float = Field(default=15.0, gt=0)
    # Resource pressure: after `pressure_pause_after_ticks` consecutive pressured ticks ORCHESTR
    # requests a pause of up to `pressure_max_pauses_per_tick` interruptible running tasks (0
    # disables it); after `pressure_resume_after_ticks` calm ticks it resumes one of them.
    pressure_pause_after_ticks: int = Field(default=3, ge=1)
    pressure_resume_after_ticks: int = Field(default=2, ge=1)
    pressure_max_pauses_per_tick: int = Field(default=1, ge=0)
    pressure_min_available_ram_mb: int = Field(default=512, ge=0)
    priority: PriorityPolicy = Field(default_factory=PriorityPolicy)
