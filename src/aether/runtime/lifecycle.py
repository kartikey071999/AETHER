"""Engine lifecycle state machine."""

from enum import StrEnum


class LifecycleState(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


_ALLOWED: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.CREATED: frozenset({LifecycleState.STARTING, LifecycleState.STOPPED}),
    LifecycleState.STARTING: frozenset({LifecycleState.RUNNING, LifecycleState.STOPPING}),
    LifecycleState.RUNNING: frozenset({LifecycleState.STOPPING}),
    LifecycleState.STOPPING: frozenset({LifecycleState.STOPPED}),
    LifecycleState.STOPPED: frozenset(),
}


class InvalidLifecycleTransition(RuntimeError):
    pass


class Lifecycle:
    def __init__(self) -> None:
        self._state = LifecycleState.CREATED

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def accepting_work(self) -> bool:
        return self._state is LifecycleState.RUNNING

    def transition(self, new_state: LifecycleState) -> None:
        if new_state not in _ALLOWED[self._state]:
            raise InvalidLifecycleTransition(
                f"Cannot move engine from {self._state.value} to {new_state.value}"
            )
        self._state = new_state
