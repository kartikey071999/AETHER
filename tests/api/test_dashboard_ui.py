"""Interactive dashboard: chrome, versioned assets, JSON views and the browser helpers."""

import json
import re
from pathlib import Path

import pytest

from aether.api.dashboard import assets
from aether.storage.repositories import ObjectiveRepository, TaskRepository

STATIC = Path(assets.__file__).parent / "static"
SCRIPTS = ["theme.js", "core.js", "dashboard.js"]
VIEWS = {
    "/dashboard/data/overview": "overview",
    "/dashboard/data/objectives": "objectives",
    "/dashboard/data/tasks": "tasks",
    "/dashboard/data/security": "security",
    "/dashboard/data/resources": "resources",
    "/dashboard/data/providers": "providers",
    "/dashboard/data/events": "events",
}
PAGES = {
    "/dashboard": "overview",
    "/dashboard/objectives": "objectives",
    "/dashboard/tasks": "tasks",
    "/dashboard/security": "security",
    "/dashboard/resources": "resources",
    "/dashboard/providers": "providers",
    "/dashboard/events": "events",
}
SCRIPT = "<script>alert('xss')</script>"


def secrets(api_token, provider_token):
    return (api_token, provider_token, "fake-agent-password", "fake-error-password")


@pytest.mark.parametrize(("path", "view"), PAGES.items())
def test_layout_has_console_chrome(client, path, view):
    html = client.get(path).text
    for marker in (
        'id="theme-toggle"',
        'id="search"',
        'id="live-toggle"',
        'id="shortcuts-open"',
        '<dialog id="shortcuts"',
        '<aside id="drawer"',
        'id="toasts"',
        'class="skip" href="#content"',
        f'data-page="{view}"',
        f'data-source="/dashboard/data/{view}"',
    ):
        assert marker in html
    assert "aria-current=page" in html
    # Every script and stylesheet is a same-origin, content-versioned URL.
    for name in ("dashboard.css", *SCRIPTS):
        assert f'"/dashboard/static/{name}?v={assets.asset(name).version}"' in html
    assert re.findall(r'<script src="[^"]+"( defer)?>', html) == ["", " defer", " defer"]


def test_filter_pages_carry_their_query_into_the_data_source(client):
    html = client.get("/dashboard/tasks", params={"state": "running", "agent": "FORGE"}).text
    assert 'data-source="/dashboard/data/tasks?state=running&amp;agent=FORGE"' in html
    html = client.get("/dashboard/events", params={"type": "task.created"}).text
    assert 'data-source="/dashboard/data/events?type=task.created"' in html


@pytest.mark.parametrize("name", ["dashboard.css", *SCRIPTS])
def test_versioned_assets_are_cached_immutably(client, name):
    item = assets.asset(name)
    fresh = client.get(f"/dashboard/static/{name}", params={"v": item.version})
    assert fresh.status_code == 200
    assert fresh.headers["cache-control"] == assets.IMMUTABLE
    assert fresh.headers["etag"] == item.etag
    assert fresh.headers["x-content-type-options"] == "nosniff"
    kind = "text/css" if name.endswith(".css") else "javascript"
    assert kind in fresh.headers["content-type"]
    assert fresh.content == (STATIC / name).read_bytes()
    stale = client.get(f"/dashboard/static/{name}", params={"v": "0" * 16})
    assert stale.headers["cache-control"] == assets.REVALIDATE
    bare = client.get(f"/dashboard/static/{name}")
    assert bare.headers["cache-control"] == assets.REVALIDATE
    again = client.get(f"/dashboard/static/{name}", headers={"If-None-Match": item.etag})
    assert again.status_code == 304
    assert not again.content


def test_unknown_assets_are_not_served(client):
    for name in ("missing.js", "..%2Fapp.py", "dashboard.CSS", "__init__.py"):
        assert client.get(f"/dashboard/static/{name}").status_code == 404
    with pytest.raises(KeyError):
        assets.url("missing.js")


