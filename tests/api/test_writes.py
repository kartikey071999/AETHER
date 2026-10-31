from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from aether.api.schemas import QueuedObjective, QueuedResponse
from aether.communication.contracts import (
    ObjectiveCreatePayload,
    UserDecision,
    UserResponsePayload,
    validate_message,
)
from aether.storage.models import AuthorizationRequestRecord, InteractionRecord, MessageRecord


async def records(client, model):
    async with client.app.state.services.database.sessions() as session:
        return list(await session.scalars(select(model)))


@pytest.mark.parametrize(
    "path",
    [
        "/objectives",
        "/authorizations/00000000-0000-0000-0000-000000000000/respond",
        "/tasks/00000000-0000-0000-0000-000000000000/pause",
        "/tasks/00000000-0000-0000-0000-000000000000/cancel",
        "/tasks/00000000-0000-0000-0000-000000000000/resume",
    ],
)
def test_every_write_requires_auth_and_is_disabled_without_token(client, path, caplog, api_token):
    for headers in (
        {},
        {"Authorization": "Bearer wrong-private-value"},
        {"Authorization": "Basic wrong-private-value"},
    ):
        result = client.post(path, headers=headers, json={})
        assert result.status_code == 401
        assert result.headers["www-authenticate"] == "Bearer"
    assert client.post(path, params={"token": api_token}, json={}).status_code == 401
    client.app.state.services.settings.api_token = None
    assert client.post(path, json={}).status_code == 503
    assert client.portal.call(records, client, MessageRecord) == []
    assert api_token not in caplog.text


def test_objective_submission_publishes_validated_user_message(
    seeded, admin_headers, caplog, api_token
):
    client, _ = seeded
    count = client.get("/objectives").json()["total"]
    response = client.post(
        "/objectives",
        headers=admin_headers,
        json={
            "title": "A new user objective",
            "completion_criteria": ["Offline success"],
            "priority": 80,
            "deadline": "2026-12-01T00:00:00Z",
        },
    )
    assert response.status_code == 202
    queued = QueuedObjective.model_validate(response.json())
    rows = client.portal.call(records, client, MessageRecord)
    row = next(row for row in rows if row.id == queued.message_id)
    api = client.app.state.services
    message = api.bus._to_message(row, row.status)
    payload = validate_message(message)
    assert isinstance(payload, ObjectiveCreatePayload)
    assert payload.title == "A new user objective" and payload.generated is False
    assert row.message_type == "objective.create"
    assert row.source_agent == "MUSE" and row.target_agent == "ORCHESTR"
    assert row.correlation_id == queued.correlation_id and row.status == "pending"
    assert client.get("/objectives").json()["total"] == count
    assert api_token not in response.text + caplog.text


@pytest.mark.parametrize(
    "body",
    [
        {"title": ""},
        {"title": "x", "priority": 101},
        {"title": "x", "generated": True},
        {"title": "x", "unrecognized": "private-invalid-input"},
        {"title": "x", "deadline": "timezone-missing"},
    ],
)
def test_invalid_objective_never_publishes(client, admin_headers, body):
    response = client.post("/objectives", headers=admin_headers, json=body)
    assert response.status_code == 422
    assert client.portal.call(records, client, MessageRecord) == []
    assert "private-invalid-input" not in response.text


def test_missing_objective_parent_is_rejected(client, admin_headers):
    response = client.post(
        "/objectives",
        headers=admin_headers,
        json={"title": "A user objective", "parent_objective_id": str(uuid4())},
    )
    assert response.status_code == 404
    assert client.portal.call(records, client, MessageRecord) == []


