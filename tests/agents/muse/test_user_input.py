from uuid import uuid4

import pytest
from sqlalchemy import select

from aether.agents.base import TaskResultStatus
from aether.agents.muse import (
    MuseAgent,
    delete_person_data,
    record_feedback,
    record_user_response,
)
from aether.agents.muse.repository import MuseRepository
from aether.communication.contracts import UserDecision, validate_message
from aether.communication.messages import MessageType
from aether.storage.models import (
    AuthorizationRequestRecord,
    EventRecord,
    InteractionRecord,
    KnowledgeEntityRecord,
    KnowledgeRelationRecord,
    MessageRecord,
    PersonRecord,
    UserPreferenceRecord,
)
from aether.storage.repositories import (
    AuthorizationRequestRepository,
    KnowledgeGraphRepository,
    PersonRepository,
)


async def records(ctx, model):
    async with ctx.sessions() as session:
        return list(await session.scalars(select(model)))


@pytest.fixture
async def authorization(ctx, objective_id, task_id, clock):
    from datetime import timedelta

    return await AuthorizationRequestRepository(ctx.sessions).create(
        actor="WARDEN",
        question="Authorize installation?",
        objective_id=objective_id,
        task_id=task_id,
        expires_at=clock.now + timedelta(hours=1),
    )


async def present_authorization(agent, ctx, request_message, authorization):
    message = request_message(
        authorization_required=True,
        authorization_request_id=authorization,
        personalization_context={
            "authorization_expires_at": "2026-10-31T01:00:00+00:00",
        },
    )
    await agent.dispatch_message(message, ctx)
    return message


async def test_authorization_render_and_explicit_response_are_linked_but_muse_never_approves(
    agent, ctx, request_message, authorization, person_id, objective_id, task_id
):
    message = await present_authorization(agent, ctx, request_message, authorization)
    outbound = (await records(ctx, InteractionRecord))[0]
    assert outbound.authorization_request_id == authorization
    assert "Options: Approve; Deny." in outbound.summary
    assert "Expiry: 2026-10-31T01:00:00+00:00." in outbound.summary
    assert "No response = no authorization." in outbound.summary

    interaction_id = await record_user_response(
        ctx,
        text="Approve this exact request",
        person_id=person_id,
        authorization_request_id=authorization,
        decision=UserDecision.APPROVED,
    )
    inbound = next(i for i in await records(ctx, InteractionRecord) if i.id == interaction_id)
    assert inbound.direction == "inbound"
    assert inbound.authorization_request_id == authorization
    assert inbound.objective_id == objective_id
    assert inbound.task_id == task_id
    responses = [
        row
        for row in await records(ctx, MessageRecord)
        if row.message_type == MessageType.USER_RESPONSE.value
    ]
    assert {row.target_agent for row in responses} == {"ORCHESTR", "WARDEN"}
    for row in responses:
        payload = validate_message(ctx.bus._to_message(row, row.status))
        assert payload.decision is UserDecision.APPROVED
        assert row.correlation_id == message.correlation_id
        assert row.objective_id == objective_id
        assert row.task_id == task_id
    authorization_row = (await records(ctx, AuthorizationRequestRecord))[0]
    assert authorization_row.status == "pending"
    assert authorization_row.responded_at is None
    assert not await AuthorizationRequestRepository(ctx.sessions).is_authorized(authorization)
    events = await records(ctx, EventRecord)
    assert any(
        e.event_type == "interaction.recorded" and e.aggregate_id == inbound.id for e in events
    )
    assert any(e.payload.get("decision") == "muse.response" for e in events)


@pytest.mark.parametrize("text", ["", " ", "\n\t"])
async def test_silence_never_records_or_authorizes(ctx, text, person_id, authorization):
    with pytest.raises(ValueError, match="Silence"):
        await record_user_response(
            ctx,
            text=text,
            person_id=person_id,
            authorization_request_id=authorization,
            decision=UserDecision.APPROVED,
        )
    assert await records(ctx, InteractionRecord) == []
    assert await records(ctx, MessageRecord) == []
    assert not await AuthorizationRequestRepository(ctx.sessions).is_authorized(authorization)


async def test_natural_language_approval_is_not_parsed_as_authorization(
    agent, ctx, request_message, authorization, person_id
):
    await present_authorization(agent, ctx, request_message, authorization)
    await record_user_response(
        ctx,
        text="yes, approved, go ahead",
        person_id=person_id,
        authorization_request_id=authorization,
    )
    responses = [
        row
        for row in await records(ctx, MessageRecord)
        if row.message_type == MessageType.USER_RESPONSE.value
    ]
    assert all(row.payload["decision"] == "none" for row in responses)
    assert (await records(ctx, AuthorizationRequestRecord))[0].status == "pending"


async def test_response_idempotency_and_conflicting_reuse(ctx, person_id, objective_id, clock):
    response_id = uuid4()
    interaction = await record_user_response(
        ctx,
        text="Use the local approach",
        person_id=person_id,
        objective_id=objective_id,
        response_id=response_id,
    )
    clock.advance(60)
    assert interaction == await record_user_response(
        ctx,
        text="Use the local approach",
        person_id=person_id,
        objective_id=objective_id,
        response_id=response_id,
    )
    assert len(await records(ctx, InteractionRecord)) == 1
    assert len(await records(ctx, MessageRecord)) == 1
    with pytest.raises(ValueError, match="reused"):
        await record_user_response(
            ctx,
            text="Use the remote approach",
            person_id=person_id,
            objective_id=objective_id,
            response_id=response_id,
        )


