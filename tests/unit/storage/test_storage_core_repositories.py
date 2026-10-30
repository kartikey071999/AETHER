from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from aether.storage.models import (
    ObjectiveRecord,
    RequirementRecord,
    RequirementStatus,
    TaskRecord,
    TaskStatus,
)
from aether.storage.repositories import (
    DependencyCycleError,
    InvalidTaskTransition,
    ObjectiveRepository,
    RequirementRepository,
    TaskDependencyRepository,
    TaskRepository,
)


async def test_objective_state_changes_are_audited(database, events_for):
    repository = ObjectiveRepository(database.sessions)
    objective_id = await repository.create(
        title="Learn OCR",
        priority=40,
        completion_criteria=["reads scanned PDFs"],
        stopping_conditions=["budget exhausted"],
    )
    await repository.update_priority(objective_id, 70, actor="ORCHESTR", reason="deadline")
    await repository.set_status(objective_id, "paused", actor="ORCHESTR", reason="user away")

    async with database.sessions() as session:
        objective = await session.get(ObjectiveRecord, objective_id)
    assert objective.priority == 70
    assert objective.base_priority == 40
    assert objective.status == "paused"
    assert objective.stopping_conditions == ["budget exhausted"]
    assert objective.last_priority_change_at is not None

    events = await events_for(objective_id)
    assert sorted(event.event_type for event in events) == [
        "objective.created",
        "objective.priority_changed",
        "objective.status_changed",
    ]
    assert {event.category for event in events} == {"objective"}
    assert {event.objective_id for event in events} == {objective_id}


async def test_objective_priority_outside_range_is_rejected(database, events_for, objective_id):
    repository = ObjectiveRepository(database.sessions)

    with pytest.raises(ValueError):
        await repository.update_priority(objective_id, 101, actor="ORCHESTR", reason="too high")

    assert [event.event_type for event in await events_for(objective_id)] == ["objective.created"]


async def test_task_lifecycle_records_timestamps_result_and_events(
    database, events_for, objective_id
):
    repository = TaskRepository(database.sessions)
    task_id = await repository.create(objective_id=objective_id, title="Install OCR")
    for status in (TaskStatus.READY, TaskStatus.SCHEDULED, TaskStatus.RUNNING):
        await repository.transition(task_id, status, actor="ORCHESTR")
    await repository.transition(task_id, "completed", actor="FORGE", result={"installed": True})

    async with database.sessions() as session:
        task = await session.get(TaskRecord, task_id)
    assert task.state == TaskStatus.COMPLETED
    assert task.scheduled_at is not None
    assert task.started_at is not None
    assert task.completed_at is not None
    assert task.result == {"installed": True}

    events = await events_for(task_id)
    assert len(events) == 5
    assert {event.category for event in events} == {"task"}
    assert {event.task_id for event in events} == {task_id}
    assert {event.objective_id for event in events} == {objective_id}


async def test_invalid_task_transition_changes_nothing(database, events_for, objective_id):
    repository = TaskRepository(database.sessions)
    task_id = await repository.create(objective_id=objective_id, title="Skip ahead")

    with pytest.raises(InvalidTaskTransition):
        await repository.transition(task_id, TaskStatus.RUNNING, actor="ORCHESTR")

    async with database.sessions() as session:
        task = await session.get(TaskRecord, task_id)
    assert task.state == TaskStatus.PENDING
    assert task.started_at is None
    assert [event.event_type for event in await events_for(task_id)] == ["task.created"]


async def test_task_idempotency_key_is_unique(database, objective_id):
    repository = TaskRepository(database.sessions)
    await repository.create(objective_id=objective_id, title="First", idempotency_key="job-1")

    with pytest.raises(IntegrityError):
        await repository.create(objective_id=objective_id, title="Again", idempotency_key="job-1")


async def test_requirement_status_changes_are_audited(database, events_for, objective_id):
    repository = RequirementRepository(database.sessions)
    requirement_id = await repository.create(
        objective_id=objective_id,
        title="Read scanned documents",
        requirement_type="capability",
        created_by="ARCHON",
        acceptance_criteria=["extracts text from a scanned PDF"],
        security_review_required=True,
    )
    await repository.set_status(
        requirement_id,
        RequirementStatus.APPROVED,
        actor="ORCHESTR",
        reason="needed for objective",
    )
    task_id = await TaskRepository(database.sessions).create(
        objective_id=objective_id,
        title="Implement OCR",
        requirement_id=requirement_id,
    )

    approved = await repository.list_by_status(RequirementStatus.APPROVED)
    assert [requirement.id for requirement in approved] == [requirement_id]
    async with database.sessions() as session:
        requirement = await session.get(RequirementRecord, requirement_id)
        task = await session.get(TaskRecord, task_id)
    assert requirement.security_review_required is True
    assert task.requirement_id == requirement_id

    events = await events_for(requirement_id)
    assert sorted(event.event_type for event in events) == [
        "requirement.created",
        "requirement.status_changed",
    ]
    assert {event.category for event in events} == {"requirement"}


async def test_task_dependencies_reject_cycles_and_duplicates(database, events_for, objective_id):
    tasks = TaskRepository(database.sessions)
    first = await tasks.create(objective_id=objective_id, title="First")
    second = await tasks.create(objective_id=objective_id, title="Second")
    third = await tasks.create(objective_id=objective_id, title="Third")
    dependencies = TaskDependencyRepository(database.sessions)

    assert await dependencies.add(second, first, actor="ORCHESTR")
    assert await dependencies.add(third, second, actor="ORCHESTR")
    assert not await dependencies.add(third, second, actor="ORCHESTR")
    with pytest.raises(DependencyCycleError):
        await dependencies.add(first, third, actor="ORCHESTR")
    with pytest.raises(DependencyCycleError):
        await dependencies.add(first, first, actor="ORCHESTR")
    with pytest.raises(LookupError):
        await dependencies.add(first, uuid4(), actor="ORCHESTR")

    assert await dependencies.list_dependencies(third) == [second]
    assert await dependencies.list_dependents(first) == [second]
    assert [event.event_type for event in await events_for(first)] == ["task.created"]
    assert sorted(event.event_type for event in await events_for(third)) == [
        "task.created",
        "task.dependency_added",
    ]

    assert await dependencies.remove(third, second, actor="ORCHESTR")
    assert not await dependencies.remove(third, second, actor="ORCHESTR")
    assert await dependencies.list_dependencies(third) == []
    assert "task.dependency_removed" in {event.event_type for event in await events_for(third)}
