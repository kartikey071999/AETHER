"""HTML rendering; every dynamic value goes through ``esc`` and no script or style is inline."""

import json
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from html import escape
from typing import Any

REFRESH_SECONDS = 10
STALE_HEARTBEAT_SECONDS = 120
NAV = (
    ("overview", "/dashboard", "Overview"),
    ("objectives", "/dashboard/objectives", "Objectives"),
    ("tasks", "/dashboard/tasks", "Tasks"),
    ("security", "/dashboard/security", "Security"),
    ("resources", "/dashboard/resources", "Resources"),
    ("providers", "/dashboard/providers", "Providers"),
    ("events", "/dashboard/events", "Events"),
)
_CSS_TOKEN = re.compile(r"[^a-z0-9_-]")


def esc(value: Any) -> str:
    return "&mdash;" if value is None or value == "" else escape(str(value), quote=True)


def css_token(value: Any) -> str:
    return _CSS_TOKEN.sub("-", str(value).lower())


def badge(value: Any) -> str:
    return f'<span class="badge s-{css_token(value)}">{esc(value)}</span>'


def when(value: Any) -> str:
    if not value:
        return esc(None)
    return esc(str(value).replace("T", " ")[:19] + " UTC")


def parse_time(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp


def age(value: Any, now: datetime) -> tuple[str, float | None]:
    stamp = parse_time(value) if value else None
    if stamp is None:
        return "never", None
    seconds = max(0.0, (now - stamp).total_seconds())
    if seconds < 90:
        return f"{int(seconds)}s ago", seconds
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago", seconds
    if seconds < 172_800:
        return f"{int(seconds // 3600)}h ago", seconds
    return f"{int(seconds // 86_400)}d ago", seconds


def number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def human_bytes(value: Any) -> str:
    size = number(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return esc(None)


def compact_json(value: Any) -> str:
    return esc(json.dumps(value, sort_keys=True, default=str)[:400]) if value else esc(None)


def short_id(value: Any) -> str:
    return f'<code title="{esc(value)}">{esc(str(value)[:8])}</code>'


def empty(message: str) -> str:
    return f'<p class="empty">{esc(message)}</p>'


def section(title: str, body: str, *, count: int | None = None) -> str:
    suffix = f' <span class="count">{count}</span>' if count is not None else ""
    return f"<section><h2>{esc(title)}{suffix}</h2>{body}</section>"


def table(headers: Sequence[str], rows: Iterable[Sequence[str]], empty_message: str) -> str:
    rows = list(rows)
    if not rows:
        return empty(empty_message)
    head = "".join(f"<th>{esc(item)}</th>" for item in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def progress(done: float, total: float) -> str:
    return (
        f'<progress max="{max(total, 1):g}" value="{min(done, max(total, 1)):g}"></progress> '
        f"{done:g}/{total:g}"
    )


def objective_link(objective: dict[str, Any]) -> str:
    return f'<a href="/dashboard/objectives/{esc(objective["id"])}">{esc(objective["title"])}</a>'


def layout(title: str, active: str, content: str, *, auth_panel: str = "") -> str:
    links = "".join(
        f'<a href="{href}"{" aria-current=page" if key == active else ""}>{esc(label)}</a>'
        for key, href, label in NAV
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)} &middot; AETHER</title>"
        '<link rel="stylesheet" href="/dashboard/static/dashboard.css"></head>'
        f'<body data-refresh-seconds="{REFRESH_SECONDS}">'
        f"<header><strong>AETHER</strong><nav>{links}</nav>"
        '<span id="api-status" class="badge s-unknown">api: checking</span></header>'
        f'{auth_panel}<main id="content">{content}</main>'
        '<script src="/dashboard/static/dashboard.js" defer></script></body></html>'
    )


def overview_page(data: dict[str, Any], now: datetime) -> str:
    agent_rows = []
    for agent in data["agents"]:
        beat = agent.get("latest_heartbeat")
        text, seconds = age(beat["observed_at"] if beat else None, now)
        status = agent["status"] if beat else "no heartbeat"
        if seconds is not None and seconds > STALE_HEARTBEAT_SECONDS:
            status = "stale"
        agent_rows.append(
            [
                f"<strong>{esc(agent['agent_id'])}</strong>",
                badge(status),
                esc(text),
                short_id(beat["current_task_id"])
                if beat and beat["current_task_id"]
                else esc(None),
                esc(agent["responsibility"]),
            ]
        )
    active = [item for item in data["objectives"] if item["status"] == "active"]
    objective_rows = []
    for item in active:
        stats = item["progress"]
        percent = stats["completion_percent"]
        objective_rows.append(
            [
                objective_link(item),
                badge(item["status"]),
                progress(stats["completed_tasks"], stats["total_tasks"])
                + (f" ({percent:.0f}%)" if percent is not None else " (no tasks)"),
                esc(item["priority"]),
            ]
        )
    counts = "".join(
        f"<li>{badge(state)} <strong>{count}</strong></li>"
        for state, count in data["task_counts"].items()
    )
    error_rows = [
        [
            when(item["occurred_at"]),
            esc(item["agent"]),
            esc(item["error_type"]),
            esc(item["message"]),
            badge("resolved" if item["resolved"] else "open"),
        ]
        for item in data["errors"]
    ]
    return (
        "<h1>Overview</h1>"
        + section(
            "Agents",
            table(
                ["Agent", "Status", "Last heartbeat", "Current task", "Responsibility"],
                agent_rows,
                "No agents are registered yet.",
            ),
            count=len(agent_rows),
        )
        + section(
            "Active objectives",
            table(
                ["Objective", "Status", "Progress", "Priority"],
                objective_rows,
                "No active objectives.",
            ),
            count=len(objective_rows),
        )
        + section("Tasks by state", f'<ul class="counts">{counts}</ul>')
        + section(
            "Recent errors",
            table(
                ["When", "Agent", "Type", "Message", "State"],
                error_rows,
                "No errors have been recorded.",
            ),
        )
    )


def objectives_page(data: dict[str, Any]) -> str:
    rows = []
    for item in data["items"]:
        stats = item["progress"]
        rows.append(
            [
                objective_link(item),
                badge(item["status"]),
                esc(item["priority"]),
                progress(stats["completed_tasks"], stats["total_tasks"]),
                when(item["created_at"]),
            ]
        )
    return "<h1>Objectives</h1>" + section(
        "All objectives",
        table(
            ["Objective", "Status", "Priority", "Progress", "Created"],
            rows,
            "No objectives have been submitted yet.",
        ),
        count=data["total"],
    )


def _dag(tasks: list[dict[str, Any]], dependencies: list[dict[str, Any]]) -> str:
    by_id = {task["id"]: task for task in tasks}
    children: dict[str, list[str]] = {}
    has_parent: set[str] = set()
    for edge in dependencies:
        parent, child = edge["depends_on_task_id"], edge["task_id"]
        if parent in by_id and child in by_id:
            children.setdefault(parent, []).append(child)
            has_parent.add(child)
    shown: set[str] = set()

    def node(task_id: str) -> str:
        task = by_id[task_id]
        label = (
            f"{badge(task['state'])} <strong>{esc(task['title'])}</strong> "
            f'<span class="muted">{esc(task["assigned_agent"])} &middot; '
            f"priority {esc(task['priority'])}</span>"
        )
        if task_id in shown:
            return f'<li>{label} <span class="muted">(listed above)</span></li>'
        shown.add(task_id)
        nested = "".join(node(child) for child in children.get(task_id, []))
        return f"<li>{label}{f'<ul>{nested}</ul>' if nested else ''}</li>"

    roots = [task["id"] for task in tasks if task["id"] not in has_parent]
    # A defensive pass keeps any task that is only reachable through a cycle visible.
    body = "".join(node(root) for root in roots) + "".join(
        node(task["id"]) for task in tasks if task["id"] not in shown
    )
    return f'<ul class="dag">{body}</ul>' if body else empty("This objective has no tasks.")


def objective_page(data: dict[str, Any]) -> str:
    item = data["objective"]
    stats = item["progress"]
    criteria = "".join(f"<li>{esc(entry)}</li>" for entry in item["completion_criteria"])
    head = (
        f"<h1>{esc(item['title'])}</h1>"
        f"<p>{badge(item['status'])} priority {esc(item['priority'])} &middot; "
        f"{progress(stats['completed_tasks'], stats['total_tasks'])}</p>"
        f"<p>{esc(item['description'])}</p>"
        + (f"<h3>Completion criteria</h3><ul>{criteria}</ul>" if criteria else "")
    )
    requirements = table(
        ["Requirement", "Type", "Status", "Priority", "Acceptance criteria"],
        [
            [
                esc(row["title"]),
                esc(row["requirement_type"]),
                badge(row["status"]),
                esc(row["priority"]),
                "<br>".join(esc(entry) for entry in row["acceptance_criteria"]) or esc(None),
            ]
            for row in data["requirements"]
        ],
        "No requirements recorded for this objective.",
    )
    findings = table(
        ["Agent", "Question", "Findings", "Class", "Confidence", "Status"],
        [
            [
                esc(row["agent"]),
                esc(row["question"]),
                esc(row["findings"]),
                badge(row["classification"]),
                f"{number(row['confidence']):.2f}",
                badge(row["status"]),
            ]
            for row in data["findings"]
        ],
        "No research findings recorded for this objective.",
    )
    capability_rows = []
    for row in data["capabilities"]:
        evidence = "<br>".join(
            f"{esc(run['suite'])}: {badge(run['verdict'] or run['status'])} "
            f"{run['passed']} passed, {run['failed']} failed, {run['errors']} errors"
            for run in row["verification_evidence"]
        ) or esc(None)
        capability_rows.append(
            [
                esc(row["name"]),
                badge(row["lifecycle_status"]),
                badge(row["verification_status"]),
                evidence,
            ]
        )
    capabilities = table(
        ["Capability", "Lifecycle", "Verification", "Evidence"],
        capability_rows,
        "No capabilities are linked to this objective.",
    )
    decisions = table(
        ["When", "Decision", "Operation", "Rationale", "Expires"],
        [
            [
                when(row["created_at"]),
                badge(row["decision"]),
                esc(row["operation"]),
                esc(row["rationale"]),
                when(row["expires_at"]),
            ]
            for row in data["decisions"]
        ],
        "No security decisions for this objective's tasks.",
    )
    authorizations = table(
        ["Requested", "Question", "Status", "Responded"],
        [
            [
                when(row["requested_at"]),
                esc(row["question"]),
                badge(row["status"]),
                when(row["responded_at"]),
            ]
            for row in data["authorizations"]
        ],
        "No authorization requests for this objective.",
    )
    final = data["final_message"]
    final_html = (
        f'<p class="muted">{when(final["occurred_at"])} via {esc(final["channel"])}</p>'
        f'<pre class="message">{esc(final["summary"])}</pre>'
        if final
        else empty("MUSE has not sent a message for this objective yet.")
    )
    return (
        '<p><a href="/dashboard/objectives">&larr; All objectives</a></p>'
        + head
        + section("Task graph", _dag(data["tasks"], data["dependencies"]), count=len(data["tasks"]))
        + section("Requirements", requirements, count=len(data["requirements"]))
        + section("Research findings", findings, count=len(data["findings"]))
        + section("Capabilities and verification", capabilities, count=len(data["capabilities"]))
        + section("Security decisions", decisions, count=len(data["decisions"]))
        + section("Authorization requests", authorizations, count=len(data["authorizations"]))
        + section("Final MUSE message", final_html)
    )


def tasks_page(
    data: dict[str, Any], states: Sequence[str], state: str | None, agent: str | None
) -> str:
    options = "".join(
        f'<option value="{esc(item)}"{" selected" if item == state else ""}>{esc(item)}</option>'
        for item in states
    )
    form = (
        '<form method="get" class="filters"><label>State <select name="state">'
        f'<option value="">all</option>{options}</select></label>'
        f'<label>Agent <input name="agent" maxlength="24" value="{escape(agent or "")}"></label>'
        '<button type="submit">Filter</button></form>'
    )
    rows = [
        [
            f'<a href="/dashboard/objectives/{esc(item["objective_id"])}">{esc(item["title"])}</a>',
            badge(item["state"]),
            esc(item["assigned_agent"]),
            esc(item["priority"]),
            f"{esc(item['attempt'])}/{esc(item['max_attempts'])}",
            esc(item["last_error"]),
            when(item["updated_at"]),
        ]
        for item in data["items"]
    ]
    return (
        "<h1>Tasks</h1>"
        + form
        + section(
            "Tasks",
            table(
                ["Task", "State", "Agent", "Priority", "Attempts", "Last error", "Updated"],
                rows,
                "No tasks match.",
            ),
            count=data["total"],
        )
    )


def security_page(data: dict[str, Any]) -> str:
    pending = data["pending"]["items"]
    pending_rows = []
    for item in pending:
        request_id = esc(item["id"])
        person = item.get("audience_person_id")
        if person:
            attrs = f'data-request-id="{request_id}" data-person-id="{esc(person)}"'
            actions = (
                f'<button type="button" data-respond="approved" {attrs}>Approve</button> '
                f'<button type="button" class="deny" data-respond="denied" {attrs}>Deny</button>'
            )
        else:
            actions = '<span class="muted">Not delivered to a person yet</span>'
        pending_rows.append(
            [
                when(item["requested_at"]),
                esc(item["question"]),
                when(item["expires_at"]),
                actions,
            ]
        )
    decision_rows = [
        [
            when(item["created_at"]),
            badge(item["decision"]),
            esc(item["operation"]),
            esc(item["rationale"]),
            compact_json(item["scope"]),
            when(item["expires_at"]),
        ]
        for item in data["decisions"]["items"]
    ]
    return (
        "<h1>Security</h1>"
        + section(
            "Pending approvals",
            table(
                ["Requested", "Question", "Expires", "Respond"],
                pending_rows,
                "Nothing is waiting for approval.",
            ),
            count=len(pending_rows),
        )
        + section(
            "Security decisions",
            table(
                ["When", "Decision", "Operation", "Rationale", "Scope", "Expires"],
                decision_rows,
                "No security decisions have been recorded.",
            ),
            count=data["decisions"]["total"],
        )
    )


AUTH_PANEL = (
    '<section id="auth-panel"><h2>Approval authentication</h2>'
    '<p class="muted">The API token is kept in this browser tab only (sessionStorage) and is sent'
    " solely as a request header when you approve or deny.</p>"
    '<label>API token <input id="api-token" type="password" autocomplete="off"'
    ' spellcheck="false"></label> '
    '<button type="button" id="forget-token">Forget token</button>'
    '<label>Response note <input id="respond-note" maxlength="500"'
    ' placeholder="Optional note recorded with the decision"></label>'
    '<div id="respond-status" role="status"></div></section>'
)


def sparkline(
    values: Sequence[float], *, low: float | None = None, high: float | None = None
) -> str:
    if not values:
        return empty("No measurements.")
    low = min(values) if low is None else low
    high = max(values) if high is None else high
    span = (high - low) or 1.0
    width, height = 300, 50
    points = [
        (
            width * index / max(len(values) - 1, 1),
            height - 2 - (height - 4) * (value - low) / span,
        )
        for index, value in enumerate(values)
    ]
    if len(points) == 1:
        points = [(0.0, points[0][1]), (float(width), points[0][1])]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" role="img" aria-label="trend">'
        f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="{path}"/></svg>'
    )