@pytest.mark.parametrize(("path", "view"), VIEWS.items())
def test_json_views_are_sanitized_and_uncached(seeded, path, view, api_token, provider_token):
    client, _ = seeded
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    for secret in secrets(api_token, provider_token):
        assert secret not in response.text
    body = response.json()
    assert body["view"] == view
    assert body["generated_at"]
    assert body["stale_heartbeat_seconds"] > 0


def test_json_views_carry_real_rows(seeded):
    client, ids = seeded
    overview = client.get("/dashboard/data/overview").json()
    assert {agent["agent_id"] for agent in overview["agents"]} >= {"FORGE", "SCOUT"}
    assert overview["task_counts"]["running"] == 1
    assert overview["task_counts"]["cancelled"] == 0
    detail = client.get(f"/dashboard/data/objectives/{ids['objective']}").json()
    assert detail["view"] == "objective"
    assert detail["objective"]["title"] == "Real stored objective"
    assert {task["title"] for task in detail["tasks"]} >= {"Actual ready task", "Live worker task"}
    assert detail["dependencies"][0]["depends_on_task_id"] == str(ids["dependency"])
    security = client.get("/dashboard/data/security").json()
    pending = {item["id"]: item for item in security["pending"]["items"]}
    assert pending[str(ids["authorization"])]["audience_person_id"] == str(ids["person"])
    providers = client.get("/dashboard/data/providers").json()
    assert providers["thresholds"] == {"warn": 0.7, "critical": 0.9}
    assert any(item["provider"] == "groq" for item in providers["items"])


def test_json_task_and_event_filters(seeded):
    client, _ = seeded
    running = client.get("/dashboard/data/tasks", params={"state": "running"}).json()
    assert [item["title"] for item in running["items"]] == ["Live worker task"]
    assert running["filters"] == {"state": "running", "agent": None}
    assert "waiting_for_approval" in running["states"]
    ledger = client.get("/dashboard/data/tasks", params={"agent": "LEDGER", "state": ""}).json()
    assert [item["title"] for item in ledger["items"]] == ["Completed prerequisite"]
    assert client.get("/dashboard/data/tasks", params={"state": "bogus"}).status_code == 422
    events = client.get("/dashboard/data/events", params={"type": "task.created"}).json()
    assert events["items"]
    assert {item["event_type"] for item in events["items"]} == {"task.created"}
    assert events["filters"] == {"type": "task.created"}


def test_task_detail_view(seeded, api_token, provider_token):
    client, ids = seeded
    response = client.get(f"/dashboard/data/tasks/{ids['task']}")
    assert response.status_code == 200
    for secret in secrets(api_token, provider_token):
        assert secret not in response.text
    body = response.json()
    assert body["view"] == "task"
    assert body["task"]["title"] == "Actual ready task"
    assert body["objective"] == {"id": str(ids["objective"]), "title": "Real stored objective"}
    assert body["task"]["dependencies"]["items"][0]["depends_on_task_id"] == str(ids["dependency"])
    assert body["task"]["executions"]["items"][0]["operation"] == "offline build"
    assert [run["suite"] for run in body["tests"]] == ["Actual offline suite"]
    assert [row["operation"] for row in body["decisions"]] == ["file_read"]
    assert body["task"]["events"]["total"] > 0
    assert client.get(f"/dashboard/data/tasks/{ids['unknown']}").status_code == 404
    assert client.get("/dashboard/data/tasks/not-a-uuid").status_code == 422
    assert client.get(f"/dashboard/data/objectives/{ids['unknown']}").status_code == 404


def test_json_views_keep_hostile_text_as_data(seeded):
    client, _ = seeded

    async def create():
        sessions = client.app.state.services.database.sessions
        objective = await ObjectiveRepository(sessions).create(title=SCRIPT)
        return await TaskRepository(sessions).create(
            objective_id=objective,
            title=f"<img src=x onerror=alert(1)>{SCRIPT}",
            assigned_agent="FORGE",
        )

    task = client.portal.call(create)
    for path in (
        "/dashboard/data/objectives",
        "/dashboard/data/tasks",
        f"/dashboard/data/tasks/{task}",
    ):
        response = client.get(path)
        assert response.headers["content-type"].startswith("application/json")
        # Raw text is only ever a JSON string value; the browser inserts it as a text node.
        assert SCRIPT in json.dumps(response.json())
    for path in ("/dashboard/data/tasks", "/dashboard/data/objectives"):
        assert client.post(path).status_code == 405


