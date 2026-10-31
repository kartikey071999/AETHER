import re

import pytest

from aether.storage.repositories import ObjectiveRepository, TaskRepository

PAGES = [
    "/dashboard",
    "/dashboard/objectives",
    "/dashboard/tasks",
    "/dashboard/security",
    "/dashboard/resources",
    "/dashboard/providers",
    "/dashboard/events",
]
EMPTY_STATES = {
    "/dashboard": ["No agents are registered yet.", "No active objectives.", "No errors"],
    "/dashboard/objectives": ["No objectives have been submitted yet."],
    "/dashboard/tasks": ["No tasks match."],
    "/dashboard/security": ["Nothing is waiting for approval.", "No security decisions"],
    "/dashboard/resources": ["No resource measurements"],
    "/dashboard/providers": ["Today&#x27;s usage vs budgets", "<progress"],
    "/dashboard/events": ["No events match."],
}
SCRIPT = "<script>alert('xss')</script>"


def get(client, path, **params):
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.text


@pytest.mark.parametrize("path", PAGES)
def test_empty_database_renders_empty_states(client, path):
    html = get(client, path)
    for message in EMPTY_STATES[path]:
        assert message in html


def test_empty_objective_detail_states(client):
    async def create():
        return await ObjectiveRepository(client.app.state.services.database.sessions).create(
            title="Fresh objective"
        )

    objective = client.portal.call(create)
    html = get(client, f"/dashboard/objectives/{objective}")
    for message in (
        "This objective has no tasks.",
        "No requirements recorded",
        "No research findings recorded",
        "No capabilities are linked",
        "MUSE has not sent a message",
    ):
        assert message in html


def test_unknown_objective_is_not_found(client, seeded):
    client, ids = seeded
    assert client.get(f"/dashboard/objectives/{ids['unknown']}").status_code == 404
    assert client.get("/dashboard/objectives/not-a-uuid").status_code == 422


@pytest.mark.parametrize("path", PAGES)
def test_pages_render_real_rows(seeded, path):
    client, _ = seeded
    html = get(client, path)
    expected = {
        "/dashboard": [
            "FORGE",
            "SCOUT",
            "no heartbeat",
            "Real stored objective",
            "TransientFailure",
        ],
        "/dashboard/objectives": ["Real stored objective", "No task evidence"],
        "/dashboard/tasks": ["Actual ready task", "Completed prerequisite", "Live worker task"],
        "/dashboard/security": ["Allow this input?", "file_read", "Allowed input", "data-respond"],
        "/dashboard/resources": ["12.5%", "<svg", "Recent measurements"],
        "/dashboard/providers": ["groq", "model-a", "<progress"],
        "/dashboard/events": ["scheduler.decision", "task.", "FORGE"],
    }[path]
    for text in expected:
        assert text in html


def test_overview_shows_counts_and_heartbeat_age(seeded):
    client, _ = seeded
    html = get(client, "/dashboard")
    assert "s-completed" in html
    assert "s-running" in html
    assert re.search(r"\d+s ago", html)


def test_objective_detail_shows_every_section(seeded):
    client, ids = seeded
    html = get(client, f"/dashboard/objectives/{ids['objective']}")
    for text in (
        "Real stored objective",
        "Offline verification",
        "A passing test",
        "Local verified tool",
        "Actual offline suite",
        "1 passed, 0 failed, 0 errors",
        "file_read",
        "Allow this input?",
        "Explicit approval or denial required",
    ):
        assert text in html
    # The dependent task is nested under its prerequisite in the task graph.
    graph = html[html.index('class="dag"') :]
    assert graph.index("Completed prerequisite") < graph.index("Actual ready task")
    assert re.search(r"Completed prerequisite.*?<ul>.*?Actual ready task", graph, re.DOTALL)


def test_task_filters(seeded):
    client, _ = seeded
    html = get(client, "/dashboard/tasks", state="completed")
    assert "Completed prerequisite" in html
    assert "Actual ready task" not in html
    html = get(client, "/dashboard/tasks", agent="LEDGER")
    assert "Completed prerequisite" in html
    assert "Live worker task" not in html
    assert "Live worker task" in get(client, "/dashboard/tasks", state="", agent="")
    assert client.get("/dashboard/tasks", params={"state": "bogus"}).status_code == 422