def resources_page(data: dict[str, Any]) -> str:
    items = data["items"]
    if not items:
        return "<h1>Resources</h1>" + empty("No resource measurements have been recorded yet.")
    latest = items[0]
    chronological = list(reversed(items))
    cards = [
        (
            "CPU",
            f"{number(latest['cpu_percent']):.1f}%",
            [number(i["cpu_percent"]) for i in chronological],
            0.0,
            100.0,
        ),
        (
            "Available RAM",
            human_bytes(latest["available_ram_bytes"]),
            [number(i["available_ram_bytes"]) for i in chronological],
            None,
            None,
        ),
        (
            "Free disk",
            human_bytes(latest["free_disk_bytes"]),
            [number(i["free_disk_bytes"]) for i in chronological],
            None,
            None,
        ),
    ]
    body = "".join(
        f'<div class="card"><h3>{esc(name)}</h3><p class="big">{esc(value)}</p>'
        f"{sparkline(series, low=low, high=high)}</div>"
        for name, value, series, low, high in cards
    )
    rows = [
        [
            when(item["observed_at"]),
            f"{number(item['cpu_percent']):.1f}%",
            human_bytes(item["available_ram_bytes"]),
            human_bytes(item["free_disk_bytes"]),
        ]
        for item in items[:20]
    ]
    return (
        "<h1>Resources</h1>"
        f'<p class="muted">Latest measurement {when(latest["observed_at"])}; '
        f"trend covers the last {len(items)} of {data['total']} measurements.</p>"
        f'<div class="cards">{body}</div>'
        + section(
            "Recent measurements",
            table(["Observed", "CPU", "Available RAM", "Free disk"], rows, ""),
        )
    )