@pytest.mark.parametrize("decision", ["approved", "denied"])
def test_authorization_response_links_request_and_queues_for_warden(
    seeded, admin_headers, decision
):
    client, ids = seeded
    response_id = str(uuid4())
    body = {
        "decision": decision,
        "text": "Explicit answer to this exact input",
        "person_id": str(ids["person"]),
        "response_id": response_id,
    }
    path = f"/authorizations/{ids['authorization']}/respond"
    response = client.post(path, headers=admin_headers, json=body)
    assert response.status_code == 202
    queued = QueuedResponse.model_validate(response.json())
    assert queued.request_id == ids["authorization"]
    retry = client.post(path, headers=admin_headers, json=body)
    assert retry.status_code == 202 and retry.json() == response.json()
    interactions = client.portal.call(records, client, InteractionRecord)
    inbound = next(row for row in interactions if row.id == queued.interaction_id)
    assert inbound.authorization_request_id == ids["authorization"]
    assert inbound.objective_id == ids["objective"] and inbound.task_id == ids["task"]
    rows = client.portal.call(records, client, MessageRecord)
    responses = [row for row in rows if row.message_type == "user.response"]
    assert {row.target_agent for row in responses} == {"WARDEN", "ORCHESTR"}
    for row in responses:
        payload = UserResponsePayload.model_validate(row.payload)
        assert payload.decision == UserDecision(decision)
        assert payload.authorization_request_id == ids["authorization"]
        assert row.correlation_id == ids["correlation"]
    stored = client.portal.call(records, client, AuthorizationRequestRecord)
    authorization = next(row for row in stored if row.id == ids["authorization"])
    assert authorization.status == "pending" and authorization.responded_at is None
    changed = {**body, "text": "A different answer"}
    assert client.post(path, headers=admin_headers, json=changed).status_code == 409


@pytest.mark.parametrize("decision", ["approved", "denied"])
def test_expired_authorization_cannot_be_approved_or_forwarded(seeded, admin_headers, decision):
    client, ids = seeded
    response = client.post(
        f"/authorizations/{ids['expired']}/respond",
        headers=admin_headers,
        json={"decision": decision, "text": "Explicit response"},
    )
    assert response.status_code == 409
    assert "expired" in response.json()["detail"]
    rows = client.portal.call(records, client, MessageRecord)
    assert all(row.message_type != "user.response" for row in rows)


def test_authorization_response_requires_delivered_correct_audience(seeded, admin_headers):
    client, ids = seeded
    body = {"decision": "approved", "text": "Explicit response"}
    assert (
        client.post(
            f"/authorizations/{ids['authorization']}/respond", headers=admin_headers, json=body
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/authorizations/{ids['undelivered']}/respond", headers=admin_headers, json=body
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/authorizations/{ids['unknown']}/respond", headers=admin_headers, json=body
        ).status_code
        == 404
    )
    assert all(
        row.message_type != "user.response"
        for row in client.portal.call(records, client, MessageRecord)
    )


def test_authorization_expiring_during_response_is_rechecked_by_muse(seeded, admin_headers):
    client, ids = seeded
    context = client.app.state.services.muse_context()
    context.clock = lambda: datetime.now(UTC) + timedelta(hours=2)
    response = client.post(
        f"/authorizations/{ids['authorization']}/respond",
        headers=admin_headers,
        json={
            "decision": "approved",
            "text": "A response that arrived too late",
            "person_id": str(ids["person"]),
        },
    )
    assert response.status_code == 409
    assert all(
        row.message_type != "user.response"
        for row in client.portal.call(records, client, MessageRecord)
    )


@pytest.mark.parametrize(
    "body",
    [{"decision": "none", "text": "Yes"}, {"decision": "approved", "text": "   "}, {"text": "Yes"}],
)
def test_silence_or_inferred_decisions_are_rejected(seeded, admin_headers, body):
    client, ids = seeded
    assert (
        client.post(
            f"/authorizations/{ids['authorization']}/respond", headers=admin_headers, json=body
        ).status_code
        == 422
    )


