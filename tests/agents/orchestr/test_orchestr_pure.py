"""Pure-function tests: priority formula, blocker routing, user-communication decision, planner."""

import pytest

from aether.agents.orchestr.communication import (
    UpdateEvent,
    UpdateKind,
    build_handoff,
    should_notify_user,
)
from aether.agents.orchestr.limits import OrchestrLimits
from aether.agents.orchestr.planner import (
    ObjectiveKind,
    PlanError,
    TaskSpec,
    apply_refinement,
    build_plan,
    classify_objective,
    plan_depth,
    topological_order,
    validate_plan,
)
from aether.agents.orchestr.priority import (
    Boost,
    PriorityInputs,
    PriorityPolicy,
    boost_contribution,
    compute_priority,
    format_boost_reason,
    format_set_reason,
    parse_boost_reason,
    parse_set_reason,
    retention,
)
from aether.agents.orchestr.routing import route_blocker
from aether.agents.orchestr.schemas import ExtraTask, PlanRefinement
from aether.communication.contracts import BlockerKind, Urgency
from aether.communication.messages import AgentId

POLICY = PriorityPolicy()


# --- priority ----------------------------------------------------------------------------


def test_decay_is_monotonic_and_floored() -> None:
    values = [retention(hours, POLICY) for hours in (0, 1, 24, 72, 500, 10_000)]
    assert values == sorted(values, reverse=True)
    assert values[0] == 1.0
    assert values[-1] == POLICY.retention_floor


def test_decay_lowers_effective_priority_without_reaching_zero() -> None:
    fresh = compute_priority(PriorityInputs(base_priority=60, hours_since_boost=0), POLICY)
    stale = compute_priority(PriorityInputs(base_priority=60, hours_since_boost=5000), POLICY)
    assert stale.effective < fresh.effective
    assert stale.effective >= POLICY.min_effective_priority


def test_explicit_zero_base_stays_zero() -> None:
    assert (
        compute_priority(PriorityInputs(base_priority=0, hours_since_boost=0), POLICY).effective
        == 0
    )


def test_boost_raises_priority_and_restarting_decay_keeps_it_higher() -> None:
    base = PriorityInputs(base_priority=50, hours_since_boost=200)
    boosted = PriorityInputs(
        base_priority=50, hours_since_boost=0, boosts=(Boost(points=15, age_hours=0),)
    )
    assert compute_priority(boosted, POLICY).effective > compute_priority(base, POLICY).effective


def test_boosts_fade_and_are_capped() -> None:
    fresh = boost_contribution([Boost(points=30, age_hours=0)], POLICY)
    later = boost_contribution([Boost(points=30, age_hours=POLICY.boost_half_life_hours)], POLICY)
    assert later == pytest.approx(fresh / 2)
    many = [Boost(points=30, age_hours=0)] * 10
    assert boost_contribution(many, POLICY) == POLICY.max_boost_points


def test_deadline_urgency_grows_as_deadline_nears() -> None:
    def effective(hours: float | None) -> int:
        inputs = PriorityInputs(base_priority=40, hours_since_boost=0, hours_to_deadline=hours)
        return compute_priority(inputs, POLICY).effective

    assert effective(None) <= effective(60) <= effective(10) <= effective(0) <= 100
    assert effective(0) > effective(None)


def test_priority_is_bounded() -> None:
    inputs = PriorityInputs(
        base_priority=100,
        hours_since_boost=0,
        hours_to_deadline=-5,
        dependency_criticality=1,
        expected_benefit=1,
        boosts=(Boost(points=40, age_hours=0),),
    )
    assert compute_priority(inputs, POLICY).effective == 100


def test_cost_lowers_priority() -> None:
    cheap = PriorityInputs(base_priority=50, hours_since_boost=0, resource_cost=0)
    costly = PriorityInputs(base_priority=50, hours_since_boost=0, resource_cost=1)
    assert compute_priority(costly, POLICY).effective < compute_priority(cheap, POLICY).effective


def test_reason_codecs_round_trip() -> None:
    assert parse_boost_reason(format_boost_reason(7.5, "new evidence")) == (7.5, "new evidence")
    assert parse_set_reason(format_set_reason(80, "user asked")) == (80, "user asked")
    assert parse_set_reason("set:101:no") is None
    assert parse_boost_reason("recomputed: base 10") is None


