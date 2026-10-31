"""Deterministic rendering and privacy-scoped personalization."""

import json
import re
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select

from aether.agents.base import AgentContext
from aether.agents.muse.prompts import OPENING_POLISH
from aether.agents.muse.repository import MuseRepository
from aether.agents.muse.schemas import CommunicationPreferences, HandoffOptions, OpeningPolish
from aether.communication.contracts import CommunicationRequestPayload, Urgency
from aether.communication.messages import AgentId
from aether.models.base import ProviderError
from aether.models.contracts import CompletionMessage, CompletionRequest, ModelTier
from aether.models.facade import ModelCallContext, ModelUnavailable
from aether.security.errors import OperationBlocked
from aether.security.untrusted import UntrustedContent
from aether.storage.models import PersonRecord
from aether.storage.repositories.knowledge_graph import KnowledgeGraphRepository

_IDENTIFIERS = re.compile(r"\b[\w.-]*\d[\w.-]*\b", re.UNICODE)


async def require_permission(ctx: AgentContext, action: str) -> None:
    if ctx.permission_service is None or not await ctx.permission_service.is_allowed(
        AgentId.MUSE, action
    ):
        raise PermissionError(f"MUSE requires {action}")


async def active_person(ctx: AgentContext, person_id: UUID) -> str:
    async with ctx.sessions() as session:
        access = await session.scalar(
            select(PersonRecord.access_level).where(
                PersonRecord.id == person_id, PersonRecord.deleted_at.is_(None)
            )
        )
    if access is None:
        raise LookupError("Audience does not exist or was deleted")
    if access in {"none", "denied", "deleted"}:
        raise PermissionError("Audience access is denied")
    return access


async def personalization(
    request: CommunicationRequestPayload, options: HandoffOptions, ctx: AgentContext
) -> tuple[CommunicationPreferences, list[str]]:
    if request.audience is None:
        return CommunicationPreferences(), []
    access = await active_person(ctx, request.audience)
    if ctx.permission_service is None or not await ctx.permission_service.is_allowed(
        AgentId.MUSE, "preferences.read"
    ):
        ctx.logger.info("MUSE personalization omitted: preferences.read is not permitted")
        return CommunicationPreferences(), []
    people = MuseRepository(ctx.sessions)
    values = await people.get_preferences(request.audience)
    values = {
        key: value for key, value in values.items() if key in CommunicationPreferences.model_fields
    }
    try:
        preferences = CommunicationPreferences.model_validate(values)
    except ValidationError:
        ctx.logger.warning("MUSE ignored unsupported stored communication preferences")
        preferences = CommunicationPreferences()
    names = []
    if access == "full":
        names = await KnowledgeGraphRepository(ctx.sessions).scoped_entity_names(
            request.audience,
            entity_ids=options.context_entity_ids,
            names=options.context_names,
            objective_id=request.objective_id,
            task_id=request.task_id,
        )
    return preferences, names


def handoff_options(request: CommunicationRequestPayload) -> HandoffOptions:
    allowed = HandoffOptions.model_fields
    try:
        return HandoffOptions.model_validate(
            {key: value for key, value in request.personalization_context.items() if key in allowed}
        )
    except ValidationError:
        raise ValueError("Invalid MUSE handoff presentation options") from None


def routine_notification(request: CommunicationRequestPayload, options: HandoffOptions) -> bool:
    return (
        options.routine
        and request.urgency in {Urgency.LOW, Urgency.NORMAL}
        and not request.uncertain
        and not request.blockers
        and not request.decisions_required
        and not request.authorization_required
    )


def opening_for(preferences: CommunicationPreferences) -> str:
    if preferences.formality == "formal":
        return "Please review the update below."
    if preferences.tone == "warm":
        return "Here is the update."
    return ""


def result_lines(result: dict[str, Any]) -> list[str]:
    """Render supplied fields without promoting reported results to verified facts."""
    lines = []
    count = result.get("tasks_completed")
    objective = result.get("objective")
    summarized = isinstance(count, int) and not isinstance(count, bool) and count >= 0
    if summarized:
        task_label = "task" if count == 1 else "tasks"
        summary = f"{count} {task_label} completed"
        if isinstance(objective, str) and objective.strip():
            summary += f" for '{objective}'"
        lines.append(summary)

    def field(label: str, value: Any) -> None:
        if isinstance(value, dict):
            if not value:
                lines.append(f"{label}: (empty mapping)")
            for key, item in value.items():
                field(f"{label}: {key}", item)
        elif isinstance(value, list):
            if not value:
                lines.append(f"{label}: (empty list)")
            for item in value:
                field(label, item)
        else:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            lines.append(f"{label}: {text}")

    for key, value in result.items():
        if summarized and (
            key == "tasks_completed"
            or (key == "objective" and isinstance(objective, str) and objective.strip())
        ):
            continue
        field(key, value)
    return lines or ["No result fields supplied."]


