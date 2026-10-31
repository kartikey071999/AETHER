"""Read-mostly API routes; privileged writes delegate to durable runtime services."""

import logging
from datetime import UTC
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from aether.agents.muse import record_user_response
from aether.api.queries import READ_COLLECTIONS
from aether.api.schemas import (
    AuthorizationResponse,
    ControlInput,
    ObjectiveSubmission,
    Page,
    QueuedObjective,
    QueuedResponse,
)
from aether.api.services import Pagination, Services, require_admin
from aether.communication.contracts import ObjectiveCreatePayload, UserDecision, build_message
from aether.communication.messages import AgentId
from aether.storage.models import AuthorizationRequestRecord, ObjectiveRecord, TaskStatus

reads = APIRouter()
admin = APIRouter(dependencies=[Depends(require_admin)])
_logger = logging.getLogger("aether.api")


@reads.get("/health")
async def health(api: Services) -> JSONResponse:
    try:
        status = await api.repository.health()
    except SQLAlchemyError as error:
        _logger.error("API health query failed", extra={"error_type": type(error).__name__})
        return api.response(
            {"status": "unavailable", "database": "unreachable", "api": "running"},
            status_code=503,
        )
    return api.response(status)


@reads.get("/agents", response_model=Page)
async def agents(api: Services, page: Pagination) -> JSONResponse:
    return api.response(await api.repository.agents(**page))


@reads.get("/objectives", response_model=Page)
async def objectives(api: Services, page: Pagination) -> JSONResponse:
    return api.response(await api.repository.objectives(**page))


@reads.get("/objectives/{objective_id}")
async def objective(objective_id: UUID, api: Services) -> JSONResponse:
    return api.response(await api.repository.objective(objective_id))


@reads.get("/tasks", response_model=Page)
async def tasks(
    api: Services,
    page: Pagination,
    state: TaskStatus | None = None,
    agent: Annotated[str | None, Query(max_length=24)] = None,
) -> JSONResponse:
    return api.response(await api.repository.tasks(state=state, agent=agent, **page))


@reads.get("/tasks/{task_id}")
async def task(task_id: UUID, api: Services, page: Pagination) -> JSONResponse:
    return api.response(await api.repository.task(task_id, **page))


@reads.get("/capabilities", response_model=Page)
async def capabilities(api: Services, page: Pagination) -> JSONResponse:
    return api.response(await api.repository.capabilities(**page))


@reads.get("/security/pending-approvals", response_model=Page)
async def pending_approvals(api: Services, page: Pagination) -> JSONResponse:
    return api.response(await api.repository.pending_approvals(**page))


@reads.get("/errors", response_model=Page)
async def errors(api: Services, page: Pagination) -> JSONResponse:
    return api.response(await api.repository.errors(**page))


@reads.get("/tests", response_model=Page)
async def tests(api: Services, page: Pagination) -> JSONResponse:
    return api.response(await api.repository.tests(**page))


@reads.get("/events", response_model=Page)
async def events(
    api: Services,
    page: Pagination,
    event_type: Annotated[str | None, Query(alias="type", max_length=64)] = None,
    task_id: UUID | None = None,
) -> JSONResponse:
    return api.response(await api.repository.events(event_type=event_type, task_id=task_id, **page))


@reads.get("/messages", response_model=Page)
async def messages(
    api: Services, page: Pagination, correlation_id: UUID | None = None
) -> JSONResponse:
    return api.response(await api.repository.messages(correlation_id=correlation_id, **page))


@reads.get("/providers/usage", response_model=Page)
async def provider_usage(api: Services, page: Pagination) -> JSONResponse:
    return api.response(await api.repository.provider_usage(api.provider_settings, **page))


def _collection_route(path: str):
    async def collection(api: Services, page: Pagination) -> JSONResponse:
        return api.response(await api.repository.page(READ_COLLECTIONS[path], **page))

    return collection


for _path in READ_COLLECTIONS:
    reads.add_api_route(
        _path,
        _collection_route(_path),
        methods=["GET"],
        response_model=Page,
        name=_path.strip("/").replace("/", "_"),
    )


@admin.post("/objectives", status_code=202, response_model=QueuedObjective)
async def submit_objective(body: ObjectiveSubmission, api: Services) -> JSONResponse:
    if body.parent_objective_id is not None:
        await api.repository.get(ObjectiveRecord, body.parent_objective_id)
    # The concrete contract class is required by build_message's type-to-message registry.
    payload = ObjectiveCreatePayload.model_validate(body.model_dump())
    message = build_message(AgentId.MUSE, AgentId.ORCHESTR, payload)
    await api.bus.publish(message)
    return api.response(
        QueuedObjective(message_id=message.message_id, correlation_id=message.correlation_id),
        status_code=202,
    )


@admin.post("/authorizations/{request_id}/respond", status_code=202, response_model=QueuedResponse)
async def respond_authorization(
    request_id: UUID, body: AuthorizationResponse, api: Services
) -> JSONResponse:
    request = await api.repository.get(AuthorizationRequestRecord, request_id)
    # MUSE checks expiry again under its transaction lock and enforces the delivered audience.
    if request["status"] != "pending":
        raise HTTPException(409, "Authorization request is not pending")
    expires = request["expires_at"]
    if expires is not None:
        expires = expires.replace(tzinfo=UTC) if expires.tzinfo is None else expires
        if expires <= api.clock():
            raise HTTPException(409, "Authorization request has expired")
    interaction_id = await record_user_response(
        api.muse_context(),
        text=body.text,
        person_id=body.person_id,
        authorization_request_id=request_id,
        decision=UserDecision(body.decision),
        response_id=body.response_id,
        channel="console",
    )
    return api.response(
        QueuedResponse(request_id=request_id, interaction_id=interaction_id), status_code=202
    )


@admin.post("/tasks/{task_id}/pause", status_code=202)
async def pause_task(
    task_id: UUID, api: Services, body: Annotated[ControlInput | None, Body()] = None
) -> JSONResponse:
    result = await api.control.request_pause(task_id, reason=body.reason if body else "")
    return api.response(result, status_code=202 if result.outcome == "pending" else 200)


@admin.post("/tasks/{task_id}/cancel", status_code=202)
async def cancel_task(
    task_id: UUID, api: Services, body: Annotated[ControlInput | None, Body()] = None
) -> JSONResponse:
    result = await api.control.request_cancel(task_id, reason=body.reason if body else "")
    return api.response(result, status_code=202 if result.outcome == "pending" else 200)


@admin.post("/tasks/{task_id}/resume")
async def resume_task(
    task_id: UUID, api: Services, body: Annotated[ControlInput | None, Body()] = None
) -> JSONResponse:
    if not await api.control.resume(task_id, reason=body.reason if body else ""):
        raise HTTPException(409, "Only paused tasks can be resumed")
    return api.response({"task_id": task_id, "action": "resume", "state": "ready"})