# --- routing -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "attempts", "assignee", "creates_task", "asks_user", "escalate"),
    [
        (BlockerKind.RESEARCH, 0, AgentId.SCOUT, True, False, False),
        (BlockerKind.RESEARCH, 1, AgentId.ROGUE, True, False, False),
        (BlockerKind.IMPLEMENTATION, 0, AgentId.FORGE, True, False, False),
        (BlockerKind.SECURITY, 0, AgentId.WARDEN, True, False, False),
        (BlockerKind.AUTHORIZATION, 0, AgentId.MUSE, False, True, False),
        (BlockerKind.RESOURCE, 0, AgentId.ORCHESTR, False, False, False),
        (BlockerKind.RESEARCH, 3, None, False, True, True),
        (BlockerKind.AUTHORIZATION, 3, None, False, True, True),
    ],
)
def test_blocker_routing_table(
    kind: BlockerKind,
    attempts: int,
    assignee: AgentId | None,
    creates_task: bool,
    asks_user: bool,
    escalate: bool,
) -> None:
    route = route_blocker(kind, attempts=attempts, max_attempts=3)
    assert (route.assignee, route.creates_task, route.asks_user, route.escalate) == (
        assignee,
        creates_task,
        asks_user,
        escalate,
    )


def test_explicit_route_is_honoured_only_when_allowed_and_first_attempt() -> None:
    assert (
        route_blocker(
            BlockerKind.RESEARCH, attempts=0, max_attempts=3, route_to=AgentId.ROGUE
        ).assignee
        is AgentId.ROGUE
    )
    assert (
        route_blocker(
            BlockerKind.RESEARCH, attempts=0, max_attempts=3, route_to=AgentId.WARDEN
        ).assignee
        is AgentId.SCOUT
    )
    assert (
        route_blocker(
            BlockerKind.RESEARCH, attempts=1, max_attempts=3, route_to=AgentId.SCOUT
        ).assignee
        is AgentId.ROGUE
    )


# --- user communication decision ----------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (UpdateEvent(UpdateKind.OBJECTIVE_COMPLETED), True),
        (UpdateEvent(UpdateKind.OBJECTIVE_COMPLETED, user_originated=False), False),
        (UpdateEvent(UpdateKind.AUTHORIZATION_REQUIRED), True),
        (UpdateEvent(UpdateKind.DECISION_REQUIRED), True),
        (UpdateEvent(UpdateKind.ESCALATION), True),
        (UpdateEvent(UpdateKind.BLOCKER, important=True), True),
        (UpdateEvent(UpdateKind.BLOCKER), False),
        (UpdateEvent(UpdateKind.DEADLINE_AT_RISK, hours_to_deadline=3), True),
        (UpdateEvent(UpdateKind.DEADLINE_AT_RISK, hours_to_deadline=200), False),
        (UpdateEvent(UpdateKind.DEADLINE_AT_RISK), False),
        (UpdateEvent(UpdateKind.TASK_COMPLETED), False),
        (UpdateEvent(UpdateKind.PROGRESS), False),
        (UpdateEvent(UpdateKind.RETRY), False),
        (UpdateEvent(UpdateKind.HEALTH), False),
        (UpdateEvent(UpdateKind.FINDING), False),
    ],
)
def test_user_is_told_only_what_matters(event: UpdateEvent, expected: bool) -> None:
    assert should_notify_user(event) is expected


def test_handoff_is_structured_clipped_and_marks_authorization() -> None:
    from uuid import uuid4

    request_id = uuid4()
    payload = build_handoff(
        UpdateEvent(UpdateKind.AUTHORIZATION_REQUIRED),
        what_happened="x" * 5000,
        blockers=["needs shell access"],
        decisions_required=["approve or deny"],
        authorization_request_id=request_id,
    )
    assert len(payload.what_happened) <= 500
    assert payload.authorization_required
    assert payload.authorization_request_id == request_id
    assert payload.urgency is Urgency.HIGH
    assert payload.personalization_context == {"notice_kind": "authorization_required"}


