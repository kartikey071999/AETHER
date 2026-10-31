import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from aether.agents.base import TaskResultStatus, UnsupportedMessageType
from aether.agents.factory import resolve_agent_class
from aether.agents.muse import MuseAgent
from aether.communication.contracts import (
    AuthorizationRequestPayload,
    InvalidPayload,
    RouteViolation,
    SecurityDecisionPayload,
    build_message,
    receivable_types,
    validate_message,
)
from aether.communication.messages import AgentId, MessageType
from aether.models.facade import AgentModels
from aether.models.providers import MockProvider
from aether.observability.recorder import EventRecorder
from aether.security.approvals import ApprovalService, DecisionOutcome
from aether.security.operations import OperationKind
from aether.security.permissions import PermissionService
from aether.security.policy import SecurityPolicy
from aether.storage.models import EventRecord, InteractionRecord, MessageRecord
from aether.storage.repositories import PersonRepository
from aether.tools.adapters import build_default_registry
from aether.tools.facade import AgentTools


async def rows(ctx, model):
    async with ctx.sessions() as session:
        return list(await session.scalars(select(model)))


def test_factory_and_accepted_types():
    assert resolve_agent_class(AgentId.MUSE) is MuseAgent
    assert MuseAgent.accepted_message_types == receivable_types(AgentId.MUSE)
    assert MessageType.COMMUNICATION_REQUEST in MuseAgent.accepted_message_types
    assert MessageType.EXECUTION_REQUEST not in MuseAgent.accepted_message_types


