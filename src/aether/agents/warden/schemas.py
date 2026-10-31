"""Structured output WARDEN accepts from its optional risk-summary model call."""

from pydantic import BaseModel, ConfigDict, Field

MAX_SUMMARY_CHARS = 1000
MAX_RISKS = 10
MAX_RISK_CHARS = 200


class RiskSummary(BaseModel):
    """Advisory only: these strings are recorded as risks, never used to decide."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(default="", max_length=MAX_SUMMARY_CHARS)
    risks: list[str] = Field(default_factory=list, max_length=MAX_RISKS)

    def advisory_risks(self) -> tuple[str, ...]:
        cleaned = (" ".join(risk.split())[:MAX_RISK_CHARS] for risk in self.risks)
        return tuple(f"advisory: {risk}" for risk in cleaned if risk)


__all__ = ["MAX_RISKS", "MAX_RISK_CHARS", "MAX_SUMMARY_CHARS", "RiskSummary"]
