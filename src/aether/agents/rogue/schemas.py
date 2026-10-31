"""Validated divergent-generation and critique outputs."""

from pydantic import Field, model_validator

from aether.agents.scout.schemas import ResearchModel
from aether.communication.contracts import CandidateApproach


class DivergentDraft(ResearchModel):
    candidates: list[CandidateApproach] = Field(min_length=1, max_length=8)


class CritiqueDraft(ResearchModel):
    ranked_indices: list[int] = Field(min_length=1, max_length=8)
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ranking(self) -> "CritiqueDraft":
        if len(set(self.ranked_indices)) != len(self.ranked_indices):
            raise ValueError("ranked candidate indices must be unique")
        return self