async def test_outbox_interaction_events_and_typed_delivery(agent, ctx, request_message, person_id):
    await PersonRepository(ctx.sessions).set_preference(person_id, "channel", "console")
    message = request_message()
    await agent.dispatch_message(message, ctx)
    interactions = await rows(ctx, InteractionRecord)
    assert len(interactions) == 1
    interaction = interactions[0]
    assert interaction.direction == "outbound"
    assert interaction.channel == "console"
    assert interaction.task_id == message.task_id
    assert interaction.objective_id == message.objective_id
    files = list(ctx.services["muse_settings"].data_dir.joinpath("outbox").glob("*.md"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == interaction.summary
    deliveries = [
        row
        for row in await rows(ctx, MessageRecord)
        if row.message_type == MessageType.COMMUNICATION_DELIVERED.value
    ]
    assert {row.target_agent for row in deliveries} == {"ORCHESTR", "LEDGER"}
    for delivery in deliveries:
        payload = validate_message(ctx.bus._to_message(delivery, delivery.status))
        assert payload.interaction_id == interaction.id
        assert payload.channel == "console"
        assert delivery.correlation_id == message.correlation_id
        assert payload.request_message_id == message.message_id
    events = await rows(ctx, EventRecord)
    assert any(
        e.event_type == "interaction.recorded" and e.aggregate_id == interaction.id for e in events
    )
    assert any(e.payload.get("decision") == "muse.delivered" for e in events)


async def test_route_payload_and_unsupported_types_rejected(agent, ctx, request_message):
    message = request_message()
    with pytest.raises(RouteViolation):
        await agent.handle_message(message.model_copy(update={"source_agent": AgentId.FORGE}), ctx)
    with pytest.raises(InvalidPayload):
        await agent.handle_message(
            message.model_copy(update={"payload": {"what_happened": ""}}), ctx
        )
    with pytest.raises(UnsupportedMessageType):
        await agent.handle_message(
            message.model_copy(update={"message_type": MessageType.EXECUTION_REQUEST}), ctx
        )
    assert await rows(ctx, InteractionRecord) == []


async def test_idempotent_redelivery_and_dedupe_window(agent, ctx, request_message, clock):
    message = request_message()
    await agent.dispatch_message(message, ctx)
    await MuseAgent().dispatch_message(message, ctx)
    duplicate = request_message(envelope={"correlation_id": message.correlation_id})
    await agent.dispatch_message(duplicate, ctx)
    assert len(await rows(ctx, InteractionRecord)) == 1
    assert len(await rows(ctx, MessageRecord)) == 2
    clock.advance(3599)
    await agent.dispatch_message(duplicate, ctx)
    await agent.dispatch_message(request_message(), ctx)
    assert len(await rows(ctx, InteractionRecord)) == 1
    clock.advance(2)
    outside_window = request_message(envelope={"correlation_id": message.correlation_id})
    await agent.dispatch_message(outside_window, ctx)
    assert len(await rows(ctx, InteractionRecord)) == 2


async def test_parallel_duplicate_requests_create_one_artifact(agent, ctx, request_message):
    message = request_message()
    await asyncio.gather(agent.communicate(message, ctx), MuseAgent().communicate(message, ctx))
    assert len(await rows(ctx, InteractionRecord)) == 1
    assert len(list(ctx.services["muse_settings"].data_dir.joinpath("outbox").glob("*.md"))) == 1


async def test_parallel_distinct_duplicate_notices_stage_one(agent, ctx, request_message):
    first, second = request_message(), request_message()
    await asyncio.gather(agent.communicate(first, ctx), MuseAgent().communicate(second, ctx))
    assert len(await rows(ctx, InteractionRecord)) == 1


async def test_same_notice_suppressed_regardless_of_correlation_new_content_delivered(
    agent, ctx, request_message
):
    first = request_message()
    await agent.dispatch_message(first, ctx)
    await agent.dispatch_message(request_message(), ctx)
    await agent.dispatch_message(
        request_message(
            what_happened="Different results.", envelope={"correlation_id": first.correlation_id}
        ),
        ctx,
    )
    assert len(await rows(ctx, InteractionRecord)) == 2


async def test_routine_suppressed_but_decisions_not_suppressed(agent, ctx, request_message):
    message = request_message(uncertain=[], blockers=[], personalization_context={"routine": True})
    await agent.dispatch_message(message, ctx)
    await agent.dispatch_message(message, ctx)
    assert await rows(ctx, InteractionRecord) == []
    assert await rows(ctx, MessageRecord) == []
    await agent.dispatch_message(
        request_message(
            uncertain=[],
            blockers=[],
            decisions_required=["Choose a plan"],
            personalization_context={"routine": True},
        ),
        ctx,
    )
    assert len(await rows(ctx, InteractionRecord)) == 1


async def test_restarts_after_file_failure_resume_from_db(
    agent, ctx, request_message, monkeypatch, person_id
):
    message = request_message()
    original = AgentTools.invoke

    async def fail(self, tool, args, call=None):
        raise OSError("unavailable filesystem")

    with monkeypatch.context() as patch:
        patch.setattr(AgentTools, "invoke", fail)
        result = await agent.communicate(message, ctx)
    assert result.status is TaskResultStatus.FAILED
    assert AgentTools.invoke is original
    assert len(await rows(ctx, InteractionRecord)) == 1
    assert await rows(ctx, MessageRecord) == []
    await PersonRepository(ctx.sessions).set_preference(person_id, "length", "short")
    result = await MuseAgent().communicate(message, ctx)
    assert result.status is TaskResultStatus.COMPLETED
    assert len(await rows(ctx, InteractionRecord)) == 1


async def test_restart_after_delivery_event_before_bus_publication(
    agent, ctx, request_message, monkeypatch
):
    message = request_message()
    original = type(ctx.bus).publish

    async def fail(self, message):
        raise RuntimeError("simulated crash")

    with monkeypatch.context() as patch:
        patch.setattr(type(ctx.bus), "publish", fail)
        with pytest.raises(RuntimeError, match="simulated crash"):
            await agent.communicate(message, ctx)
    assert type(ctx.bus).publish is original
    results = await MuseAgent().resume_pending(ctx)
    assert all(r.status is TaskResultStatus.COMPLETED for r in results)
    assert len(await rows(ctx, InteractionRecord)) == 1
    assert len(await rows(ctx, MessageRecord)) == 2


async def test_default_policy_cannot_write_muse_outbox(
    agent, ctx, request_message, database, policy, clock
):
    default = SecurityPolicy(
        workspace_dir=policy.workspace_dir,
        artifact_dir=policy.artifact_dir,
        data_dir=policy.data_dir,
        agent_permissions={AgentId.MUSE: {OperationKind.LLM_CALL}},
    )
    permissions = PermissionService(database.sessions, EventRecorder(), default, clock=clock)
    registry = build_default_registry(permissions, database.sessions, EventRecorder())
    ctx.tools = AgentTools(AgentId.MUSE, registry)
    await agent.dispatch_message(request_message(), ctx)
    records = await rows(ctx, MessageRecord)
    assert all(r.message_type != MessageType.COMMUNICATION_DELIVERED.value for r in records)
    assert records[0].payload["details"]["status"] == "blocked"
    assert records[0].payload["details"]["block_reason"] == "agent_not_permitted"
    assert not policy.data_dir.joinpath("outbox").exists()


async def test_review_waits_and_only_retries_matching_approved_operation(
    agent, ctx, request_message, database, policy, clock
):
    reviewed = SecurityPolicy(
        workspace_dir=policy.workspace_dir,
        artifact_dir=policy.artifact_dir,
        data_dir=policy.data_dir,
        agent_permissions={AgentId.MUSE: {OperationKind.FILE_WRITE}},
        approval_required_kinds=[OperationKind.FILE_WRITE],
    )
    permissions = PermissionService(database.sessions, EventRecorder(), reviewed, clock=clock)
    ctx.tools = AgentTools(
        AgentId.MUSE, build_default_registry(permissions, database.sessions, EventRecorder())
    )
    message = request_message()
    await agent.dispatch_message(message, ctx)
    records = await rows(ctx, MessageRecord)
    review = next(r for r in records if r.message_type == MessageType.SECURITY_EVALUATE.value)
    assert review.correlation_id == message.correlation_id
    payload = validate_message(ctx.bus._to_message(review, review.status))
    assert payload.operation.task_id == message.task_id
    assert not policy.data_dir.joinpath("outbox").exists()
    decision = await ApprovalService(
        database.sessions, EventRecorder(), reviewed, clock=clock
    ).record_decision(
        AgentId.WARDEN,
        payload.operation,
        DecisionOutcome.APPROVED,
        rationale="Test scoped write",
        evidence=[{"check": "offline"}],
    )
    response = build_message(
        AgentId.WARDEN,
        AgentId.MUSE,
        SecurityDecisionPayload(
            decision_id=decision.decision_id,
            decision=DecisionOutcome.APPROVED,
            input_hash=payload.operation.input_hash,
            rationale="Test scoped write",
        ),
        correlation_id=message.correlation_id,
    )
    await MuseAgent().dispatch_message(response, ctx)
    assert any(
        r.message_type == MessageType.COMMUNICATION_DELIVERED.value
        for r in await rows(ctx, MessageRecord)
    )


async def test_opening_is_deterministic_and_never_calls_a_model(agent, ctx, request_message):
    provider = MockProvider(['{"opening":"99 invented facts"}'])
    ctx.models = AgentModels(AgentId.MUSE, provider.complete)
    await agent.dispatch_message(request_message(), ctx)
    text = (await rows(ctx, InteractionRecord))[0].summary
    assert "12 tests passed." in text
    assert "99" not in text
    assert provider.requests == []


async def test_non_handoff_authorization_notice_never_communicates(agent, ctx):
    message = build_message(
        AgentId.WARDEN,
        AgentId.MUSE,
        AuthorizationRequestPayload(
            authorization_request_id=uuid4(), question="Authorize?", operation_summary="Operation"
        ),
    )
    await agent.dispatch_message(message, ctx)
    assert await rows(ctx, InteractionRecord) == []
    async with ctx.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(EventRecord)) == 1


async def test_missing_tool_facade_does_not_claim_delivery(agent, ctx, request_message):
    ctx.tools = None
    await agent.dispatch_message(request_message(), ctx)
    records = await rows(ctx, MessageRecord)
    assert records[0].payload["details"]["status"] == "failed"
    assert all(r.message_type != MessageType.COMMUNICATION_DELIVERED.value for r in records)


async def test_no_permission_service_cannot_communicate(agent, ctx, request_message):
    ctx.permission_service = None
    with pytest.raises(PermissionError, match="communication.prepare"):
        await agent.dispatch_message(request_message(), ctx)
    assert await rows(ctx, InteractionRecord) == []


async def test_heartbeat_reports_lifecycle():
    agent = MuseAgent()
    assert (await agent.heartbeat()).status.value == "unhealthy"
    await agent.start()
    assert (await agent.heartbeat()).status.value == "healthy"
    await agent.stop()
    assert (await agent.heartbeat()).status.value == "unhealthy"


async def test_task_cannot_be_used_to_fabricate_a_handoff(agent, ctx, database, task_id):
    from aether.storage.models import TaskRecord

    async with database.sessions() as session:
        task = await session.get(TaskRecord, task_id)
    result = await agent.execute_task(task, ctx)
    assert result.status is TaskResultStatus.BLOCKED
    assert "task text is not a handoff" in result.summary
