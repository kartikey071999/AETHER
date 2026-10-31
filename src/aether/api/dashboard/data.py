"""Dashboard reads: persistent state only, scrubbed through the API's own sanitizer."""

from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select

from aether.api.queries import READ_COLLECTIONS
from aether.api.services import ApiServices
from aether.storage.models import (
    AuthorizationRequestRecord,
    CapabilityRecord,
    InteractionDirection,
    InteractionRecord,
    ObjectiveRecord,
    RequirementRecord,
    ResearchFindingRecord,
    SecurityDecisionRecord,
    TaskDependencyRecord,
    TaskRecord,
    TaskStatus,
    TestRunRecord,
)

LIST_LIMIT = 100
WIDE_LIMIT = 200


async def _items(api: ApiServices, statement: Select[Any], limit: int = LIST_LIMIT) -> list[Any]:
    page = await api.repository.page(statement, limit=limit, offset=0)
    return api.sanitize(page["items"])


async def task_counts(api: ApiServices) -> dict[str, int]:
    async with api.repository.sessions() as session:
        rows = await session.execute(
            select(TaskRecord.state, func.count()).group_by(TaskRecord.state)
        )
        found = {state: count for state, count in rows}
    # Every known state is listed so an empty state shows as zero rather than missing.
    counts = {status.value: 0 for status in TaskStatus}
    counts.update(found)
    return counts


async def overview(api: ApiServices) -> dict[str, Any]:
    agents = await api.repository.agents(limit=WIDE_LIMIT, offset=0)
    objectives = await api.repository.objectives(limit=WIDE_LIMIT, offset=0)
    errors = await api.repository.errors(limit=10, offset=0)
    return {
        "agents": api.sanitize(agents)["items"],
        "objectives": api.sanitize(objectives)["items"],
        "errors": api.sanitize(errors)["items"],
        "task_counts": await task_counts(api),
    }


async def objectives(api: ApiServices) -> dict[str, Any]:
    return api.sanitize(await api.repository.objectives(limit=WIDE_LIMIT, offset=0))


async def objective_detail(api: ApiServices, objective_id: UUID) -> dict[str, Any]:
    objective = api.sanitize(await api.repository.objective(objective_id))
    tasks = await _items(
        api,
        select(TaskRecord)
        .where(TaskRecord.objective_id == objective_id)
        .order_by(TaskRecord.created_at, TaskRecord.id),
        WIDE_LIMIT,
    )
    task_ids = [UUID(task["id"]) for task in tasks]
    dependencies = (
        await _items(
            api,
            select(TaskDependencyRecord).where(TaskDependencyRecord.task_id.in_(task_ids)),
            WIDE_LIMIT * 5,
        )
        if task_ids
        else []
    )
    requirements = await _items(
        api,
        select(RequirementRecord)
        .where(RequirementRecord.objective_id == objective_id)
        .order_by(RequirementRecord.priority.desc(), RequirementRecord.created_at),
    )
    findings = await _items(
        api,
        select(ResearchFindingRecord)
        .where(ResearchFindingRecord.objective_id == objective_id)
        .order_by(ResearchFindingRecord.created_at.desc()),
    )
    capabilities = await _items(
        api,
        select(CapabilityRecord)
        .where(CapabilityRecord.objective_id == objective_id)
        .order_by(CapabilityRecord.name),
    )
    for capability in capabilities:
        runs = await api.repository.tests(limit=10, offset=0, capability_id=UUID(capability["id"]))
        capability["verification_evidence"] = api.sanitize(runs)["items"]
    decisions = (
        await _items(
            api,
            select(SecurityDecisionRecord)
            .where(SecurityDecisionRecord.task_id.in_(task_ids))
            .order_by(SecurityDecisionRecord.created_at.desc()),
        )
        if task_ids
        else []
    )
    authorizations = await _items(
        api,
        select(AuthorizationRequestRecord)
        .where(AuthorizationRequestRecord.objective_id == objective_id)
        .order_by(AuthorizationRequestRecord.requested_at.desc()),
    )
    final = await _items(
        api,
        select(InteractionRecord)
        .where(
            InteractionRecord.objective_id == objective_id,
            InteractionRecord.direction == InteractionDirection.OUTBOUND.value,
        )
        .order_by(InteractionRecord.occurred_at.desc()),
        1,
    )
    return {
        "objective": objective,
        "tasks": tasks,
        "dependencies": dependencies,
        "requirements": requirements,
        "findings": findings,
        "capabilities": capabilities,
        "decisions": decisions,
        "authorizations": authorizations,
        "final_message": final[0] if final else None,
    }


async def tasks(api: ApiServices, *, state: TaskStatus | None, agent: str | None) -> dict[str, Any]:
    page = await api.repository.tasks(state=state, agent=agent, limit=WIDE_LIMIT, offset=0)
    return api.sanitize(page)


async def security(api: ApiServices) -> dict[str, Any]:
    pending = api.sanitize(await api.repository.pending_approvals(limit=WIDE_LIMIT, offset=0))
    decisions = await api.repository.page(
        READ_COLLECTIONS["/security/decisions"], limit=WIDE_LIMIT, offset=0
    )
    requests = [UUID(item["id"]) for item in pending["items"]]
    delivered = (
        await _items(
            api,
            select(InteractionRecord)
            .where(
                InteractionRecord.authorization_request_id.in_(requests),
                InteractionRecord.direction == InteractionDirection.OUTBOUND.value,
                InteractionRecord.person_id.is_not(None),
            )
            .order_by(InteractionRecord.occurred_at),
            WIDE_LIMIT,
        )
        if requests
        else []
    )
    # The API only accepts an answer from the person the question was delivered to.
    audience = {row["authorization_request_id"]: row["person_id"] for row in delivered}
    for item in pending["items"]:
        item["audience_person_id"] = audience.get(item["id"])
    return {"pending": pending, "decisions": api.sanitize(decisions)}


async def resources(api: ApiServices) -> dict[str, Any]:
    page = await api.repository.resources(since=None, until=None, task_id=None, limit=120, offset=0)
    return api.sanitize(page)


async def providers(api: ApiServices) -> dict[str, Any]:
    page = await api.repository.provider_usage(api.provider_settings, limit=WIDE_LIMIT, offset=0)
    return api.sanitize(page)


async def events(api: ApiServices, *, event_type: str | None) -> dict[str, Any]:
    page = await api.repository.events(
        event_type=event_type, task_id=None, limit=WIDE_LIMIT, offset=0
    )
    return api.sanitize(page)


async def task_detail(api: ApiServices, task_id: UUID) -> dict[str, Any]:
    """One task with its runs, verification evidence, security decisions and events."""
    task = api.sanitize(await api.repository.task(task_id, limit=50, offset=0))
    tests = await _items(
        api,
        select(TestRunRecord)
        .where(TestRunRecord.task_id == task_id)
        .order_by(TestRunRecord.started_at.desc(), TestRunRecord.id),
        20,
    )
    decisions = await _items(
        api,
        select(SecurityDecisionRecord)
        .where(SecurityDecisionRecord.task_id == task_id)
        .order_by(SecurityDecisionRecord.created_at.desc()),
        20,
    )
    objective = await _items(
        api, select(ObjectiveRecord).where(ObjectiveRecord.id == UUID(task["objective_id"])), 1
    )
    return {
        "task": task,
        "objective": (
            {"id": objective[0]["id"], "title": objective[0]["title"]} if objective else None
        ),
        "tests": tests,
        "decisions": decisions,
    }
