"""HTTP contracts; objective input uses the inter-agent contract unchanged."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from aether.communication.contracts import ObjectiveCreatePayload

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class Page(BaseModel):
    items: list[dict[str, JsonValue]]
    total: int
    limit: int
    offset: int


class ObjectiveSubmission(ObjectiveCreatePayload):
    generated: Literal[False] = False


class AuthorizationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal["approved", "denied"]
    text: str = Field(min_length=1, max_length=16_384)
    person_id: UUID | None = None
    response_id: UUID | None = None


class ControlInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(default="", max_length=500)


class QueuedObjective(BaseModel):
    message_id: UUID
    correlation_id: UUID
    status: Literal["queued"] = "queued"


class QueuedResponse(BaseModel):
    request_id: UUID
    interaction_id: UUID
    status: Literal["queued"] = "queued"
