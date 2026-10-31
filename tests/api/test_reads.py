from decimal import Decimal

import pytest
from sqlalchemy.exc import OperationalError

from aether.api.schemas import Page
from aether.storage.repositories import ObjectiveRepository

COLLECTIONS = (
    "/agents",
    "/objectives",
    "/tasks",
    "/requirements",
    "/capabilities",
    "/security/decisions",
    "/security/pending-approvals",
    "/scheduler/decisions",
    "/resources",
    "/errors",
    "/executions",
    "/tests",
    "/events",
    "/messages",
    "/providers/usage",
)


@pytest.mark.parametrize("path", COLLECTIONS)
def test_collections_are_real_paginated_rows(seeded, path):
    client, ids = seeded
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    page = Page.model_validate(response.json())
    assert page.items and page.total >= len(page.items)
    assert page.limit == 50 and page.offset == 0
    expected = {
        "/objectives": "objective",
        "/tasks": "task",
        "/requirements": "requirement",
        "/capabilities": "capability",
        "/security/decisions": "decision",
        "/security/pending-approvals": "authorization",
        "/resources": "resource",
        "/errors": "error",
        "/executions": "execution",
        "/tests": "test_run",
        "/messages": "message",
    }
    if path in expected:
        assert str(ids[expected[path]]) in {item["id"] for item in page.items}
    if path == "/scheduler/decisions":
        assert page.items[0]["payload"]["selected_task_id"] == str(ids["task"])
    if path == "/events":
        assert any(row["event_type"] == "task.created" for row in page.items)


@pytest.mark.parametrize("path", COLLECTIONS)
def test_empty_database_never_returns_sample_rows(client, path):
    response = client.get(path)
    assert response.status_code == 200
    body = response.json()
    if path == "/providers/usage":
        assert all(row["today"]["requests"] == 0 for row in body["items"])
    else:
        assert body["items"] == [] and body["total"] == 0


@pytest.mark.parametrize("path", COLLECTIONS)
def test_pagination_is_bounded_and_offset_is_real(seeded, path):
    client, _ = seeded
    whole = client.get(path).json()
    first = client.get(path, params={"limit": 1}).json()
    second = client.get(path, params={"limit": 1, "offset": 1}).json()
    assert first["total"] == whole["total"]
    assert len(first["items"]) == 1
    assert second["items"] == whole["items"][1:2]
    assert client.get(path, params={"limit": 200}).status_code == 200
    for params in ({"limit": 0}, {"limit": 201}, {"offset": -1}):
        assert client.get(path, params=params).status_code == 422
    assert client.get(path, params={"offset": 10_000}).json()["items"] == []


def test_agent_latest_heartbeat_and_unknown_status(seeded):
    client, ids = seeded
    agents = {row["agent_id"]: row for row in client.get("/agents").json()["items"]}
    assert agents["FORGE"]["status"] == "busy"
    beat = agents["FORGE"]["latest_heartbeat"]
    assert beat["id"] == str(ids["heartbeat"])
    assert beat["current_task_id"] == str(ids["running"])
    assert beat["observed_at"].endswith("+00:00")
    assert agents["SCOUT"]["status"] is None
    assert agents["SCOUT"]["latest_heartbeat"] is None


def test_health_reports_db_not_assumed_worker_liveness(seeded):
    client, _ = seeded
    result = client.get("/health")
    assert result.status_code == 200
    assert result.json()["database"] == "reachable"
    assert result.json()["latest_heartbeat_at"] is not None


def test_health_reports_no_heartbeat_for_empty_database(client):
    assert client.get("/health").json()["latest_heartbeat_at"] is None


def test_health_and_reads_report_database_failures_without_details(
    client, monkeypatch, caplog, api_token
):
    async def broken(*args, **kwargs):
        raise OperationalError("statement", {"token": api_token}, Exception(api_token))

    monkeypatch.setattr(client.app.state.services.repository, "health", broken)
    monkeypatch.setattr(client.app.state.services.repository, "objectives", broken)
    health = client.get("/health")
    assert health.status_code == 503
    assert health.json()["status"] == "unavailable"
    read = client.get("/objectives")
    assert read.status_code == 503
    assert api_token not in health.text + read.text + caplog.text
    assert "API health query failed" in caplog.text
    assert "API database operation failed" in caplog.text


def test_objective_progress_and_fresh_reads(seeded):
    client, ids = seeded
    objective = client.get(f"/objectives/{ids['objective']}").json()
    progress = objective["progress"]
    assert progress["total_tasks"] == 3 and progress["completed_tasks"] == 1
    assert progress["completion_percent"] == pytest.approx(100 / 3)
    assert progress["tasks_by_state"] == {"completed": 1, "ready": 1, "running": 1}
    taskless = client.get(f"/objectives/{ids['taskless']}").json()
    assert taskless["progress"]["completion_percent"] is None

    async def create():
        return await ObjectiveRepository(client.app.state.services.database.sessions).create(
            title="Created after API startup"
        )

    new_id = client.portal.call(create)
    assert str(new_id) in {row["id"] for row in client.get("/objectives").json()["items"]}


