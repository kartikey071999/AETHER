"""When ORCHESTR talks to the user (via MUSE) and what the handoff contains.

The decision is a pure function so it can be tested exhaustively. ORCHESTR never writes user-facing
text; it only builds the structured ``CommunicationRequestPayload`` that MUSE renders.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from aether.communication.contracts import CommunicationRequestPayload, Urgency

MAX_RESULTS = 10
MAX_TEXT = 500


class UpdateKind(StrEnum):
    OBJECTIVE_COMPLETED = "objective_completed"
    DECISION_REQUIRED = "decision_required"
    AUTHORIZATION_REQUIRED = "authorization_required"
    BLOCKER = "blocker"
    ESCALATION = "escalation"
    DEADLINE_AT_RISK = "deadline_at_risk"
    TASK_COMPLETED = "task_completed"
    PROGRESS = "progress"
    RETRY = "retry"
    HEALTH = "health"
    FINDING = "finding"


@dataclass(frozen=True, slots=True)
class UpdateEvent:
    kind: UpdateKind
    user_originated: bool = True
    important: bool = False
    hours_to_deadline: float | None = None


def should_notify_user(event: UpdateEvent, deadline_window_hours: float = 24.0) -> bool:
    """True only for outcomes the user needs: results, decisions, blockers, deadline risk."""
    kind = event.kind
    if kind is UpdateKind.OBJECTIVE_COMPLETED:
        return event.user_originated
    if kind in (
        UpdateKind.DECISION_REQUIRED,
        UpdateKind.AUTHORIZATION_REQUIRED,
        UpdateKind.ESCALATION,
    ):
        return True
    if kind is UpdateKind.BLOCKER:
        return event.important
    if kind is UpdateKind.DEADLINE_AT_RISK:
        return (
            event.hours_to_deadline is not None and event.hours_to_deadline <= deadline_window_hours
        )
    return False


def urgency_for(event: UpdateEvent) -> Urgency:
    if event.kind in (UpdateKind.AUTHORIZATION_REQUIRED, UpdateKind.DECISION_REQUIRED):
        return Urgency.HIGH
    if event.kind in (UpdateKind.ESCALATION, UpdateKind.BLOCKER):
        return Urgency.HIGH
    if event.kind is UpdateKind.DEADLINE_AT_RISK:
        hours = event.hours_to_deadline
        return Urgency.CRITICAL if hours is not None and hours <= 1 else Urgency.HIGH
    return Urgency.NORMAL


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def build_handoff(
    event: UpdateEvent,
    *,
    what_happened: str,
    objective_id: UUID | None = None,
    task_id: UUID | None = None,
    results: Sequence[dict[str, Any]] = (),
    verified: Sequence[str] = (),
    uncertain: Sequence[str] = (),
    blockers: Sequence[str] = (),
    decisions_required: Sequence[str] = (),
    authorization_request_id: UUID | None = None,
) -> CommunicationRequestPayload:
    """Structured facts for MUSE: what happened, what is proven, what is not, what is needed."""
    return CommunicationRequestPayload(
        what_happened=_clip(what_happened),
        results=[
            {key: _clip(value) if isinstance(value, str) else value for key, value in item.items()}
            for item in list(results)[:MAX_RESULTS]
        ],
        verified=[_clip(item) for item in verified][:MAX_RESULTS],
        uncertain=[_clip(item) for item in uncertain][:MAX_RESULTS],
        blockers=[_clip(item) for item in blockers][:MAX_RESULTS],
        decisions_required=[_clip(item) for item in decisions_required][:MAX_RESULTS],
        authorization_required=authorization_request_id is not None,
        authorization_request_id=authorization_request_id,
        urgency=urgency_for(event),
        objective_id=objective_id,
        task_id=task_id,
    )