def test_admin_pause_resume_cancel_use_durable_control(seeded, admin_headers):
    client, ids = seeded
    task = ids["task"]
    response = client.post(
        f"/tasks/{task}/pause", headers=admin_headers, json={"reason": "User pause"}
    )
    assert response.status_code == 200 and response.json()["state"] == "paused"
    assert client.get(f"/tasks/{task}").json()["state"] == "paused"
    response = client.post(f"/tasks/{task}/resume", headers=admin_headers)
    assert response.status_code == 200 and response.json()["state"] == "ready"
    assert client.post(f"/tasks/{task}/resume", headers=admin_headers).status_code == 409
    response = client.post(f"/tasks/{task}/cancel", headers=admin_headers)
    assert response.status_code == 200 and response.json()["state"] == "cancelled"
    assert client.get(f"/tasks/{task}").json()["state"] == "cancelled"
    events = client.get("/events", params={"task_id": str(task)}).json()["items"]
    assert any(
        row["event_type"] == "task.control_requested" and row["actor"] == "API" for row in events
    )


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_running_task_control_is_cooperative_preserves_lease(seeded, admin_headers, action):
    client, ids = seeded
    path = f"/tasks/{ids['running']}"
    before = client.get(path).json()
    response = client.post(f"{path}/{action}", headers=admin_headers)
    assert response.status_code == 202 and response.json()["outcome"] == "pending"
    after = client.get(path).json()
    assert after["state"] == "running" and after["lease"] == before["lease"]

    async def pending():
        return await client.app.state.services.control.pending_action(ids["running"])

    assert client.portal.call(pending).value == action


@pytest.mark.parametrize("action", ["pause", "cancel", "resume"])
def test_missing_task_control_returns_404(seeded, admin_headers, action):
    client, ids = seeded
    assert (
        client.post(f"/tasks/{ids['unknown']}/{action}", headers=admin_headers).status_code == 404
    )


def test_invalid_inputs_do_not_echo_secret_values(seeded, admin_headers, caplog, api_token):
    client, ids = seeded
    response = client.post(
        "/objectives", headers=admin_headers, json={"title": {"api_token": api_token}}
    )
    assert response.status_code == 422 and api_token not in response.text + caplog.text
    bad_control = client.post(
        f"/tasks/{ids['task']}/pause",
        headers=admin_headers,
        json={"reason": "x" * 501, "secret": api_token},
    )
    assert bad_control.status_code == 422 and api_token not in bad_control.text + caplog.text


def test_unicode_tokens_compare_as_bytes_without_exceptions(client):
    # compare_digest(str, str) rejects non-ASCII; the API compares encoded byte strings.
    token = "unicode-" + chr(0x2603)
    client.app.state.services.settings.api_token = SecretStr(token)
    api = client.app.state.services

    from fastapi.security import HTTPAuthorizationCredentials

    from aether.api.services import require_admin

    require_admin(api, HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))


def test_config_and_cli_use_central_settings(monkeypatch, capsys):
    import uvicorn

    from aether.config import AetherSettings
    from aether.main import build_parser, main

    for name in ("AETHER_API_TOKEN", "AETHER_API_HOST", "AETHER_API_PORT"):
        monkeypatch.delenv(name, raising=False)
    settings = AetherSettings(_env_file=None)
    assert settings.api_host == "127.0.0.1" and settings.api_port == 8000
    assert settings.api_token is None
    for value in ("", " ", SecretStr("")):
        assert AetherSettings(_env_file=None, api_token=value).api_token is None
    with pytest.raises(ValueError):
        AetherSettings(_env_file=None, api_port=65536)
    monkeypatch.setenv("AETHER_API_TOKEN", "private-cli-test-token")
    monkeypatch.setenv("AETHER_API_PORT", "9123")
    monkeypatch.setenv("AETHER_DATABASE_URL", "sqlite+aiosqlite://")
    called = {}

    def serve(app, **kwargs):
        called.update(kwargs)
        assert app.state.services.settings.api_token.get_secret_value() == "private-cli-test-token"

    monkeypatch.setattr(uvicorn, "run", serve)
    assert build_parser().parse_args(["api", "serve"]).api_command == "serve"
    main(["api", "serve"])
    assert called == {"host": "127.0.0.1", "port": 9123, "access_log": False}
    assert "private-cli-test-token" not in capsys.readouterr().out
