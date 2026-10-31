"""Bounded research configuration, persisted work, and validated model output."""

from datetime import timedelta
from typing import Annotated, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from aether.communication.contracts import CandidateApproach, ResearchRequestPayload
from aether.security.untrusted import UntrustedContent
from aether.storage.models import FindingClassification

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ResearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class SourceSpec(ResearchModel):
    kind: Literal["http", "file"]
    location: Text
    purpose: str = "Collect evidence"
    perspective: Literal["conventional", "unconventional", "both"] = "both"


@runtime_checkable
class HttpSourceResult(Protocol):
    status_code: int
    body: UntrustedContent
    truncated: bool


@runtime_checkable
class FileSourceResult(Protocol):
    content: UntrustedContent
    truncated: bool


class ResearchScope(ResearchModel):
    sources: list[SourceSpec] = Field(default_factory=list, max_length=32)


class ResearchLimits(ResearchModel):
    max_active_objectives: int = Field(default=8, ge=1, le=100)
    max_queue_size: int = Field(default=16, ge=1, le=1000)
    max_generated_per_interval: int = Field(default=2, ge=0, le=100)
    generation_interval_seconds: int = Field(default=86400, ge=1)
    review_seconds: int = Field(default=86400, ge=1)
    max_sources: int = Field(default=4, ge=0, le=32)
    max_llm_requests: int = Field(default=2, ge=0, le=8)
    max_attempts: int = Field(default=2, ge=1, le=5)
    max_follow_ups: int = Field(default=1, ge=0, le=4)
    top_k: int = Field(default=3, ge=1, le=8)
    default_sources: tuple[SourceSpec, ...] = ()

    @property
    def review_interval(self) -> timedelta:
        return timedelta(seconds=self.review_seconds)


class ResearchWork(ResearchModel):
    request: ResearchRequestPayload
    correlation_id: UUID
    origin_message_id: UUID


class FindingDraft(ResearchModel):
    summary: Text
    candidates: list[CandidateApproach] = Field(min_length=1, max_length=8)
    recommendation: Text
    confidence: float = Field(ge=0, le=1)
    classification: FindingClassification = FindingClassification.HYPOTHESIS
    evidence_indices: list[int] = Field(default_factory=list, max_length=32)
    security_considerations: list[str] = Field(default_factory=list, max_length=16)
    next_steps: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def check_recommendation(self) -> "FindingDraft":
        if len({candidate.name for candidate in self.candidates}) != len(self.candidates):
            raise ValueError("candidate names must be unique")
        if self.recommendation not in {candidate.name for candidate in self.candidates}:
            raise ValueError("recommendation must name a candidate")
        if self.classification is FindingClassification.VERIFIED:
            raise ValueError("a model cannot establish independent verification")
        if len(set(self.evidence_indices)) != len(self.evidence_indices):
            raise ValueError("evidence indices must be unique")
        return self


class GeneratedObjective(ResearchModel):
    question: Text
    rationale: Text
    expected_benefit: Text
    estimated_cost: float = Field(ge=0)
    stopping_conditions: list[Text] = Field(min_length=1, max_length=8)
    scope: str = ""
