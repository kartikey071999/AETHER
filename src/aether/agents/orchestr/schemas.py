"""LLM-facing schemas: validated, bounded, and never authoritative on their own."""

from pydantic import BaseModel, ConfigDict, Field

from aether.communication.messages import AgentId

KEY_PATTERN = r"^[a-z][a-z0-9_-]{0,47}$"


class ExtraTask(BaseModel):
    """One additional task the model proposes on top of the deterministic template."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=KEY_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    agent: AgentId
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=8)
    depends_on: list[str] = Field(default_factory=list, max_length=5)
    before: str | None = None


class PlanRefinement(BaseModel):
    """Optional model output refining the deterministic plan for one objective."""

    model_config = ConfigDict(extra="forbid")

    kind: str | None = None
    extra_tasks: list[ExtraTask] = Field(default_factory=list, max_length=8)
    notes: str = Field(default="", max_length=1000)
