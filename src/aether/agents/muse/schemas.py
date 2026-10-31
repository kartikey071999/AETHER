"""Validated local presentation options and durable workflow checkpoints."""

from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aether.communication.contracts import Sha256Hex, UserResponsePayload, UtcDatetime


class MuseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class CommunicationPreferences(MuseModel):
    length: Literal["short", "normal", "long"] = "normal"
    tone: Literal["neutral", "direct", "warm"] = "neutral"
    detail: Literal["minimal", "normal", "detailed"] = "normal"
    formality: Literal["neutral", "casual", "formal"] = "neutral"
    channel: Literal["file", "console"] = "file"


class HandoffOptions(MuseModel):
    decision_options: dict[str, list[str]] = Field(default_factory=dict)
    decision_deadlines: dict[str, UtcDatetime] = Field(default_factory=dict)
    authorization_expires_at: UtcDatetime | None = None
    context_entity_ids: list[UUID] = Field(default_factory=list, max_length=20)
    context_names: list[str] = Field(default_factory=list, max_length=20)
    routine: bool = False
    # What the notice is about (e.g. ORCHESTR's update kind); part of the dedupe key.
    notice_kind: str | None = Field(default=None, min_length=1, max_length=64)


class OpeningPolish(MuseModel):
    opening: Literal["", "Here is the update.", "Please review the update below."]


class FileWriteReceipt(MuseModel):
    path: Path
    bytes_written: int = Field(ge=0)
    sha256: Sha256Hex


class OutboxCheckpoint(MuseModel):
    request_message_id: UUID
    interaction_id: UUID
    audience: UUID | None
    channel: Literal["file", "console"]
    path: Path
    content_hash: str
    rendered_text_hash: str
    correlation_id: UUID
    objective_id: UUID | None = None
    task_id: UUID | None = None
    requirement_id: UUID | None = None
    prepared_at: UtcDatetime
    # Dedupe identity (T-047); absent on checkpoints written before it existed.
    notice_kind: str | None = None
    dedupe_hash: str | None = None


class ResponseCheckpoint(MuseModel):
    response_id: UUID
    interaction_id: UUID
    person_id: UUID | None
    channel: Literal["file", "console"]
    correlation_id: UUID
    requirement_id: UUID | None = None
    response: UserResponsePayload


class StoredOutbox(MuseModel):
    checkpoint: OutboxCheckpoint
    text: str
    delivered_at: datetime | None = None