def test_event_filter(seeded):
    client, _ = seeded
    assert "scheduler.decision" in get(client, "/dashboard/events", type="scheduler.decision")
    html = get(client, "/dashboard/events", type="does.not.exist")
    assert "No events match." in html


def test_malicious_content_is_escaped(seeded):
    client, _ = seeded

    async def create():
        sessions = client.app.state.services.database.sessions
        objective = await ObjectiveRepository(sessions).create(
            title=f"{SCRIPT}\"'", description="<img src=x onerror=alert(1)>"
        )
        await TaskRepository(sessions).create(
            objective_id=objective, title=f"<b onmouseover=x>{SCRIPT}", assigned_agent="FORGE"
        )
        return objective

    objective = client.portal.call(create)
    pages = [*PAGES, f"/dashboard/objectives/{objective}"]
    for path in pages:
        html = get(client, path, agent="<x>") if path.endswith("/tasks") else get(client, path)
        assert SCRIPT not in html
        assert "<img src=x" not in html
        assert "<b onmouseover" not in html
    html = get(client, f"/dashboard/objectives/{objective}")
    assert "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "&lt;b onmouseover=x&gt;" in html
    assert 'value="&lt;x&gt;"' in get(client, "/dashboard/tasks", agent="<x>")
    assert 'value="&quot;&gt;&lt;script&gt;' in get(client, "/dashboard/events", type='"><script>')


@pytest.mark.parametrize("path", PAGES)
def test_pages_expose_no_secrets_or_inline_code(seeded, path, api_token, provider_token):
    client, _ = seeded
    response = client.get(path)
    html = response.text
    for secret in (api_token, provider_token, "fake-agent-password", "fake-error-password"):
        assert secret not in html
        assert secret not in str(response.headers)
    assert not re.search(r"<script(?![^>]*\ssrc=)", html)
    assert not re.search(r"\son\w+=|\sstyle=|javascript:", html)
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


def test_fragment_returns_only_refreshable_content(seeded):
    client, _ = seeded
    full = get(client, "/dashboard/security")
    part = get(client, "/dashboard/security", fragment="true")
    assert "<html" in full
    assert 'id="api-token"' in full
    assert "<html" not in part
    assert 'id="api-token"' not in part
    assert "Allow this input?" in part


def test_static_assets_work_offline(client):
    css = client.get("/dashboard/static/dashboard.css")
    js = client.get("/dashboard/static/dashboard.js")
    assert css.status_code == js.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert "javascript" in js.headers["content-type"]
    assert "sessionStorage" in js.text
    assert client.get("/dashboard/static/../app.py").status_code == 404
    assert client.get("/dashboard/static/missing.js").status_code == 404
    html = client.get("/dashboard").text
    assert "http://" not in html and "https://" not in html


def test_token_is_never_placed_in_urls_or_logs(client):
    js = client.get("/dashboard/static/dashboard.js").text
    assert "sessionStorage" in js
    assert "localStorage" not in js
    assert "location.search" not in js
    assert "console." not in js
    assert re.search(r"Authorization: `Bearer \$\{token\}`", js)


def test_respond_action_requires_the_api_token(seeded, admin_headers):
    client, ids = seeded
    html = get(client, "/dashboard/security")
    # The dashboard hands the delivered audience to the API; undelivered requests get no buttons.
    assert f'data-request-id="{ids["authorization"]}" data-person-id="{ids["person"]}"' in html
    assert f'data-request-id="{ids["undelivered"]}"' not in html
    assert "Not delivered to a person yet" in html
    assert str(ids["expired"]) not in html
    path = f"/authorizations/{ids['authorization']}/respond"
    body = {"decision": "approved", "text": "from dashboard", "person_id": str(ids["person"])}
    assert client.post(path, json=body).status_code == 401
    wrong = {"Authorization": "Bearer wrong"}
    assert client.post(path, json=body, headers=wrong).status_code == 401
    assert client.post(path, json=body, headers=admin_headers).status_code == 202


def test_dashboard_is_read_only(client):
    for path in PAGES:
        assert client.post(path).status_code == 405
        assert client.delete(path).status_code == 405
