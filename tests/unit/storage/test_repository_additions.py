import asyncio
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from aether.observability.events import EVENT_CATEGORIES, EVENT_TYPES
from aether.observability.recorder import EventRecorder
from aether.storage.models import (
    AuthorizationRequestRecord,
    ObjectiveRecord,
    RequirementRecord,
    RequirementStatus,
    TaskRecord,
)
from aether.storage.repositories import (
    ALLOWED_REQUIREMENT_TRANSITIONS,
    AuthorizationRequestRepository,
    InvalidRequirementTransition,
    ObjectiveRepository,
    RequirementConflictError,
    RequirementRepository,
    TaskRepository,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def assert_typed(event, kind, *, actor="ORCHESTR", correlation_id=None):
    assert event.event_type == kind
    assert event.category == EVENT_CATEGORIES[kind].value
    assert event.actor == actor
    if correlation_id is not None:
        assert event.correlation_id == correlation_id
    EVENT_TYPES[kind].model_validate(event.payload)


async def test_objective_details_and_base_priority_emit_one_event_each(
    database, objective_id, events_for
):
    repository = ObjectiveRepository(database.sessions)
    correlation_id = uuid4()
    await repository.update_details(
        objective_id,
        description="Extract offline",
        completion_criteria=["reads PDFs"],
        stopping_conditions=["budget exhausted"],
        deadline=NOW,
        review_at=NOW - timedelta(days=1),
        actor="ORCHESTR",
        correlation_id=correlation_id,
    )
    events = await events_for(objective_id)
    assert len(events) == 2
    assert_typed(events[-1], "objective.updated", correlation_id=correlation_id)
    assert events[-1].payload["changes"]["deadline"] == NOW.isoformat()

    await repository.set_base_priority(
        objective_id, 80, actor="ORCHESTR", reason="strategic", correlation_id=correlation_id
    )
    async with database.sessions() as session:
        record = await session.get(ObjectiveRecord, objective_id)
    assert record.description == "Extract offline"
    assert record.completion_criteria == ["reads PDFs"]
    assert record.stopping_conditions == ["budget exhausted"]
    assert record.deadline.replace(tzinfo=UTC) == NOW
    assert record.review_at.replace(tzinfo=UTC) == NOW - timedelta(days=1)
    assert record.base_priority == 80
    assert record.priority == 50
    assert record.last_priority_change_at is None
    events = await events_for(objective_id)
    assert len(events) == 3
    assert_typed(events[-1], "objective.base_priority_changed", correlation_id=correlation_id)
    assert events[-1].payload == {"from_priority": 50, "to_priority": 80, "reason": "strategic"}

    await repository.update_details(
        objective_id,
        description="",
        completion_criteria=[],
        stopping_conditions=[],
        deadline=None,
        review_at=None,
        actor="ORCHESTR",
    )
    async with database.sessions() as session:
        record = await session.get(ObjectiveRecord, objective_id)
    assert record.description == ""
    assert record.completion_criteria == record.stopping_conditions == []
    assert record.deadline is record.review_at is None
    assert len(await events_for(objective_id)) == 4


@pytest.mark.parametrize("priority", [-1, 101])
async def test_invalid_priorities_do_not_write(database, objective_id, events_for, priority):
    objectives = ObjectiveRepository(database.sessions)
    tasks = TaskRepository(database.sessions)
    task_id = await tasks.create(objective_id=objective_id, title="Task")
    with pytest.raises(ValueError):
        await objectives.set_base_priority(objective_id, priority, actor="ORCHESTR", reason="x")
    with pytest.raises(ValueError):
        await tasks.set_priority(task_id, priority, actor="ORCHESTR", reason="x")
    assert len(await events_for(objective_id)) == len(await events_for(task_id)) == 1


@pytest.mark.parametrize("value", [0, 100])
async def test_priority_and_cpu_bounds_are_inclusive(database, objective_id, value):
    objectives = ObjectiveRepository(database.sessions)
    tasks = TaskRepository(database.sessions)
    task_id = await tasks.create(objective_id=objective_id, title="Task")
    await objectives.set_base_priority(objective_id, value, actor="ORCHESTR", reason="boundary")
    await tasks.set_priority(task_id, value, actor="ORCHESTR", reason="boundary")
    await tasks.set_estimates(
        task_id, duration_s=0, ram_mb=0, disk_mb=0, cpu_percent=value, actor="ORCHESTR"
    )
    async with database.sessions() as session:
        objective = await session.get(ObjectiveRecord, objective_id)
        task = await session.get(TaskRecord, task_id)
    assert objective.base_priority == task.priority == task.estimated_cpu_percent == value


async def test_task_updates_emit_one_typed_event_each(database, objective_id, events_for):
    repository = TaskRepository(database.sessions)
    task_id = await repository.create(objective_id=objective_id, title="Task")
    correlation_id = uuid4()
    await repository.set_priority(
        task_id, 90, actor="ORCHESTR", reason="deadline", correlation_id=correlation_id
    )
    events = await events_for(task_id)
    assert len(events) == 2
    assert_typed(events[-1], "task.priority_changed", correlation_id=correlation_id)
    assert events[-1].payload == {"from_priority": 50, "to_priority": 90, "reason": "deadline"}
    await repository.set_estimates(
        task_id,
        duration_s=60,
        ram_mb=256,
        disk_mb=512,
        cpu_percent=25,
        actor="ORCHESTR",
        correlation_id=correlation_id,
    )
    async with database.sessions() as session:
        record = await session.get(TaskRecord, task_id)
    assert record.priority == 90
    assert record.estimated_duration_seconds == 60
    assert record.estimated_ram_mb == 256
    assert record.estimated_disk_mb == 512
    assert record.estimated_cpu_percent == 25
    events = await events_for(task_id)
    assert len(events) == 3
    assert_typed(events[-1], "task.estimates_changed", correlation_id=correlation_id)
    assert events[-1].objective_id == objective_id
    assert events[-1].task_id == task_id
    assert events[-1].payload == {
        "duration_s": 60,
        "ram_mb": 256,
        "disk_mb": 512,
        "cpu_percent": 25,
    }
    await repository.set_estimates(
        task_id, duration_s=None, ram_mb=0, disk_mb=0, cpu_percent=None, actor="ORCHESTR"
    )
    async with database.sessions() as session:
        record = await session.get(TaskRecord, task_id)
    assert record.estimated_duration_seconds is record.estimated_cpu_percent is None


@pytest.mark.parametrize(
    "changes",
    [
        {"duration_s": -1},
        {"ram_mb": -1},
        {"disk_mb": -1},
        {"cpu_percent": -0.1},
        {"cpu_percent": 100.1},
        {"cpu_percent": float("nan")},
        {"cpu_percent": float("inf")},
    ],
)
async def test_invalid_estimates_do_not_write(database, objective_id, events_for, changes):
    repository = TaskRepository(database.sessions)
    task_id = await repository.create(objective_id=objective_id, title="Task")
    values = {"duration_s": 1, "ram_mb": 1, "disk_mb": 1, "cpu_percent": 1, **changes}
    with pytest.raises(ValueError):
        await repository.set_estimates(task_id, actor="ORCHESTR", **values)
    assert len(await events_for(task_id)) == 1


async def test_new_methods_reject_missing_records(database):
    with pytest.raises(LookupError):
        await ObjectiveRepository(database.sessions).set_base_priority(
            uuid4(), 50, actor="ORCHESTR", reason="missing"
        )
    with pytest.raises(LookupError):
        await ObjectiveRepository(database.sessions).update_details(
            uuid4(),
            description="",
            completion_criteria=[],
            stopping_conditions=[],
            deadline=None,
            review_at=None,
            actor="ORCHESTR",
        )
    with pytest.raises(LookupError):
        await TaskRepository(database.sessions).set_priority(
            uuid4(), 50, actor="ORCHESTR", reason="missing"
        )
    with pytest.raises(LookupError):
        await TaskRepository(database.sessions).set_estimates(
            uuid4(), duration_s=None, ram_mb=0, disk_mb=0, cpu_percent=None, actor="ORCHESTR"
        )


async def test_requirement_create_is_idempotent_after_status_changes(
    database, objective_id, events_for
):
    repository = RequirementRepository(database.sessions)
    chosen_id = uuid4()
    values = {
        "requirement_id": chosen_id,
        "objective_id": objective_id,
        "title": "OCR",
        "description": "local",
        "requirement_type": "capability",
        "created_by": "SCOUT",
        "acceptance_criteria": ["offline"],
        "priority": 60,
    }
    assert await repository.create(**values) == chosen_id
    assert await repository.create(**values, correlation_id=uuid4()) == chosen_id
    assert len(await events_for(chosen_id)) == 1
    await repository.set_status(chosen_id, RequirementStatus.EVALUATING, actor="SCOUT")
    assert await repository.create(**values) == chosen_id
    assert len(await events_for(chosen_id)) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "changed"},
        {"description": "changed"},
        {"acceptance_criteria": ["new"]},
        {"requirement_type": "security"},
        {"created_by": "FORGE"},
        {"priority": 51},
        {"security_review_required": True},
        {"objective_id": uuid4()},
        {"task_id": uuid4()},
        {"source_finding_id": uuid4()},
    ],
)
async def test_requirement_id_conflicts_compare_all_content(
    database, objective_id, events_for, changes
):
    repository = RequirementRepository(database.sessions)
    chosen_id = uuid4()
    values = {
        "requirement_id": chosen_id,
        "objective_id": objective_id,
        "title": "OCR",
        "requirement_type": "capability",
        "created_by": "SCOUT",
    }
    await repository.create(**values)
    with pytest.raises(RequirementConflictError, match="different content"):
        await repository.create(**{**values, **changes})
    assert len(await events_for(chosen_id)) == 1


