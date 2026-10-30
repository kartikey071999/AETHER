import asyncio
import signal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from runtime_helpers import StubAgent

from aether.agents import get_agent
from aether.agents.base import (
    AgentContext,
    BaseAgent,
    HealthStatus,
    TaskResult,
    TaskResultStatus,
    UnsupportedMessageType,
)
from aether.communication.bus import MessageBus
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.runtime.lifecycle import InvalidLifecycleTransition, Lifecycle, LifecycleState
from aether.runtime.shutdown import ShutdownSignalHandler


def message(message_type: MessageType) -> AgentMessage:
    return AgentMessage(
        source_agent=AgentId.ORCHESTR,
        target_agent=AgentId.FORGE,
        message_type=message_type,
        payload={},
    )


async def test_base_agent_rejects_unaccepted_message_types(sessions) -> None:
    agent = StubAgent()
    ctx = AgentContext(sessions=sessions, bus=MessageBus(sessions))

    await agent.dispatch_message(message(MessageType.TASK_ASSIGN), ctx)
    with pytest.raises(UnsupportedMessageType) as exc_info:
        await agent.dispatch_message(message(MessageType.SECURITY_DECISION), ctx)

    assert len(agent.handled) == 1
    assert exc_info.value.to_dict() == {
        "error": "unsupported_message_type",
        "agent_id": "FORGE",
        "message_type": "security.decision",
        "accepted": ["task.assign"],
    }


def test_base_agent_exposes_registry_identity_and_permissions() -> None:
    agent = StubAgent()

    assert agent.definition is get_agent(AgentId.FORGE)
    assert agent.permissions == get_agent(AgentId.FORGE).allowed_actions
    assert "execution.request" in agent.permissions


def test_base_agent_is_abstract() -> None:
    with pytest.raises(TypeError):
        BaseAgent()  # type: ignore[abstract]


async def test_agent_health_cancel_and_result_model() -> None:
    agent = StubAgent()
    assert (await agent.heartbeat()).status is HealthStatus.UNHEALTHY

    await agent.start()
    health = await agent.health_check()
    assert health.status is HealthStatus.HEALTHY and health.agent_id is AgentId.FORGE

    task_id = uuid4()
    assert agent.is_cancelled(task_id) is False
    await agent.cancel(task_id)
    assert agent.is_cancelled(task_id) is True

    await agent.stop()
    assert (await agent.heartbeat()).status is HealthStatus.UNHEALTHY

    result = TaskResult(status=TaskResultStatus.COMPLETED, summary="done", outputs={"a": 1})
    assert result.evidence == [] and result.error is None


def test_lifecycle_enforces_ordered_transitions() -> None:
    lifecycle = Lifecycle()
    assert lifecycle.accepting_work is False

    lifecycle.transition(LifecycleState.STARTING)
    lifecycle.transition(LifecycleState.RUNNING)
    assert lifecycle.accepting_work is True
    with pytest.raises(InvalidLifecycleTransition):
        lifecycle.transition(LifecycleState.STARTING)

    lifecycle.transition(LifecycleState.STOPPING)
    lifecycle.transition(LifecycleState.STOPPED)
    with pytest.raises(InvalidLifecycleTransition):
        lifecycle.transition(LifecycleState.RUNNING)


class FakeLoop:
    def __init__(self, *, supports_signal_handlers: bool) -> None:
        self.supports = supports_signal_handlers
        self.registered: list[int] = []
        self.removed: list[int] = []
        self.threadsafe_calls: list = []

    def add_signal_handler(self, signum, callback, *args) -> None:
        if not self.supports:
            raise NotImplementedError
        self.registered.append(signum)

    def remove_signal_handler(self, signum) -> bool:
        self.removed.append(signum)
        return True

    def call_soon_threadsafe(self, callback, *args) -> None:
        self.threadsafe_calls.append(callback)


def fake_signal_module() -> SimpleNamespace:
    handlers: dict[int, object] = {}

    def register(signum, handler):
        previous = handlers.get(signum, "previous")
        handlers[signum] = handler
        return previous

    return SimpleNamespace(
        SIGINT=signal.SIGINT, SIGTERM=15, SIGBREAK=21, signal=register, handlers=handlers
    )


def test_signal_fallback_schedules_stop_thread_safely_when_loop_lacks_support() -> None:
    stops: list[str] = []
    loop = FakeLoop(supports_signal_handlers=False)
    fake_signal = fake_signal_module()
    handler = ShutdownSignalHandler(lambda: stops.append("stop"), signal_module=fake_signal)

    handler.install(loop)  # type: ignore[arg-type]

    assert loop.registered == []
    assert set(fake_signal.handlers) == {signal.SIGINT, 15, 21}
    fake_signal.handlers[21](21, None)  # simulate Ctrl+Break arriving on the main thread
    assert loop.threadsafe_calls == [handler._callback]
    loop.threadsafe_calls[0]()
    assert stops == ["stop"]

    handler.uninstall()
    assert set(fake_signal.handlers.values()) == {"previous"}


def test_signal_handlers_use_the_loop_when_supported() -> None:
    loop = FakeLoop(supports_signal_handlers=True)
    fake_signal = fake_signal_module()
    handler = ShutdownSignalHandler(lambda: None, signal_module=fake_signal)

    handler.install(loop)  # type: ignore[arg-type]
    assert loop.registered == [signal.SIGINT, 15, 21]
    assert fake_signal.handlers == {}

    handler.uninstall()
    assert loop.removed == [signal.SIGINT, 15, 21]


async def test_real_signal_handler_install_and_uninstall_is_safe() -> None:
    calls: list[int] = []
    handler = ShutdownSignalHandler(lambda: calls.append(1))

    handler.install(asyncio.get_running_loop())
    handler.uninstall()

    assert calls == []