async def test_response_restart_after_bus_failure_is_recoverable(
    ctx, person_id, objective_id, monkeypatch
):
    async def crash(self, message):
        raise RuntimeError("simulated crash")

    with monkeypatch.context() as patch:
        patch.setattr(type(ctx.bus), "publish", crash)
        with pytest.raises(RuntimeError, match="simulated crash"):
            await record_user_response(
                ctx, text="Use the local approach", person_id=person_id, objective_id=objective_id
            )
    assert len(await records(ctx, InteractionRecord)) == 1
    assert await records(ctx, MessageRecord) == []
    assert len(await MuseRepository(ctx.sessions).pending_responses()) == 1
    await MuseAgent().resume_pending(ctx)
    assert len(await records(ctx, MessageRecord)) == 1
    assert await MuseRepository(ctx.sessions).pending_responses() == []


async def test_wrong_audience_and_unpresented_request_cannot_authorize(
    agent, ctx, request_message, authorization, person_id
):
    with pytest.raises(PermissionError, match="not addressed"):
        await record_user_response(
            ctx,
            text="approve",
            person_id=person_id,
            authorization_request_id=authorization,
            decision=UserDecision.APPROVED,
        )
    await present_authorization(agent, ctx, request_message, authorization)
    other = await PersonRepository(ctx.sessions).create(display_name="Someone else")
    with pytest.raises(PermissionError, match="not addressed"):
        await record_user_response(
            ctx,
            text="approve",
            person_id=other,
            authorization_request_id=authorization,
            decision=UserDecision.APPROVED,
        )


async def test_expired_request_and_conflicting_links_cannot_authorize(
    agent, ctx, request_message, authorization, person_id, clock
):
    await present_authorization(agent, ctx, request_message, authorization)
    with pytest.raises(ValueError, match="contradict"):
        await record_user_response(
            ctx,
            text="approve",
            person_id=person_id,
            authorization_request_id=authorization,
            objective_id=uuid4(),
            decision=UserDecision.APPROVED,
        )
    clock.advance(3600)
    with pytest.raises(ValueError, match="expired"):
        await record_user_response(
            ctx,
            text="approve",
            person_id=person_id,
            authorization_request_id=authorization,
            decision=UserDecision.APPROVED,
        )
    assert (await records(ctx, AuthorizationRequestRecord))[0].status == "pending"


async def test_response_without_association_is_invalid(ctx, person_id):
    with pytest.raises(ValueError, match="must reference"):
        await record_user_response(ctx, text="Hello", person_id=person_id)
    assert await records(ctx, InteractionRecord) == []


async def test_feedback_is_explicit_versioned_and_idempotent(ctx, person_id):
    people = MuseRepository(ctx.sessions)
    await people.set_preference(person_id, "length", "long")
    feedback_id = uuid4()
    await record_feedback(ctx, person_id=person_id, text="shorter please", feedback_id=feedback_id)
    await record_feedback(ctx, person_id=person_id, text="shorter please", feedback_id=feedback_id)
    assert await people.get_preferences(person_id) == {"length": "short"}
    preferences = await records(ctx, UserPreferenceRecord)
    assert preferences[0].source == "feedback"
    versions = [e for e in await records(ctx, EventRecord) if e.event_type == "preference.set"]
    assert len(versions) == 2
    assert versions[-1].payload["previous"] == "long"
    assert versions[-1].payload["value"] == "short"
    with pytest.raises(ValueError, match="reused"):
        await record_feedback(
            ctx, person_id=person_id, text="longer please", feedback_id=feedback_id
        )


async def test_deletion_purges_owned_preferences_and_graph_and_never_rereads_person(
    agent, ctx, request_message, person_id, objective_id
):
    people = MuseRepository(ctx.sessions)
    await people.set_preference(person_id, "length", "short")
    graph = KnowledgeGraphRepository(ctx.sessions)
    owned = await graph.upsert_entity(
        entity_type="project", name="Owned", actor="MUSE", person_id=person_id
    )
    shared = await graph.upsert_entity(entity_type="project", name="Shared", actor="SCOUT")
    await graph.add_relation(
        source_entity_id=shared, target_entity_id=owned, relation_type="references", actor="MUSE"
    )
    message = request_message()
    await agent.dispatch_message(message, ctx)
    await delete_person_data(ctx, person_id=person_id)
    await delete_person_data(ctx, person_id=person_id)
    assert await records(ctx, UserPreferenceRecord) == []
    assert [e.id for e in await records(ctx, KnowledgeEntityRecord)] == [shared]
    assert await records(ctx, KnowledgeRelationRecord) == []
    person = (await records(ctx, PersonRecord))[0]
    assert person.deleted_at is not None
    assert person.display_name == "Deleted person"
    with pytest.raises(LookupError):
        await people.get_preferences(person_id)
    with pytest.raises(LookupError):
        await agent.dispatch_message(message, ctx)
    with pytest.raises(LookupError):
        await record_user_response(
            ctx, text="A reply", person_id=person_id, objective_id=objective_id
        )
    with pytest.raises(LookupError):
        await record_feedback(ctx, person_id=person_id, text="shorter please")
    assert await MuseAgent().resume_pending(ctx) == []
    deleted_events = [
        e for e in await records(ctx, EventRecord) if e.event_type == "person.deleted"
    ]
    assert len(deleted_events) == 1


async def test_deleted_person_staged_delivery_is_never_resumed(
    agent, ctx, request_message, person_id
):
    tools = ctx.tools
    ctx.tools = None
    result = await agent.communicate(request_message(), ctx)
    assert result.status is TaskResultStatus.FAILED
    await delete_person_data(ctx, person_id=person_id)
    ctx.tools = tools
    assert await MuseAgent().resume_pending(ctx) == []
    assert not ctx.services["muse_settings"].data_dir.joinpath("outbox").exists()
