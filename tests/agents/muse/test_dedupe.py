"""MUSE duplicate-notice suppression (T-047 notification storm).

The incident's root cause: MUSE only compared a request against earlier outbox entries with the
same correlation id (plus identical task, requirement and rendered text). ORCHESTR builds every
user update with a fresh random correlation id and per-task references, so 55 escalations for one
objective never matched and all reached the user. The dedupe key is now (audience, objective,
notice kind, content hash) within a configurable window, independent of correlation and task.
"""

from datetime import timedelta

from sqlalchemy import select

from aether.agents.muse import MuseSettings
from aether.agents.orchestr.communication import UpdateEvent, UpdateKind, build_handoff
from aether.communication.contracts import build_message
from aether.communication.messages import AgentId
from aether.storage.models import EventRecord, InteractionRecord
from aether.storage.repositories import ObjectiveRepository, PersonRepository, TaskRepository


async def rows(ctx, model):
    async with ctx.sessions() as session:
        return list(await session.scalars(select(model)))


async def suppressed(ctx) -> list[EventRecord]:
    async with ctx.sessions() as session:
        return list(
            await session.scalars(
                select(EventRecord).where(EventRecord.event_type == "communication.suppressed")
            )
        )


def escalation(objective_id, task_id, audience, *, kind=UpdateKind.ESCALATION):
    """What ORCHESTR's ``Env.send_user_update`` hands to MUSE (random correlation id)."""
    payload = build_handoff(
        UpdateEvent(kind),
        what_happened="Work on 'Communication objective' is blocked and needs your input.",
        objective_id=objective_id,
        task_id=task_id,
        blockers=["research blocker persisted after 3 attempts: no maintained library"],
        decisions_required=["Decide whether to retry, change the approach, or cancel"],
    ).model_copy(update={"audience": audience})
    return build_message(
        AgentId.ORCHESTR, AgentId.MUSE, payload, objective_id=objective_id, task_id=task_id
    )


async def test_orchestr_escalations_from_different_tasks_reach_the_user_once(
    agent, ctx, database, objective_id, person_id
):
    tasks = TaskRepository(database.sessions)
    messages = []
    for index in range(55):
        task_id = await tasks.create(
            objective_id=objective_id, title=f"Blocked task {index}", assigned_agent="SCOUT"
        )
        messages.append(escalation(objective_id, task_id, person_id))
    assert len({message.correlation_id for message in messages}) == 55  # the original miss

    for message in messages:
        await agent.dispatch_message(message, ctx)

    assert len(await rows(ctx, InteractionRecord)) == 1
    events = await suppressed(ctx)
    assert len(events) == 54
    first = messages[0].message_id
    assert {event.payload["duplicate_of_message_id"] for event in events} == {str(first)}
    assert {event.payload["notice_kind"] for event in events} == {"escalation"}
    assert all(event.objective_id == objective_id for event in events)


async def test_redelivered_duplicate_records_one_suppression(
    agent, ctx, objective_id, task_id, person_id
):
    await agent.dispatch_message(escalation(objective_id, task_id, person_id), ctx)
    duplicate = escalation(objective_id, task_id, person_id)
    await agent.dispatch_message(duplicate, ctx)
    await agent.dispatch_message(duplicate, ctx)
    assert len(await suppressed(ctx)) == 1
    result = await agent.communicate(duplicate, ctx)
    assert "suppressed" in result.summary.lower()


async def test_other_kind_objective_audience_or_content_is_delivered(
    agent, ctx, database, objective_id, task_id, person_id
):
    other_objective = await ObjectiveRepository(database.sessions).create(title="Other")
    other_person = await PersonRepository(database.sessions).create(
        display_name="Second", access_level="full"
    )
    await agent.dispatch_message(escalation(objective_id, task_id, person_id), ctx)
    await agent.dispatch_message(
        escalation(objective_id, task_id, person_id, kind=UpdateKind.DECISION_REQUIRED), ctx
    )
    await agent.dispatch_message(escalation(other_objective, None, person_id), ctx)
    await agent.dispatch_message(escalation(objective_id, task_id, other_person), ctx)
    changed = escalation(objective_id, task_id, person_id)
    payload = dict(changed.payload, blockers=["a different blocker"])
    await agent.dispatch_message(changed.model_copy(update={"payload": payload}), ctx)

    assert len(await rows(ctx, InteractionRecord)) == 5
    assert await suppressed(ctx) == []


async def test_window_expiry_and_disabled_window(
    agent, ctx, clock, objective_id, task_id, person_id
):
    await agent.dispatch_message(escalation(objective_id, task_id, person_id), ctx)
    clock.advance(3601)
    await agent.dispatch_message(escalation(objective_id, task_id, person_id), ctx)
    assert len(await rows(ctx, InteractionRecord)) == 2

    settings = ctx.services["muse_settings"]
    ctx.services["muse_settings"] = MuseSettings(
        data_dir=settings.data_dir, dedupe_window=timedelta(0)
    )
    await agent.dispatch_message(escalation(objective_id, task_id, person_id), ctx)
    assert len(await rows(ctx, InteractionRecord)) == 3


async def test_default_window_is_one_hour_and_configurable_from_settings(tmp_path):
    from aether.config import AetherSettings
    from aether.runtime.bootstrap import agent_services

    assert MuseSettings().dedupe_window == timedelta(hours=1)
    settings = AetherSettings(_env_file=None, data_dir=tmp_path, muse_dedupe_window_minutes=15)
    services = agent_services(settings, None, None, [AgentId.MUSE])
    assert services[AgentId.MUSE]["muse_settings"].dedupe_window == timedelta(minutes=15)
