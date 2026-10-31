"""Validated, provider-independent completion contracts."""

import json
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from aether.communication.messages import AgentId


class ModelTier(StrEnum):
    NONE = "none"
    LIGHT = "light"
    STANDARD = "standard"
    REASONING = "reasoning"
    PREMIUM = "premium"


class CompletionMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    role: Literal["system", "user", "assistant"]
    content: str = Field(repr=False)


class CompletionRequest(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", allow_inf_nan=False, hide_input_in_errors=True
    )

    messages: tuple[CompletionMessage, ...] = Field(min_length=1, repr=False)
    max_tokens: int = Field(default=1024, ge=1)
    max_tokens_cap: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    response_format: Literal["text", "json_object", "json_schema"] = "text"
    json_schema: dict[str, JsonValue] | None = Field(default=None, repr=False)
    tier: ModelTier = ModelTier.STANDARD
    agent: AgentId = AgentId.ORCHESTR
    task_id: UUID | None = None
    objective_id: UUID | None = None
    provider: str | None = Field(default=None, min_length=1, max_length=64)
    model: str | None = Field(default=None, min_length=1, max_length=200)

    def with_model_limits(
        self, model: "ModelInfo", *, max_tokens_per_request: int | None = None
    ) -> "CompletionRequest":
        output_tokens = max(self.max_tokens, model.min_max_tokens)
        # A request-level cap below the model's minimum would fail on every call; the model
        # minimum wins there. Provider and per-request budget caps below it still fail.
        request_cap = (
            None if self.max_tokens_cap is None else max(self.max_tokens_cap, model.min_max_tokens)
        )
        caps = [
            cap
            for cap in (
                request_cap,
                (
                    max_tokens_per_request - self.input_token_estimate()
                    if max_tokens_per_request is not None
                    else None
                ),
            )
            if cap is not None
        ]
        if caps and output_tokens > min(caps):
            raise ValueError(
                f"required max_tokens={output_tokens} (model minimum={model.min_max_tokens}) "
                f"exceeds tokens per request output cap={min(caps)}"
            )
        return self.model_copy(
            update={
                "model": model.name,
                "max_tokens": output_tokens,
                "max_tokens_cap": request_cap,
            }
        )

    def input_token_estimate(self) -> int:
        """Conservative byte-based reservation, including message/schema framing."""
        content = [message.model_dump() for message in self.messages]
        return len(
            json.dumps(
                {"messages": content, "schema": self.json_schema}, ensure_ascii=False
            ).encode("utf-8")
        ) + 32 * len(self.messages)


class CompletionResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    text: str = Field(repr=False)
    model: str
    provider: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: float = Field(ge=0)
    cost_usd: float = Field(default=0, ge=0)
    cost_estimate_unknown: bool = False
    usage_estimated: bool = False
    finish_reason: str | None = None


class ModelInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    tiers: frozenset[ModelTier]
    min_max_tokens: int = Field(default=1, ge=1)


class ProviderHealth(BaseModel):
    available: bool
    healthy: bool
    detail: str