def test_task_detail_dependencies_lease_attempts_and_events(seeded):
    client, ids = seeded
    body = client.get(f"/tasks/{ids['task']}").json()
    assert body["dependencies"]["items"][0]["depends_on_task_id"] == str(ids["dependency"])
    assert body["attempts"] == {"current": 0, "maximum": 3}
    assert body["executions"]["items"][0]["id"] == str(ids["execution"])
    assert body["events"]["total"] >= 3
    running = client.get(f"/tasks/{ids['running']}").json()
    assert running["lease"]["owner"] == "live-worker"
    assert running["lease"]["expires_at"] is not None
    assert running["attempts"]["current"] == 1
    second = client.get(f"/tasks/{ids['task']}", params={"limit": 1, "offset": 1}).json()
    assert len(second["events"]["items"]) == 1
    assert second["dependencies"]["items"] == []


def test_task_event_and_message_filters(seeded):
    client, ids = seeded
    tasks = client.get("/tasks", params={"state": "ready", "agent": "FORGE"}).json()
    assert [row["id"] for row in tasks["items"]] == [str(ids["task"])]
    assert client.get("/tasks", params={"state": "not-a-state"}).status_code == 422
    assert client.get("/tasks", params={"agent": "no-such-agent"}).json()["total"] == 0
    events = client.get(
        "/events", params={"type": "task.created", "task_id": str(ids["task"])}
    ).json()
    assert events["total"] == 1
    assert events["items"][0]["aggregate_id"] == str(ids["task"])
    messages = client.get("/messages", params={"correlation_id": str(ids["correlation"])}).json()
    assert messages["total"] == 1 and messages["items"][0]["id"] == str(ids["message"])
    assert client.get("/messages", params={"correlation_id": "invalid"}).status_code == 422
    assert client.get("/events", params={"task_id": "invalid"}).status_code == 422


def test_capability_verification_test_results_and_recovery_evidence(seeded):
    client, ids = seeded
    capability = client.get("/capabilities").json()["items"][0]
    assert capability["lifecycle_status"] == capability["verification_status"] == "verified"
    run = capability["verification_evidence"]["items"][0]
    assert run["id"] == str(ids["test_run"]) and run["verdict"] == "verified"
    assert run["acceptance_results"][0]["passed"] is True
    result = run["results"]["items"][0]
    assert result["id"] == str(ids["test_result"]) and result["outcome"] == "passed"
    tests = client.get("/tests").json()["items"][0]
    assert tests["results"]["items"][0]["name"] == "test_acceptance"
    error = client.get("/errors").json()["items"][0]
    assert error["recovery_attempts"]["items"][0]["id"] == str(ids["recovery"])


def test_pending_approvals_excludes_expired_without_mutating(seeded):
    client, ids = seeded
    pending = client.get("/security/pending-approvals").json()
    assert {row["id"] for row in pending["items"]} == {
        str(ids["authorization"]),
        str(ids["undelivered"]),
    }


def test_provider_usage_utc_day_provider_and_model_budgets(seeded):
    client, _ = seeded
    rows = client.get("/providers/usage").json()["items"]
    groq = next(row for row in rows if row["provider"] == "groq")
    assert groq["today"]["requests"] == 2
    assert groq["today"]["tokens"] == 180
    assert Decimal(str(groq["today"]["cost_usd"])) == Decimal("0.012")
    # Outstanding pre-midnight reservations still count toward global concurrency.
    assert groq["today"]["active"] == 1
    assert groq["budget"]["max_requests_per_day"] == 2000
    model = groq["model_budgets"][0]
    assert model["model"] == "model-a"
    assert model["today"]["tokens"] == 140
    assert model["budget"]["max_requests_per_day"] == 7
    ollama = next(row for row in rows if row["provider"] == "ollama")
    assert ollama["budget"]["max_cost_usd_per_day"] is None


@pytest.mark.parametrize("resource", ["objectives", "tasks"])
def test_missing_and_invalid_ids_are_explicit(seeded, resource):
    client, ids = seeded
    assert client.get(f"/{resource}/{ids['unknown']}").status_code == 404
    assert client.get(f"/{resource}/invalid").status_code == 422


def test_responses_scrub_credentials_but_keep_reference_ids(
    seeded, caplog, api_token, provider_token
):
    client, ids = seeded
    text = "".join(client.get(path).text for path in COLLECTIONS)
    text += client.get(f"/objectives/{ids['objective']}").text
    text += client.get(f"/tasks/{ids['task']}").text
    assert api_token not in text + caplog.text
    assert provider_token not in text + caplog.text
    for secret in ("fake-agent-password", "fake-db-password", "fake-error-password"):
        assert secret not in text
    assert client.get("/agents").json()["items"][0]["config"]["apiKey"] == "***"


def test_openapi_documents_all_routes_and_admin_bearer(client):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    for path in COLLECTIONS:
        assert "get" in schema["paths"][path]
    for path in (
        "/objectives",
        "/authorizations/{request_id}/respond",
        "/tasks/{task_id}/pause",
        "/tasks/{task_id}/cancel",
        "/tasks/{task_id}/resume",
    ):
        assert schema["paths"][path]["post"]["security"] == [{"HTTPBearer": []}]
    assert "security" not in schema["paths"]["/health"]["get"]