async def test_concurrent_requirement_create_writes_only_one_event(
    database, objective_id, events_for
):
    repository = RequirementRepository(database.sessions)
    chosen_id = uuid4()
    values = {
        "requirement_id": chosen_id,
        "objective_id": objective_id,
        "title": "OCR",
        "requirement_type": "capability",
        "created_by": "SCOUT",
    }
    assert await asyncio.gather(*(repository.create(**values) for _ in range(3))) == [chosen_id] * 3
    assert len(await events_for(chosen_id)) == 1


@pytest.mark.parametrize("verdict", [RequirementStatus.VERIFIED, RequirementStatus.FAILED])
async def test_requirement_evaluation_and_implementation_lifecycle(
    database, objective_id, events_for, verdict
):
    repository = RequirementRepository(database.sessions)
    requirement_id = await repository.create(
        objective_id=objective_id, title="OCR", requirement_type="capability", created_by="SCOUT"
    )
    path = [
        RequirementStatus.EVALUATING,
        RequirementStatus.NEEDS_EVIDENCE,
        RequirementStatus.EVALUATING,
        RequirementStatus.APPROVED,
        RequirementStatus.IMPLEMENTED,
        verdict,
    ]
    for index, status in enumerate(path, start=2):
        correlation_id = uuid4()
        await repository.set_status(
            requirement_id,
            status,
            actor="SCOUT",
            reason="evidence",
            correlation_id=correlation_id,
        )
        events = await events_for(requirement_id)
        assert len(events) == index
        assert_typed(
            events[-1],
            "requirement.status_changed",
            actor="SCOUT",
            correlation_id=correlation_id,
        )
    assert [record.id for record in await repository.list_by_status(verdict)] == [requirement_id]
    with pytest.raises(InvalidRequirementTransition):
        await repository.set_status(requirement_id, RequirementStatus.PROPOSED, actor="SCOUT")
    assert len(await events_for(requirement_id)) == len(path) + 1


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RequirementStatus.PROPOSED, RequirementStatus.IMPLEMENTED),
        (RequirementStatus.PROPOSED, RequirementStatus.VERIFIED),
        (RequirementStatus.EVALUATING, RequirementStatus.VERIFIED),
        (RequirementStatus.NEEDS_EVIDENCE, RequirementStatus.APPROVED),
        (RequirementStatus.APPROVED, RequirementStatus.VERIFIED),
        (RequirementStatus.IMPLEMENTED, RequirementStatus.APPROVED),
        (RequirementStatus.REJECTED, RequirementStatus.PROPOSED),
        (RequirementStatus.VERIFIED, RequirementStatus.EVALUATING),
        (RequirementStatus.FAILED, RequirementStatus.VERIFIED),
        (RequirementStatus.SUPERSEDED, RequirementStatus.EVALUATING),
        (RequirementStatus.PROPOSED, RequirementStatus.PROPOSED),
    ],
)
async def test_invalid_requirement_transitions_leave_state_and_events_unchanged(
    database, objective_id, events_for, source, target
):
    repository = RequirementRepository(database.sessions)
    requirement_id = await repository.create(
        objective_id=objective_id, title="OCR", requirement_type="capability", created_by="SCOUT"
    )
    async with database.sessions() as session, session.begin():
        record = await session.get(RequirementRecord, requirement_id)
        record.status = source
    with pytest.raises(InvalidRequirementTransition):
        await repository.set_status(requirement_id, target, actor="SCOUT", reason="skip")
    async with database.sessions() as session:
        record = await session.get(RequirementRecord, requirement_id)
    assert record.status == source
    assert len(await events_for(requirement_id)) == 1