def render_message(
    request: CommunicationRequestPayload,
    preferences: CommunicationPreferences,
    options: HandoffOptions,
    *,
    deadline: datetime | None = None,
    context_names: list[str] | None = None,
    opening: str | None = None,
) -> str:
    short = preferences.length == "short" or preferences.detail == "minimal"
    detailed = preferences.length == "long" or preferences.detail == "detailed"
    parts = [opening if opening is not None else opening_for(preferences)]

    def section(title: str, lines: list[str]) -> None:
        content = lines or ["Not supplied."]
        if short:
            parts.append(f"{title}: " + " | ".join(content))
        else:
            parts.append(f"## {title}\n" + "\n".join(f"- {line}" for line in content))

    section("What happened", [request.what_happened])
    results = [line for result in request.results for line in result_lines(result)]
    section("Reported results (not independently verified by MUSE)", results)
    section("Verified", request.verified)
    section("Uncertain", request.uncertain)
    section("Blockers", request.blockers)
    questions = []
    for decision in request.decisions_required:
        question = decision if decision.endswith("?") else f"{decision}?"
        choices = options.decision_options.get(decision, [])
        choices_text = "; ".join(choices) if choices else "not supplied; state your decision"
        expires = options.decision_deadlines.get(decision) or deadline
        expiry_text = expires.isoformat() if expires else "not supplied"
        questions.append(
            f"{question}\n  Options: {choices_text}.\n  Deadline: {expiry_text}.\n"
            "  No response = no authorization."
        )
    section("Decisions required", questions)
    authorization = []
    if request.authorization_required:
        expires = options.authorization_expires_at or deadline
        expiry_text = expires.isoformat() if expires else "not supplied"
        authorization = [
            "Do you authorize the requested operation?",
            f"Request: {request.authorization_request_id}",
            "Options: Approve; Deny.",
            f"Expiry: {expiry_text}.",
            "No response = no authorization.",
        ]
    section("Authorization requests", authorization)
    if context_names:
        section("Relevant context (not verification)", context_names)
    if detailed and (request.objective_id or request.task_id):
        section(
            "References",
            [
                f"{label}: {value}"
                for label, value in (("Objective", request.objective_id), ("Task", request.task_id))
                if value
            ],
        )
    text = "\n\n".join(part for part in parts if part) + "\n"
    source = {
        "handoff": request.model_dump(mode="json"),
        "options": options.model_dump(mode="json"),
        "deadlines": [
            value.isoformat()
            for value in [
                deadline,
                options.authorization_expires_at,
                *options.decision_deadlines.values(),
            ]
            if value is not None
        ],
    }

    def source_identifiers(value: Any) -> set[str]:
        # Inspect decoded strings so JSON escapes cannot alter supplied identifiers.
        if isinstance(value, dict):
            return set().union(
                *(source_identifiers(item) for pair in value.items() for item in pair)
            )
        if isinstance(value, list):
            return set().union(*(source_identifiers(item) for item in value))
        literal = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return set(_IDENTIFIERS.findall(literal))

    if not set(_IDENTIFIERS.findall(text)).issubset(source_identifiers(source)):
        raise ValueError("Rendered communication contains an unsupported number or identifier")
    return text


async def polish_opening(
    preferences: CommunicationPreferences,
    ctx: AgentContext,
    *,
    objective_id: UUID | None = None,
    task_id: UUID | None = None,
) -> str:
    fallback = opening_for(preferences)
    if ctx.models is None:
        ctx.logger.info("MUSE template fallback: model facade is unavailable")
        return fallback
    data = UntrustedContent(
        source="explicit-communication-preferences", content=preferences.model_dump_json()
    )
    request = CompletionRequest(
        tier=ModelTier.LIGHT,
        messages=(
            CompletionMessage(role="system", content=OPENING_POLISH),
            CompletionMessage(role="user", content=data.quote()),
        ),
        response_format="json_object",
        max_tokens=128,
        temperature=0,
    )
    try:
        response = await ctx.models.complete(
            request, ModelCallContext(objective_id=objective_id, task_id=task_id)
        )
        candidate = OpeningPolish.model_validate_json(response.text).opening
    except (ModelUnavailable, ProviderError, OperationBlocked, ValidationError):
        ctx.logger.warning(
            "MUSE template fallback: optional opening polish was unavailable or invalid"
        )
        return fallback
    # A model cannot override the explicit presentation preference either.
    return candidate if candidate == fallback else fallback


def feedback_preferences(text: str) -> dict[str, str]:
    commands = {
        "shorter please": {"length": "short"},
        "longer please": {"length": "long"},
        "more detail please": {"detail": "detailed"},
        "less detail please": {"detail": "minimal"},
        "more formal please": {"formality": "formal"},
        "less formal please": {"formality": "casual"},
        "warmer please": {"tone": "warm"},
        "more direct please": {"tone": "direct"},
    }
    value = commands.get(text.strip().lower().rstrip(".!"))
    if value is None:
        raise ValueError("Feedback must specify a supported explicit communication preference")
    return value