def test_resource_cells_carry_sort_keys(seeded):
    client, _ = seeded
    html = client.get("/dashboard/resources").text
    assert '<data value="12.5">12.5%</data>' in html
    assert '<data value="123456">120.6 KiB</data>' in html


def test_scripts_never_parse_strings_as_markup():
    forbidden = re.compile(
        r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|"
        r"setTimeout\(\s*['\"`]|setInterval\(\s*['\"`]|\.on\w+\s*=|javascript:"
    )
    for name in SCRIPTS:
        source = (STATIC / name).read_text(encoding="utf-8")
        assert not forbidden.search(source), name
        assert "console." not in source
        assert "location.search" not in source


def test_only_the_theme_uses_local_storage(api_token, provider_token):
    for name in SCRIPTS:
        source = (STATIC / name).read_text(encoding="utf-8")
        for secret in secrets(api_token, provider_token):
            assert secret not in source
        if name == "theme.js":
            assert re.findall(r"localStorage\.(\w+)\((\w+)", source) == [
                ("getItem", "KEY"),
                ("setItem", "KEY"),
            ]
            assert 'const KEY = "aether.theme";' in source
        else:
            assert "localStorage" not in source
    dashboard = (STATIC / "dashboard.js").read_text(encoding="utf-8")
    assert 'const TOKEN_KEY = "aether.apiToken";' in dashboard
    assert dashboard.count("TOKEN_KEY") == dashboard.count("sessionStorage") + 1


def test_assets_stay_small_and_plain():
    total = sum((STATIC / name).stat().st_size for name in ("dashboard.css", *SCRIPTS))
    assert total < 150_000
    css = (STATIC / "dashboard.css").read_text(encoding="utf-8")
    for banned in ("gradient(", "backdrop-filter", "@import", "url(http", "font-face"):
        assert banned not in css
    for name in SCRIPTS:
        source = (STATIC / name).read_text(encoding="utf-8")
        urls = set(re.findall(r"https?://[^\s\"'`]+", source))
        assert urls <= {"http://www.w3.org/2000/svg"}, name


# --- core.js in a real JavaScript engine -------------------------------------------------

FAKE_DOM = """
function Node(tag, ns) {
  this.tagName = tag; this.namespaceURI = ns || null; this.nodeType = 1;
  this.attributes = {}; this.childNodes = [];
}
Node.prototype.setAttribute = function (k, v) { this.attributes[k] = String(v); };
Node.prototype.appendChild = function (c) { this.childNodes.push(c); return c; };
Object.defineProperty(Node.prototype, "textContent", { get() {
  return this.childNodes.map((c) => c.nodeType === 3 ? c.data : c.textContent).join("");
} });
var window = globalThis;
var document = {
  createElement: (t) => new Node(t),
  createElementNS: (ns, t) => new Node(t, ns),
  createTextNode: (d) => ({ nodeType: 3, data: d }),
};
"""


@pytest.fixture(scope="module")
def v8():
    racer = pytest.importorskip("py_mini_racer")
    context = racer.MiniRacer()
    context.eval(FAKE_DOM)
    context.eval((STATIC / "core.js").read_text(encoding="utf-8"))
    return lambda source: json.loads(context.eval(f"JSON.stringify((() => {{ {source} }})())"))


def test_core_builds_text_nodes_only(v8):
    result = v8(
        """
        const node = Aether.h("p", { class: "x", title: "<b>" }, "<script>x</script>", 3, null,
          [Aether.h("b", null, "y")]);
        return { kinds: node.childNodes.map((c) => c.nodeType), text: node.textContent,
                 attrs: node.attributes };
        """
    )
    assert result == {
        "kinds": [3, 3, 1],
        "text": "<script>x</script>3y",
        "attrs": {"class": "x", "title": "<b>"},
    }


