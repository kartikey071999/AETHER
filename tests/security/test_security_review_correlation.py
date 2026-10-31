from uuid import uuid4

from aether.agents.base import AgentContext, TaskResultStatus
from aether.agents.security_review import request_security_review
from aether.communication.messages import AgentId, AgentMessage
from aether.security.errors import BlockReason
from aether.security.operations import Operation, OperationKind


class RecordingBus:
    def __init__(self) -> None:
        self.messages: list[AgentMessage] = []

    async def publish(self, message: AgentMessage) -> bool:
        self.messages.append(message)
        return True


def operation(agent: AgentId = AgentId.SCOUT) -> Operation:
    return Operation(
        kind=OperationKind.HTTP_REQUEST,
        target="https://api.example.com/x",
        hosts=("api.example.com",),
        requested_by=agent,
    )


async def test_review_reuses_the_callers_correlation_id():
    bus = RecordingBus()
    ctx = AgentContext(sessions=None, bus=bus)  # type: ignore[arg-type]
    correlation_id = uuid4()

    result = await request_security_review(
        operation(), ctx, reason=BlockReason.APPROVAL_MISSING, correlation_id=correlation_id
    )

    assert result.status is TaskResultStatus.WAITING_FOR_APPROVAL
    [message] = bus.messages
    assert message.correlation_id == correlation_id
    assert result.outputs["review_message_id"] == str(message.message_id)


async def test_review_still_generates_a_correlation_id_when_none_is_given():
    bus = RecordingBus()
    ctx = AgentContext(sessions=None, bus=bus)  # type: ignore[arg-type]

    await request_security_review(operation(), ctx)
    await request_security_review(operation(), ctx)

    first, second = bus.messages
    assert first.correlation_id != second.correlation_id
