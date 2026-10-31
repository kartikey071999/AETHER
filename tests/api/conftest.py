import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine

from aether.agents.muse.repository import MuseRepository
from aether.api import create_app
from aether.communication.contracts import (
    CommunicationRequestPayload,
    ProgressUpdatePayload,
    build_message,
)
from aether.communication.messages import AgentId
from aether.config import AetherSettings
from aether.models.settings import BudgetPolicy, ProviderSettings
from aether.runtime.leases import LeaseManager
from aether.runtime.scheduler import Scheduler, SchedulerPolicy, SchedulingDecision
from aether.storage.models import (
    AgentHealthStatus,
    Base,
    CapabilityStatus,
    ResultOutcome,
    RunStatus,
    TaskStatus,
    TestVerdict,
)
from aether.storage.repositories import (
    AgentHeartbeatRepository,
    AgentRepository,
    AuthorizationRequestRepository,
    CapabilityRepository,
    ErrorRepository,
    ObjectiveRepository,
    PersonRepository,
    RequirementRepository,
    ResourceMeasurementRepository,
    TaskDependencyRepository,
    TaskRepository,
)
from aether.storage.repositories.execution import ExecutionRunRepository
from aether.storage.repositories.provider_usage import ProviderUsageRepository
from aether.storage.repositories.security import SecurityDecisionRepository
from aether.storage.repositories.test_runs import TestRunRepository

API_TOKEN = "private-local-api-test-value"
PROVIDER_TOKEN = "private-provider-test-value"


@pytest.fixture(scope="session")
def api_schema(tmp_path_factory):
    path = tmp_path_factory.mktemp("api-schema") / "empty.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(connection)
    finally:
        engine.dispose()
    return path


@pytest.fixture
def client(tmp_path, api_schema) -> Iterator[TestClient]:
    path = tmp_path / "api.db"
    shutil.copyfile(api_schema, path)
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    settings = AetherSettings(
        _env_file=None,
        database_url=url,
        api_token=SecretStr(API_TOKEN),
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspaces",
        artifact_dir=tmp_path / "artifacts",
    )
    providers = ProviderSettings(
        _env_file=None,
        groq_api_key=SecretStr(PROVIDER_TOKEN),
        model_budgets={"groq/model-a": BudgetPolicy(max_requests_per_day=7)},
    )
    app = create_app(settings, provider_settings=providers)
    with TestClient(app) as client:
        yield client