def test_requirement_transition_table_covers_all_states():
    assert set(ALLOWED_REQUIREMENT_TRANSITIONS) == set(RequirementStatus)
    assert ALLOWED_REQUIREMENT_TRANSITIONS[RequirementStatus.REJECTED] == frozenset()


async def test_only_scout_can_explicitly_reopen_with_a_reason(database, objective_id, events_for):
    repository = RequirementRepository(database.sessions)
    requirement_id = await repository.create(
        objective_id=objective_id, title="OCR", requirement_type="capability", created_by="SCOUT"
    )
    await repository.set_status(requirement_id, RequirementStatus.REJECTED, actor="SCOUT")
    for actor, reason in [("ORCHESTR", "new evidence"), ("SCOUT", ""), ("SCOUT", " ")]:
        with pytest.raises(InvalidRequirementTransition):
            await repository.reopen(requirement_id, actor=actor, reason=reason)
    assert len(await events_for(requirement_id)) == 2
    await repository.reopen(requirement_id, actor="SCOUT", reason="new evidence")
    events = await events_for(requirement_id)
    assert len(events) == 3
    assert_typed(events[-1], "requirement.status_changed", actor="SCOUT")
    assert events[-1].payload == {
        "from_status": "rejected",
        "to_status": "proposed",
        "reason": "new evidence",
    }