@pytest.mark.parametrize(
    "attrs",
    [
        '{ onclick: "x" }',
        '{ ONLOAD: "x" }',
        '{ href: "javascript:alert(1)" }',
        '{ href: "//evil.example" }',
        '{ href: "https://evil.example" }',
        '{ src: "data:text/html,x" }',
        '{ action: " javascript:x" }',
    ],
)
def test_core_refuses_unsafe_attributes(v8, attrs):
    outcome = v8(
        f'try {{ Aether.h("a", {attrs}); return "built"; }} catch (e) {{ return e.message; }}'
    )
    assert outcome.startswith("Refusing unsafe attribute")


def test_core_allows_same_origin_links(v8):
    assert v8('return Aether.h("a", { href: "/dashboard/tasks" }).attributes.href') == (
        "/dashboard/tasks"
    )
    assert v8('return Aether.h("a", { href: "#task=1" }).attributes.href') == "#task=1"


def test_core_lays_out_dependencies_left_to_right(v8):
    layout = v8(
        """
        const tasks = ["a", "b", "c", "d"].map((id) => ({ id }));
        const deps = [
          { task_id: "b", depends_on_task_id: "a" },
          { task_id: "c", depends_on_task_id: "b" },
          { task_id: "d", depends_on_task_id: "a" },
          { task_id: "x", depends_on_task_id: "a" },
        ];
        return Aether.layoutGraph(tasks, deps);
        """
    )
    layers = {node["id"]: node["layer"] for node in layout["nodes"]}
    assert layers == {"a": 0, "b": 1, "d": 1, "c": 2}
    assert layout["layers"] == 3
    assert {(edge["from"], edge["to"]) for edge in layout["edges"]} == {
        ("a", "b"),
        ("b", "c"),
        ("a", "d"),
    }
    xs = {node["id"]: node["x"] for node in layout["nodes"]}
    assert xs["a"] < xs["b"] < xs["c"]
    positions = {(node["x"], node["y"]) for node in layout["nodes"]}
    assert len(positions) == 4
    assert layout["width"] > xs["c"] and layout["height"] > 0


def test_core_layout_survives_cycles(v8):
    layout = v8(
        """
        const tasks = [{ id: "a" }, { id: "b" }, { id: "c" }];
        const deps = [
          { task_id: "b", depends_on_task_id: "a" },
          { task_id: "a", depends_on_task_id: "b" },
          { task_id: "c", depends_on_task_id: "c" },
        ];
        return Aether.layoutGraph(tasks, deps);
        """
    )
    assert sorted(node["id"] for node in layout["nodes"]) == ["a", "b", "c"]
    assert all(edge["d"].startswith("M") for edge in layout["edges"])
    assert v8("return Aether.layoutGraph([], []).nodes.length") == 0


def test_core_formatting_matches_the_server(v8):
    payload = {"b": [1, None, True], "a": {"z": "\u00e9<", "y": 1.5}}
    assert v8(f"return Aether.pyJson({json.dumps(payload)})") == json.dumps(payload, sort_keys=True)
    assert v8("return [5, 125, 7200, 259200, -120].map(Aether.relative)") == [
        "5s ago",
        "2m ago",
        "2h ago",
        "3d ago",
        "in 2m",
    ]
    assert v8('return Aether.parseTime("2024-01-02T03:04:05.123456")') == 1704164645123
    assert v8('return Aether.parseTime("2024-01-02T03:04:05+00:00")') == 1704164645000
    assert v8('return Aether.parseTime("nonsense")') is None
    assert v8("return [0.5, 0.75, 0.95].map((r) => Aether.tone(r))") == ["ok", "warn", "crit"]
    assert v8("return [123456, 2000000].map(Aether.humanBytes)") == ["120.6 KiB", "1.9 MiB"]
    assert v8("return Aether.count(2000000)") == "2,000,000"


def test_core_sorting_and_filtering(v8):
    assert v8('return ["10", "9", "1,000", "$0.5"].sort(Aether.compareKeys)') == [
        "$0.5",
        "9",
        "10",
        "1,000",
    ]
    assert v8('return ["task 10", "task 9"].sort(Aether.compareKeys)') == ["task 9", "task 10"]
    assert v8('return Aether.matches("Live worker task FORGE", "forge  live")') is True
    assert v8('return Aether.matches("Live worker task", "ledger")') is False