def _budget_bar(used: Any, limit: Any, *, money: bool = False) -> str:
    used_text = f"${number(used):.4f}" if money else f"{number(used):g}"
    if limit is None:
        return f'{esc(used_text)} <span class="muted">(no limit)</span>'
    limit_text = f"${number(limit):.2f}" if money else f"{number(limit):g}"
    return f"{progress_value(number(used), number(limit))} {esc(used_text)} / {esc(limit_text)}"


def progress_value(used: float, limit: float) -> str:
    cap = max(limit, 1e-9)
    return f'<progress max="{cap:g}" value="{min(used, cap):g}"></progress>'


def providers_page(data: dict[str, Any]) -> str:
    rows = []
    for item in data["items"]:
        today, budget = item["today"], item["budget"] or {}
        rows.append(
            [
                f"<strong>{esc(item['provider'])}</strong>",
                esc(item["date"]),
                _budget_bar(today["requests"], budget.get("max_requests_per_day")),
                _budget_bar(today["tokens"], budget.get("max_tokens_per_day")),
                _budget_bar(today["cost_usd"], budget.get("max_cost_usd_per_day"), money=True),
                esc(today["active"]),
            ]
        )
        for model in item["model_budgets"]:
            usage, policy = model["today"], model["budget"]
            rows.append(
                [
                    f"&nbsp;&nbsp;{esc(item['provider'])}/{esc(model['model'])}",
                    esc(item["date"]),
                    _budget_bar(usage["requests"], policy.get("max_requests_per_day")),
                    _budget_bar(usage["tokens"], policy.get("max_tokens_per_day")),
                    _budget_bar(usage["cost_usd"], policy.get("max_cost_usd_per_day"), money=True),
                    esc(usage["active"]),
                ]
            )
    return "<h1>Providers</h1>" + section(
        "Today's usage vs budgets (UTC day)",
        table(
            ["Provider", "Date", "Requests", "Tokens", "Cost", "In flight"],
            rows,
            "No provider usage or budgets are configured.",
        ),
    )


def events_page(data: dict[str, Any], event_type: str | None) -> str:
    form = (
        '<form method="get" class="filters"><label>Event type '
        f'<input name="type" maxlength="64" value="{escape(event_type or "")}"></label>'
        '<button type="submit">Filter</button></form>'
    )
    rows = [
        [
            when(item["created_at"]),
            esc(item["event_type"]),
            esc(item["category"]),
            esc(item["actor"]),
            f"{esc(item['aggregate_type'])} {short_id(item['aggregate_id'])}",
            compact_json(item["payload"]),
        ]
        for item in data["items"]
    ]
    return (
        "<h1>Events</h1>"
        + form
        + section(
            "Latest events",
            table(
                ["When", "Type", "Category", "Actor", "Subject", "Payload"],
                rows,
                "No events match.",
            ),
            count=data["total"],
        )
    )