async def test_authorization_creation_uses_injected_clock(database, events_for):
    repository = AuthorizationRequestRepository(database.sessions)
    request_id = await repository.create(question="Allow?", actor="MUSE", now=NOW)
    async with database.sessions() as session:
        record = await session.get(AuthorizationRequestRecord, request_id)
    assert record.requested_at.replace(tzinfo=UTC) == NOW
    events = await events_for(request_id)
    assert len(events) == 1
    assert_typed(events[0], "authorization.requested", actor="MUSE")
    with pytest.raises(ValueError, match="timezone"):
        await repository.create(question="Allow?", actor="MUSE", now=NOW.replace(tzinfo=None))


async def test_repository_timestamps_preserve_offset_instants(database, objective_id):
    offset_now = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
    request_id = await AuthorizationRequestRepository(database.sessions).create(
        question="Allow?", actor="MUSE", now=offset_now
    )
    await ObjectiveRepository(database.sessions).update_details(
        objective_id,
        description="",
        completion_criteria=[],
        stopping_conditions=[],
        deadline=offset_now,
        review_at=offset_now,
        actor="ORCHESTR",
    )
    async with database.sessions() as session:
        request = await session.get(AuthorizationRequestRecord, request_id)
        objective = await session.get(ObjectiveRecord, objective_id)
    assert request.requested_at.replace(tzinfo=UTC) == NOW
    assert objective.deadline.replace(tzinfo=UTC) == NOW
    assert objective.review_at.replace(tzinfo=UTC) == NOW


async def test_event_failure_rolls_back_requirement_creation(database, objective_id, monkeypatch):
    repository = RequirementRepository(database.sessions)

    def fail(self, session, event):
        raise RuntimeError("event store unavailable")

    monkeypatch.setattr(EventRecorder, "add", fail)
    with pytest.raises(RuntimeError, match="event store"):
        await repository.create(
            requirement_id=uuid4(),
            objective_id=objective_id,
            title="OCR",
            requirement_type="capability",
            created_by="SCOUT",
        )
    async with database.sessions() as session:
        assert list(await session.scalars(select(RequirementRecord))) == []


@pytest.mark.parametrize("operation", ["details", "baseline", "priority", "estimates"])
async def test_event_failure_rolls_back_objective_and_task_updates(
    database, objective_id, events_for, monkeypatch, operation
):
    objectives = ObjectiveRepository(database.sessions)
    tasks = TaskRepository(database.sessions)
    task_id = await tasks.create(objective_id=objective_id, title="Task")

    def fail(self, session, event):
        raise RuntimeError("event store unavailable")

    monkeypatch.setattr(EventRecorder, "add", fail)
    with pytest.raises(RuntimeError, match="event store"):
        if operation == "details":
            await objectives.update_details(
                objective_id,
                description="changed",
                completion_criteria=["changed"],
                stopping_conditions=["changed"],
                deadline=NOW,
                review_at=NOW,
                actor="ORCHESTR",
            )
        elif operation == "baseline":
            await objectives.set_base_priority(objective_id, 80, actor="ORCHESTR", reason="changed")
        elif operation == "priority":
            await tasks.set_priority(task_id, 80, actor="ORCHESTR", reason="changed")
        else:
            await tasks.set_estimates(
                task_id, duration_s=60, ram_mb=256, disk_mb=512, cpu_percent=25, actor="ORCHESTR"
            )
    async with database.sessions() as session:
        objective = await session.get(ObjectiveRecord, objective_id)
        task = await session.get(TaskRecord, task_id)
    assert objective.description == ""
    assert objective.base_priority == task.priority == 50
    assert objective.completion_criteria == objective.stopping_conditions == []
    assert objective.deadline is objective.review_at is None
    assert task.estimated_duration_seconds is task.estimated_cpu_percent is None
    assert task.estimated_ram_mb == task.estimated_disk_mb == 0
    assert len(await events_for(objective_id)) == len(await events_for(task_id)) == 1