async def populate(client):
    api = client.app.state.services
    sessions = api.database.sessions
    now = datetime.now(UTC)
    objectives = ObjectiveRepository(sessions)
    objective = await objectives.create(
        title="Real stored objective",
        description=f"Opaque credential {PROVIDER_TOKEN}; Bearer {API_TOKEN}; password=fake-db-password",
    )
    taskless = await objectives.create(title="No task evidence")
    tasks = TaskRepository(sessions)
    task = await tasks.create(
        objective_id=objective,
        title="Actual ready task",
        assigned_agent="FORGE",
        priority=70,
    )
    dependency = await tasks.create(
        objective_id=objective, title="Completed prerequisite", assigned_agent="LEDGER"
    )
    running = await tasks.create(
        objective_id=objective, title="Live worker task", assigned_agent="FORGE", priority=60
    )
    for task_id in (task, dependency, running):
        await tasks.transition(task_id, TaskStatus.READY, actor="ORCHESTR")
    leases = LeaseManager(sessions)
    await leases.acquire(dependency, "completed-worker", timedelta(minutes=10))
    await leases.release(dependency, "completed-worker", TaskStatus.COMPLETED)
    await leases.acquire(running, "live-worker", timedelta(minutes=10))
    await TaskDependencyRepository(sessions).add(task, dependency, actor="ORCHESTR")
    agents = AgentRepository(sessions)
    await agents.upsert(
        "FORGE",
        responsibility="Build actual work",
        config={"apiKey": PROVIDER_TOKEN, "nested": {"password": "fake-agent-password"}},
    )
    await agents.upsert("SCOUT", responsibility="No heartbeat yet")
    beats = AgentHeartbeatRepository(sessions)
    await beats.record("FORGE", AgentHealthStatus.IDLE)
    heartbeat = await beats.record(
        "FORGE",
        AgentHealthStatus.BUSY,
        current_task_id=running,
        details={"access_token": API_TOKEN},
    )
    requirement = await RequirementRepository(sessions).create(
        objective_id=objective,
        title="Offline verification",
        requirement_type="implementation",
        acceptance_criteria=["A passing test"],
        created_by="SCOUT",
    )
    capabilities = CapabilityRepository(sessions)
    capability = await capabilities.register(
        name="Local verified tool",
        actor="LEDGER",
        objective_id=objective,
        requirement_id=requirement,
    )
    for status in (
        CapabilityStatus.PROPOSED,
        CapabilityStatus.APPROVED,
        CapabilityStatus.INSTALLED,
        CapabilityStatus.TESTED,
        CapabilityStatus.VERIFIED,
    ):
        await capabilities.transition(capability, status, actor="LEDGER")
    async with sessions() as session, session.begin():
        decision = await SecurityDecisionRepository.stage(
            session,
            decision="approved",
            operation="file_read",
            scope={"api_key": PROVIDER_TOKEN},
            evidence=[{"summary": "Exact input checked"}],
            rationale="Allowed input",
            input_hash="a" * 64,
            expires_at=now + timedelta(hours=1),
            created_at=now,
            task_id=task,
        )
        execution = await ExecutionRunRepository.stage_started(
            session, agent="FORGE", operation="offline build", started_at=now, task_id=task
        )
        await ExecutionRunRepository.stage_finished(
            session,
            execution.id,
            status=RunStatus.SUCCEEDED,
            finished_at=now,
            exit_code=0,
        )
        decision_id, execution_id = decision.id, execution.id
    resource_id = await ResourceMeasurementRepository(sessions).record(
        cpu_percent=12.5,
        available_ram_bytes=123456,
        free_disk_bytes=654321,
        observed_at=now,
    )
    runs = TestRunRepository(sessions)
    test_run = await runs.create(
        suite="Actual offline suite",
        capability_id=capability,
        task_id=task,
        execution_run_id=execution_id,
    )
    result = await runs.add_result(
        test_run, name="test_acceptance", outcome=ResultOutcome.PASSED, duration_ms=7
    )
    await runs.finish(
        test_run,
        verdict=TestVerdict.VERIFIED,
        acceptance_results=[
            {"criterion": "A passing test", "passed": True, "evidence": "1 passed"}
        ],
    )
    errors = ErrorRepository(sessions)
    error = await errors.record(
        error_type="TransientFailure",
        message=f"Bearer {API_TOKEN}",
        agent="FORGE",
        aggregate_type="task",
        aggregate_id=task,
        details={"password": "fake-error-password", "api_token": API_TOKEN},
    )
    recovery = await errors.record_recovery_attempt(
        error, action="retry", outcome="scheduled", actor="ORCHESTR"
    )
    await Scheduler(SchedulerPolicy(), sessions).record_decision(
        SchedulingDecision(task, 70.0, "Fits resources", (), now)
    )
    person = await PersonRepository(sessions).create(
        display_name="Local owner", access_level="full"
    )
    authorizations = AuthorizationRequestRepository(sessions)
    authorization = await authorizations.create(
        actor="WARDEN",
        question="Allow this input?",
        objective_id=objective,
        task_id=task,
        security_decision_id=decision_id,
        expires_at=now + timedelta(hours=1),
        input_hash="a" * 64,
    )
    expired = await authorizations.create(
        actor="WARDEN", question="Expired input?", expires_at=now - timedelta(seconds=1)
    )
    undelivered = await authorizations.create(
        actor="WARDEN", question="Not delivered?", expires_at=now + timedelta(hours=1)
    )
    handoff = build_message(
        AgentId.ORCHESTR,
        AgentId.MUSE,
        CommunicationRequestPayload(
            what_happened="User authorization requested",
            audience=person,
            objective_id=objective,
            task_id=task,
            authorization_required=True,
            authorization_request_id=authorization,
        ),
        objective_id=objective,
        task_id=task,
    )
    muse = MuseRepository(sessions)
    outbox = await muse.prepare_outbox(
        handoff,
        CommunicationRequestPayload.model_validate(handoff.payload),
        rendered="Explicit approval or denial required",
        canonical_text="Explicit approval or denial required",
        channel="console",
        data_dir=api.settings.data_dir,
        now=now,
        dedupe_window=timedelta(minutes=5),
    )
    await muse.mark_delivered(outbox, now)
    message = build_message(
        AgentId.FORGE,
        AgentId.ORCHESTR,
        ProgressUpdatePayload(
            task_id=task, summary="Actual progress", details={"secret": PROVIDER_TOKEN}
        ),
        task_id=task,
        objective_id=objective,
        correlation_id=handoff.correlation_id,
    )
    await api.bus.publish(message)
    usage = ProviderUsageRepository(sessions)
    await usage.record(
        provider="groq", model="model-a", input_tokens=100, output_tokens=40, cost_usd="0.010"
    )
    await usage.record(
        provider="groq", model="model-b", input_tokens=30, output_tokens=10, cost_usd="0.002"
    )
    async with usage.transaction() as session:
        ProviderUsageRepository.stage_reservation(
            session,
            provider="groq",
            model="model-a",
            agent="FORGE",
            task_id=task,
            input_tokens=7,
            output_tokens=3,
            cost_usd=Decimal("0.001"),
            occurred_at=now.replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(seconds=1),
        )
    return {
        "objective": objective,
        "taskless": taskless,
        "task": task,
        "dependency": dependency,
        "running": running,
        "heartbeat": heartbeat,
        "requirement": requirement,
        "capability": capability,
        "decision": decision_id,
        "execution": execution_id,
        "resource": resource_id,
        "test_run": test_run,
        "test_result": result,
        "error": error,
        "recovery": recovery,
        "person": person,
        "authorization": authorization,
        "expired": expired,
        "undelivered": undelivered,
        "message": message.message_id,
        "correlation": handoff.correlation_id,
        "unknown": uuid4(),
    }


@pytest.fixture
def seeded(client):
    ids = client.portal.call(populate, client)
    return client, ids


@pytest.fixture
def admin_headers():
    return {"Authorization": f"Bearer {API_TOKEN}"}


@pytest.fixture
def api_token():
    return API_TOKEN


@pytest.fixture
def provider_token():
    return PROVIDER_TOKEN