# --- planner -----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(ObjectiveKind))
def test_template_plans_are_valid_dags(kind: ObjectiveKind) -> None:
    specs = build_plan(kind, "Do the thing", "", ["it works"])
    ordered = validate_plan(specs, OrchestrLimits())
    position = {spec.key: index for index, spec in enumerate(ordered)}
    for spec in specs:
        assert all(position[dep] < position[spec.key] for dep in spec.depends_on)
    keys = {spec.key for spec in specs}
    assert "verification" in keys
    verification = next(spec for spec in specs if spec.key == "verification")
    assert verification.agent is AgentId.LEDGER
    assert verification.acceptance_criteria == ("it works",)


def test_implementation_never_precedes_security_review() -> None:
    for kind in (ObjectiveKind.CAPABILITY, ObjectiveKind.IMPLEMENTATION):
        specs = {spec.key: spec for spec in build_plan(kind, "Build it")}
        assert "security_review" in specs["implementation"].depends_on


def test_classification() -> None:
    assert classify_objective("Add a PDF parsing capability") is ObjectiveKind.CAPABILITY
    assert classify_objective("Research vector databases") is ObjectiveKind.RESEARCH


def test_cycles_and_unknown_dependencies_are_rejected() -> None:
    cyclic = [
        TaskSpec(key="a", title="a", agent=AgentId.SCOUT, depends_on=("b",)),
        TaskSpec(key="b", title="b", agent=AgentId.SCOUT, depends_on=("a",)),
    ]
    with pytest.raises(PlanError):
        topological_order(cyclic)
    with pytest.raises(PlanError):
        topological_order([TaskSpec(key="a", title="a", agent=AgentId.SCOUT, depends_on=("zz",))])


def test_plan_bounds_are_enforced() -> None:
    chain = [
        TaskSpec(
            key=f"t{i}",
            title="t",
            agent=AgentId.SCOUT,
            depends_on=(f"t{i - 1}",) if i else (),
        )
        for i in range(6)
    ]
    assert plan_depth(chain) == 6
    with pytest.raises(PlanError):
        validate_plan(chain, OrchestrLimits(max_plan_depth=3))
    with pytest.raises(PlanError):
        validate_plan(chain, OrchestrLimits(max_tasks_per_objective=5))


def test_refinement_extras_are_merged_and_forge_extras_wait_for_security() -> None:
    base = build_plan(ObjectiveKind.CAPABILITY, "Build it")
    refinement = PlanRefinement(
        extra_tasks=[
            ExtraTask(key="spike", title="Spike", agent=AgentId.FORGE),
            ExtraTask(key="survey", title="Survey", agent=AgentId.SCOUT),
        ]
    )
    merged = {spec.key: spec for spec in apply_refinement(base, refinement, OrchestrLimits())}
    assert "security_review" in merged["extra.spike"].depends_on
    assert "extra.survey" in merged["consolidation"].depends_on


@pytest.mark.parametrize("agent", [AgentId.WARDEN, AgentId.MUSE, AgentId.ORCHESTR])
def test_refinement_cannot_assign_privileged_agents(agent: AgentId) -> None:
    base = build_plan(ObjectiveKind.CAPABILITY, "Build it")
    refinement = PlanRefinement(extra_tasks=[ExtraTask(key="x", title="X", agent=agent)])
    with pytest.raises(PlanError):
        apply_refinement(base, refinement, OrchestrLimits())


def test_refinement_cannot_exceed_limits_or_make_cycles() -> None:
    base = build_plan(ObjectiveKind.CAPABILITY, "Build it")
    many = PlanRefinement(
        extra_tasks=[ExtraTask(key=f"e{i}", title="E", agent=AgentId.SCOUT) for i in range(6)]
    )
    with pytest.raises(PlanError):
        apply_refinement(base, many, OrchestrLimits(max_refinement_tasks=5))
    cyclic = PlanRefinement(
        extra_tasks=[
            ExtraTask(
                key="loop",
                title="Loop",
                agent=AgentId.SCOUT,
                depends_on=["consolidation"],
                before="consolidation",
            ),
        ]
    )
    with pytest.raises(PlanError):
        apply_refinement(base, cyclic, OrchestrLimits())
    unknown = PlanRefinement(
        extra_tasks=[ExtraTask(key="x", title="X", agent=AgentId.SCOUT, depends_on=["nope"])]
    )
    with pytest.raises(PlanError):
        apply_refinement(base, unknown, OrchestrLimits())
