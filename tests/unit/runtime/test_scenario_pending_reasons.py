"""The demo trace explains why tasks are still pending."""

from types import SimpleNamespace
from uuid import uuid4

from aether.runtime.scenario import pending_reasons


def _task(agent: str, *, state: str = "pending", requirement_id=None):
    return SimpleNamespace(
        id=uuid4(), assigned_agent=agent, state=state, requirement_id=requirement_id
    )


def _deferral(reasons: dict):
    return SimpleNamespace(
        payload={"deferred": {str(key): value for key, value in reasons.items()}}
    )


def test_pending_tasks_report_the_latest_scheduler_deferral() -> None:
    build = _task("FORGE")
    approved = SimpleNamespace(id=uuid4(), status="approved")
    build.requirement_id = approved.id
    trace = SimpleNamespace(
        tasks=[build],
        dependencies=[],
        requirements=[approved],
        scheduler_deferrals=[
            _deferral({build.id: "deferred: host CPU capacity below the task's estimate."}),
            _deferral({build.id: "deferred: host CPU above the configured ceiling."}),
        ],
    )

    assert pending_reasons(trace) == {build.id: "deferred: host CPU above the configured ceiling."}


def test_forge_without_an_approved_requirement_is_held() -> None:
    build = _task("FORGE")
    trace = SimpleNamespace(
        tasks=[build],
        dependencies=[],
        requirements=[],
        scheduler_deferrals=[_deferral({build.id: "deferred: host CPU"})],
    )

    assert pending_reasons(trace) == {build.id: "held: no approved requirement"}


def test_dependencies_and_unexplained_tasks_and_finished_tasks() -> None:
    first = _task("SCOUT", state="running")
    waiting = _task("ROGUE")
    waiting_dependency = SimpleNamespace(task_id=waiting.id, depends_on_task_id=first.id)
    silent = _task("LEDGER")
    done = _task("MUSE", state="completed")
    trace = SimpleNamespace(
        tasks=[first, waiting, silent, done],
        dependencies=[waiting_dependency],
        requirements=[],
        scheduler_deferrals=[],
    )

    reasons = pending_reasons(trace)

    assert reasons[waiting.id].startswith("waiting:")
    assert reasons[silent.id] == "no scheduler decision recorded yet"
    assert first.id not in reasons and done.id not in reasons


def test_a_pending_task_with_an_escalation_says_so() -> None:
    build = _task("FORGE")
    approved = SimpleNamespace(id=uuid4(), status="approved")
    build.requirement_id = approved.id
    escalation = SimpleNamespace(
        task_id=build.id, payload={"decision": "escalated", "rationale": "deferred for resources"}
    )
    trace = SimpleNamespace(
        tasks=[build],
        dependencies=[],
        requirements=[approved],
        scheduler_deferrals=[_deferral({build.id: "Concurrent task limit reached."})],
        orchestr_decisions=[escalation],
    )

    assert pending_reasons(trace) == {
        build.id: "Concurrent task limit reached. [escalated to the user]"
    }
