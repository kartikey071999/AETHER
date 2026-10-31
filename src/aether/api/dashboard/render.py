"""HTML rendering; every dynamic value goes through ``esc`` and no script or style is inline.

The server-rendered pages are complete without JavaScript. ``dashboard.js`` redraws the same
markup from the ``/dashboard/data`` JSON views, so the class names here are a shared contract.
"""

import json
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from html import escape
from typing import Any

from aether.api.dashboard import assets

REFRESH_SECONDS = 10
STALE_HEARTBEAT_SECONDS = 120
WARN_RATIO = 0.7
CRITICAL_RATIO = 0.9
NAV = (
    ("overview", "/dashboard", "Overview", "g h"),
    ("objectives", "/dashboard/objectives", "Objectives", "g o"),
    ("tasks", "/dashboard/tasks", "Tasks", "g t"),
    ("security", "/dashboard/security", "Security", "g s"),
    ("resources", "/dashboard/resources", "Resources", "g r"),
    ("providers", "/dashboard/providers", "Providers", "g p"),
    ("events", "/dashboard/events", "Events", "g e"),
)
SHORTCUTS = (
    ("/", "Focus the row filter"),
    ("g h", "Go to overview"),
    ("g o", "Go to objectives"),
    ("g t", "Go to tasks"),
    ("g s", "Go to security"),
    ("g r", "Go to resources"),
    ("g p", "Go to providers"),
    ("g e", "Go to events"),
    ("j / k", "Move between table rows"),
    ("Enter", "Open the selected task"),
    ("r", "Refresh now"),
    ("t", "Toggle light / dark theme"),
    ("Esc", "Close panel, dialog or filter"),
    ("?", "Show this help"),
)
ICONS = {
    "overview": "M2.5 2.5h4.5v4.5H2.5zM9 2.5h4.5v4.5H9zM2.5 9h4.5v4.5H2.5zM9 9h4.5v4.5H9z",
    "objectives": "M8 1.5a6.5 6.5 0 1 0 0 13a6.5 6.5 0 1 0 0-13zM8 5a3 3 0 1 0 0 6a3 3 0 1 0 0-6z",
    "tasks": "M6 3.5h7.5M6 8h7.5M6 12.5h7.5M2.5 3.5h1M2.5 8h1M2.5 12.5h1",
    "security": "M8 1.5l5.5 2v4c0 3.3-2.3 5.9-5.5 7c-3.2-1.1-5.5-3.7-5.5-7v-4z",
    "resources": "M1.5 8.5h3l2-5l3 9l2-4h3",
    "providers": "M2.5 12a5.5 5.5 0 1 1 11 0M8 12l2.8-3.6",
    "events": "M8 1.5a6.5 6.5 0 1 0 0 13a6.5 6.5 0 1 0 0-13zM8 4.5V8l2.5 1.5",
    "sun": "M8 5.2a2.8 2.8 0 1 0 0 5.6a2.8 2.8 0 1 0 0-5.6zM8 1v1.5M8 13.5V15M1 8h1.5M13.5 8H15"
    "M3 3l1.1 1.1M11.9 11.9L13 13M3 13l1.1-1.1M11.9 4.1L13 3",
    "moon": "M13.5 9.6A5.6 5.6 0 0 1 6.4 2.5a5.6 5.6 0 1 0 7.1 7.1z",
    "pause": "M5.5 3.5v9M10.5 3.5v9",
    "play": "M5 3.2l8 4.8l-8 4.8z",
    "help": "M8 1.5a6.5 6.5 0 1 0 0 13a6.5 6.5 0 1 0 0-13zM6.2 6.3a1.9 1.9 0 1 1 2.7 1.7"
    "c-.6.3-.9.7-.9 1.3v.3M8 11.2v.3",
    "search": "M7 2.5a4.5 4.5 0 1 0 0 9a4.5 4.5 0 1 0 0-9zM10.3 10.3l3.2 3.2",
    "close": "M4 4l8 8M12 4l-8 8",
    "mark": "M8 1.8l6.2 11.4H1.8zM8 6.6l2.6 4.8H5.4z",
}
_CSS_TOKEN = re.compile(r"[^a-z0-9_-]")
Column = str | tuple[str, str]


def esc(value: Any) -> str:
    return "&mdash;" if value is None or value == "" else escape(str(value), quote=True)


def css_token(value: Any) -> str:
    return _CSS_TOKEN.sub("-", str(value).lower())


def icon(name: str) -> str:
    return (
        '<svg class="icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">'
        f'<path d="{ICONS[name]}"/></svg>'
    )


def badge(value: Any) -> str:
    return f'<span class="badge s-{css_token(value)}">{esc(value)}</span>'


def parse_time(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp


def relative(seconds: float) -> str:
    """Compact relative time; negative seconds lie in the future."""
    future = seconds < -0.5
    seconds = abs(seconds)
    if seconds < 90:
        text = f"{int(seconds)}s"
    elif seconds < 5400:
        text = f"{int(seconds // 60)}m"
    elif seconds < 172_800:
        text = f"{int(seconds // 3600)}h"
    else:
        text = f"{int(seconds // 86_400)}d"
    return f"in {text}" if future else f"{text} ago"


def age(value: Any, now: datetime) -> tuple[str, float | None]:
    stamp = parse_time(value) if value else None
    if stamp is None:
        return "never", None
    seconds = max(0.0, (now - stamp).total_seconds())
    return relative(seconds), seconds


def when(value: Any, now: datetime) -> str:
    """Relative time as text with the exact UTC time on hover; JS keeps the text current."""
    moment = parse_time(value) if value else None
    if moment is None:
        return esc(None)
    moment = moment.astimezone(UTC)
    absolute = moment.strftime("%Y-%m-%d %H:%M:%S UTC")
    text = relative((now - moment).total_seconds())
    return f'<time datetime="{esc(moment.isoformat())}" title="{absolute}">{esc(text)}</time>'


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
    if not value:
        return esc(None)
    text = esc(json.dumps(value, sort_keys=True, default=str)[:400])
    return f'<code class="payload" title="{text}">{text}</code>'


def count(value: Any) -> str:
    amount = number(value)
    return f"{amount:,.0f}" if amount.is_integer() else f"{amount:,.2f}"


def keyed(value: float, html: str) -> str:
    """Wrap formatted ``html`` with its machine-readable sort key."""
    return f'<data value="{value:.15g}">{html}</data>'


def short_id(value: Any) -> str:
    if not value:
        return esc(None)
    return f'<code class="id" title="{esc(value)}" data-copy="{esc(value)}">{esc(str(value)[:8])}</code>'


def empty(message: str, hint: str = "") -> str:
    extra = f'<p class="empty-hint">{esc(hint)}</p>' if hint else ""
    return f'<div class="empty"><p class="empty-title">{esc(message)}</p>{extra}</div>'


def section(
    title: str, body: str, *, count: int | None = None, key: str = "", note: str = ""
) -> str:
    ident = f' id="{esc(key)}"' if key else ""
    suffix = f'<span class="count">{count}</span>' if count is not None else ""
    aside = f'<span class="panel-note">{esc(note)}</span>' if note else ""
    return (
        f'<section class="panel"{ident}><div class="panel-head"><h2>{esc(title)}</h2>'
        f"{suffix}{aside}</div>{body}</section>"
    )


def page_head(title: str, sub_html: str = "") -> str:
    """``sub_html`` must already be escaped markup."""
    extra = f'<p class="page-sub">{sub_html}</p>' if sub_html else ""
    return f'<div class="page-head"><h1>{esc(title)}</h1>{extra}</div>'


def table(
    columns: Sequence[Column],
    rows: Iterable[Sequence[str]],
    empty_message: str,
    *,
    hint: str = "",
    key: str = "",
    row_attrs: Sequence[str] | None = None,
) -> str:
    rows = list(rows)
    if not rows:
        return empty(empty_message, hint)
    names = [item if isinstance(item, str) else item[0] for item in columns]
    kinds = ["" if isinstance(item, str) else f' class="{item[1]}"' for item in columns]
    head = "".join(
        f'<th scope="col"{kind}>{esc(name)}</th>' for name, kind in zip(names, kinds, strict=True)
    )
    body = "".join(
        f"<tr{(' ' + row_attrs[index]) if row_attrs and row_attrs[index] else ''}>"
        + "".join(f"<td{kind}>{cell}</td>" for cell, kind in zip(row, kinds, strict=True))
        + "</tr>"
        for index, row in enumerate(rows)
    )
    table_key = f' data-table="{esc(key)}"' if key else ""
    return (
        f'<div class="table-wrap"><table class="data"{table_key}>'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def tone(ratio: float) -> str:
    if ratio >= CRITICAL_RATIO:
        return "crit"
    if ratio >= WARN_RATIO:
        return "warn"
    return "ok"


def bar(ratio: float, label: str, *, kind: str = "progress", marks: bool = False) -> str:
    width = max(0.0, min(ratio, 1.0)) * 100
    lines = (
        "".join(
            f'<line class="bar-mark" x1="{mark * 100:g}" x2="{mark * 100:g}" y1="0" y2="8"/>'
            for mark in (WARN_RATIO, CRITICAL_RATIO)
        )
        if marks
        else ""
    )
    return (
        f'<svg class="bar t-{kind}" viewBox="0 0 100 8" preserveAspectRatio="none" role="img" '
        f'aria-label="{esc(label)}"><rect class="bar-track" width="100" height="8"/>'
        f'<rect class="bar-fill" width="{width:.1f}" height="8"/>{lines}</svg>'
    )


def progress(done: float, total: float) -> str:
    ratio = done / total if total else 0.0
    return (
        f'<span class="progress">{bar(ratio, f"{done:g} of {total:g} tasks completed")}'
        f'<span class="num">{done:g}/{total:g}</span></span>'
    )


def objective_link(objective: dict[str, Any]) -> str:
    return f'<a href="/dashboard/objectives/{esc(objective["id"])}">{esc(objective["title"])}</a>'


def task_attr(task_id: Any) -> str:
    return f'data-task-id="{esc(task_id)}"' if task_id else ""


def _nav(active: str) -> str:
    return "".join(
        f'<a href="{href}"{" aria-current=page" if key == active else ""}>'
        f'{icon(key)}<span class="nav-label">{esc(label)}</span>'
        f'<kbd class="nav-key">{esc(keys)}</kbd></a>'
        for key, href, label, keys in NAV
    )


def _shortcuts() -> str:
    rows = "".join(
        f"<div><dt>{''.join(f'<kbd>{esc(part)}</kbd>' for part in keys.split())}</dt>"
        f"<dd>{esc(text)}</dd></div>"
        for keys, text in SHORTCUTS
    )
    return (
        '<dialog id="shortcuts" class="dialog" aria-labelledby="shortcuts-title">'
        '<div class="dialog-head"><h2 id="shortcuts-title">Keyboard shortcuts</h2>'
        '<button type="button" class="icon-btn" data-close-dialog aria-label="Close shortcuts">'
        f'{icon("close")}</button></div><dl class="keys">{rows}</dl></dialog>'
    )


def layout(title: str, active: str, content: str, *, auth_panel: str = "", source: str = "") -> str:
    label = next((name for key, _, name, _ in NAV if key == active), title)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="dark light">'
        f"<title>{esc(title)} &middot; AETHER</title>"
        f'<link rel="stylesheet" href="{assets.url("dashboard.css")}">'
        f'<script src="{assets.url("theme.js")}"></script></head>'
        f'<body data-refresh-seconds="{REFRESH_SECONDS}" data-page="{esc(active)}"'
        f' data-source="{esc(source)}" data-stale-seconds="{STALE_HEARTBEAT_SECONDS}">'
        '<a class="skip" href="#content">Skip to content</a><div class="shell">'
        '<aside class="sidebar"><a class="brand" href="/dashboard">'
        f'<svg class="mark" viewBox="0 0 16 16" aria-hidden="true"><path d="{ICONS["mark"]}"/>'
        "</svg><span>AETHER</span></a>"
        f'<nav class="nav" aria-label="Sections">{_nav(active)}</nav>'
        '<div class="sidebar-foot">'
        '<span id="api-status" class="badge s-unknown">api: checking</span>'
        '<span class="foot-actions">'
        '<button type="button" id="theme-toggle" class="icon-btn js-only"'
        ' aria-label="Toggle colour theme" title="Toggle theme (t)">'
        f'<span class="when-dark">{icon("sun")}</span><span class="when-light">{icon("moon")}'
        "</span></button>"
        '<button type="button" id="shortcuts-open" class="icon-btn js-only"'
        f' aria-label="Keyboard shortcuts" title="Keyboard shortcuts (?)">{icon("help")}</button>'
        "</span></div></aside>"
        '<div class="main"><div class="topbar">'
        f'<span class="crumb">{esc(label)}</span>'
        '<label class="search js-only"><span class="visually-hidden">Filter rows</span>'
        f'{icon("search")}<input id="search" type="search" autocomplete="off" spellcheck="false"'
        ' placeholder="Filter rows on this page"><kbd>/</kbd></label>'
        '<span id="live" class="live js-only" data-state="idle" role="status">'
        '<span class="live-dot" aria-hidden="true"></span><span id="live-text">Live</span></span>'
        '<button type="button" id="live-toggle" class="icon-btn js-only" aria-pressed="false"'
        ' aria-label="Pause live updates" title="Pause live updates">'
        f'<span class="when-live">{icon("pause")}</span><span class="when-paused">'
        f"{icon('play')}</span></button></div>"
        f'{auth_panel}<main id="content" tabindex="-1">{content}</main></div></div>'
        '<aside id="drawer" class="drawer" aria-labelledby="drawer-title" hidden>'
        '<div class="drawer-head"><h2 id="drawer-title">Task</h2>'
        '<button type="button" id="drawer-close" class="icon-btn" aria-label="Close task details">'
        f'{icon("close")}</button></div><div id="drawer-body" class="drawer-body"></div></aside>'
        f"{_shortcuts()}"
        '<div id="toasts" class="toasts" role="status" aria-live="polite"></div>'
        f'<script src="{assets.url("core.js")}" defer></script>'
        f'<script src="{assets.url("dashboard.js")}" defer></script></body></html>'
    )


def stat(label: str, value: Any, *, state: str = "", hint: str = "") -> str:
    extra = f'<span class="stat-hint">{esc(hint)}</span>' if hint else ""
    marker = f" s-{css_token(state)}" if state else ""
    return (
        f'<div class="stat{marker}"><span class="stat-label">{esc(label)}</span>'
        f'<span class="stat-value">{esc(value)}</span>{extra}</div>'
    )


def task_mix(counts: dict[str, int]) -> str:
    total = sum(counts.values())
    offset = 0.0
    segments = []
    for state, count in counts.items():
        if count:
            width = 100 * count / total
            segments.append(
                f'<rect class="seg s-{css_token(state)}" x="{offset:.2f}" width="{width:.2f}"'
                ' height="8"/>'
            )
            offset += width
    chart = (
        '<svg class="mix" viewBox="0 0 100 8" preserveAspectRatio="none" role="img" '
        f'aria-label="{total} tasks by state">{"".join(segments)}</svg>'
        if total
        else ""
    )
    items = "".join(
        f'<li{" class=zero" if not count else ""}>{badge(state)} <strong class="num">{count}'
        "</strong></li>"
        for state, count in counts.items()
    )
    return f'{chart}<ul class="counts">{items}</ul>'


def overview_page(data: dict[str, Any], now: datetime) -> str:
    agent_rows, attrs, healthy = [], [], 0
    for agent in data["agents"]:
        beat = agent.get("latest_heartbeat")
        _, seconds = age(beat["observed_at"] if beat else None, now)
        status = agent["status"] if beat else "no heartbeat"
        if seconds is not None and seconds > STALE_HEARTBEAT_SECONDS:
            status = "stale"
        elif beat and status not in ("degraded", "stopping", "stopped"):
            healthy += 1
        current = beat["current_task_id"] if beat else None
        agent_rows.append(
            [
                f"<strong>{esc(agent['agent_id'])}</strong>",
                badge(status),
                when(beat["observed_at"], now) if beat else '<span class="muted">never</span>',
                short_id(current),
                esc(agent["responsibility"]),
            ]
        )
        attrs.append(task_attr(current))
    active = [item for item in data["objectives"] if item["status"] == "active"]
    objective_rows = []
    for item in active:
        stats = item["progress"]
        percent = stats["completion_percent"]
        objective_rows.append(
            [
                objective_link(item),
                badge(item["status"]),
                progress(stats["completed_tasks"], stats["total_tasks"]),
                f"{percent:.0f}%" if percent is not None else '<span class="muted">no tasks</span>',
                esc(item["priority"]),
            ]
        )
    counts = data["task_counts"]
    error_rows = [
        [
            when(item["occurred_at"], now),
            esc(item["agent"]),
            f"<code>{esc(item['error_type'])}</code>",
            esc(item["message"]),
            badge("resolved" if item["resolved"] else "open"),
        ]
        for item in data["errors"]
    ]
    waiting = counts.get("waiting_for_approval", 0) + counts.get("waiting_for_verification", 0)
    failing = counts.get("failed", 0) + counts.get("blocked", 0)
    unresolved = sum(1 for item in data["errors"] if not item["resolved"])
    stats_html = (
        '<div class="stats">'
        + stat(
            "Agents healthy",
            f"{healthy}/{len(data['agents'])}",
            state="ok" if data["agents"] and healthy == len(data["agents"]) else "",
        )
        + stat("Active objectives", len(active))
        + stat("Running tasks", counts.get("running", 0), state="running")
        + stat(
            "Waiting", waiting, state="paused" if waiting else "", hint="approval or verification"
        )
        + stat("Failed or blocked", failing, state="failed" if failing else "")
        + stat("Open errors", unresolved, state="open" if unresolved else "", hint="latest 10")
        + "</div>"
    )
    return (
        page_head("Overview", esc("Agents, objectives and task flow from persistent state."))
        + stats_html
        + section(
            "Agents",
            table(
                ["Agent", "Status", "Last heartbeat", "Current task", "Responsibility"],
                agent_rows,
                "No agents are registered yet.",
                hint="Agents appear here once the runtime registers them.",
                key="agents",
                row_attrs=attrs,
            ),
            count=len(agent_rows),
            key="agents",
        )
        + section(
            "Active objectives",
            table(
                ["Objective", "Status", "Progress", ("Done", "num"), ("Priority", "num")],
                objective_rows,
                "No active objectives.",
                hint="Objectives submitted to the API show here while they are active.",
                key="active-objectives",
            ),
            count=len(objective_rows),
            key="active-objectives",
        )
        + section("Tasks by state", task_mix(counts), count=sum(counts.values()), key="task-mix")
        + section(
            "Recent errors",
            table(
                ["When", "Agent", "Type", "Message", "State"],
                error_rows,
                "No errors have been recorded.",
                hint="Failures and their recovery attempts are listed here.",
                key="errors",
            ),
            key="errors",
        )
    )


def objectives_page(data: dict[str, Any], now: datetime) -> str:
    rows = []
    for item in data["items"]:
        stats = item["progress"]
        rows.append(
            [
                objective_link(item),
                badge(item["status"]),
                esc(item["priority"]),
                progress(stats["completed_tasks"], stats["total_tasks"]),
                when(item["created_at"], now),
                short_id(item["id"]),
            ]
        )
    return page_head("Objectives") + section(
        "All objectives",
        table(
            ["Objective", "Status", ("Priority", "num"), "Progress", "Created", "ID"],
            rows,
            "No objectives have been submitted yet.",
            hint="Submit one with POST /objectives using the API token.",
            key="objectives",
        ),
        count=data["total"],
        key="objectives",
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
        return f"<li {task_attr(task_id)}>{label}{f'<ul>{nested}</ul>' if nested else ''}</li>"

    roots = [task["id"] for task in tasks if task["id"] not in has_parent]
    # A defensive pass keeps any task that is only reachable through a cycle visible.
    body = "".join(node(root) for root in roots) + "".join(
        node(task["id"]) for task in tasks if task["id"] not in shown
    )
    if not body:
        return empty("This objective has no tasks.", "ORCHESTR adds tasks as it plans the work.")
    return f'<div id="task-graph" class="graph-host"></div><ul class="dag">{body}</ul>'


def objective_page(data: dict[str, Any], now: datetime) -> str:
    item = data["objective"]
    stats = item["progress"]
    criteria = "".join(f"<li>{esc(entry)}</li>" for entry in item["completion_criteria"])
    head = (
        '<nav class="crumbs" aria-label="Breadcrumb">'
        '<a href="/dashboard/objectives">Objectives</a><span aria-hidden="true">/</span>'
        f"{short_id(item['id'])}</nav>"
        f'<div class="page-head"><h1>{esc(item["title"])}</h1>'
        f'<p class="meta">{badge(item["status"])}<span>priority '
        f'<strong class="num">{esc(item["priority"])}</strong></span>'
        f"{progress(stats['completed_tasks'], stats['total_tasks'])}"
        f"<span>created {when(item['created_at'], now)}</span></p></div>"
        f'<p class="lead">{esc(item["description"])}</p>'
        + (
            f'<div class="criteria"><h3>Completion criteria</h3><ul>{criteria}</ul></div>'
            if criteria
            else ""
        )
    )
    requirements = table(
        ["Requirement", "Type", "Status", ("Priority", "num"), "Acceptance criteria"],
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
        key="requirements",
    )
    findings = table(
        ["Agent", "Question", "Findings", "Class", ("Confidence", "num"), "Status"],
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
        key="findings",
    )
    capability_rows = []
    for row in data["capabilities"]:
        evidence = "<br>".join(
            f"{esc(run['suite'])}: {badge(run['verdict'] or run['status'])} "
            f"{esc(run['passed'])} passed, {esc(run['failed'])} failed, "
            f"{esc(run['errors'])} errors"
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
        key="capabilities",
    )
    decisions = table(
        ["When", "Decision", "Operation", "Rationale", "Expires"],
        [
            [
                when(row["created_at"], now),
                badge(row["decision"]),
                f"<code>{esc(row['operation'])}</code>",
                esc(row["rationale"]),
                when(row["expires_at"], now),
            ]
            for row in data["decisions"]
        ],
        "No security decisions for this objective's tasks.",
        key="objective-decisions",
        row_attrs=[task_attr(row["task_id"]) for row in data["decisions"]],
    )
    authorizations = table(
        ["Requested", "Question", "Status", "Responded"],
        [
            [
                when(row["requested_at"], now),
                esc(row["question"]),
                badge(row["status"]),
                when(row["responded_at"], now),
            ]
            for row in data["authorizations"]
        ],
        "No authorization requests for this objective.",
        key="authorizations",
    )
    final = data["final_message"]
    final_html = (
        f'<p class="muted">{when(final["occurred_at"], now)} via {esc(final["channel"])}</p>'
        f'<pre class="message">{esc(final["summary"])}</pre>'
        if final
        else empty("MUSE has not sent a message for this objective yet.")
    )
    return (
        head
        + section(
            "Task graph",
            _dag(data["tasks"], data["dependencies"]),
            count=len(data["tasks"]),
            key="graph",
        )
        + section("Requirements", requirements, count=len(data["requirements"]))
        + section("Research findings", findings, count=len(data["findings"]))
        + section("Capabilities and verification", capabilities, count=len(data["capabilities"]))
        + section("Security decisions", decisions, count=len(data["decisions"]))
        + section("Authorization requests", authorizations, count=len(data["authorizations"]))
        + section("Final MUSE message", final_html, key="final")
    )


def tasks_page(
    data: dict[str, Any],
    states: Sequence[str],
    state: str | None,
    agent: str | None,
    now: datetime,
) -> str:
    options = "".join(
        f'<option value="{esc(item)}"{" selected" if item == state else ""}>{esc(item)}</option>'
        for item in states
    )
    form = (
        '<form method="get" class="filters" id="task-filters"><label>State <select name="state">'
        f'<option value="">all</option>{options}</select></label>'
        f'<label>Agent <input name="agent" maxlength="24" value="{escape(agent or "")}"'
        ' autocomplete="off"></label>'
        '<button type="submit">Apply</button></form>'
    )
    rows = [
        [
            f'<a href="/dashboard/objectives/{esc(item["objective_id"])}">{esc(item["title"])}</a>',
            badge(item["state"]),
            esc(item["assigned_agent"]),
            esc(item["priority"]),
            f"{esc(item['attempt'])}/{esc(item['max_attempts'])}",
            f'<span class="error-text">{esc(item["last_error"])}</span>'
            if item["last_error"]
            else esc(None),
            when(item["updated_at"], now),
            short_id(item["id"]),
        ]
        for item in data["items"]
    ]
    return (
        page_head("Tasks", esc("Select a row to see its runs, evidence and events."))
        + form
        + section(
            "Tasks",
            table(
                [
                    "Task",
                    "State",
                    "Agent",
                    ("Priority", "num"),
                    ("Attempts", "num"),
                    "Last error",
                    "Updated",
                    "ID",
                ],
                rows,
                "No tasks match.",
                hint="Clear the filters or wait for ORCHESTR to plan work.",
                key="tasks",
                row_attrs=[task_attr(item["id"]) for item in data["items"]],
            ),
            count=data["total"],
            key="tasks",
        )
    )


def security_page(data: dict[str, Any], now: datetime) -> str:
    pending = data["pending"]["items"]
    pending_rows = []
    for item in pending:
        request_id = esc(item["id"])
        person = item.get("audience_person_id")
        if person:
            attrs = f'data-request-id="{request_id}" data-person-id="{esc(person)}"'
            actions = (
                '<span class="actions">'
                f'<button type="button" class="approve" data-respond="approved" {attrs}>Approve'
                "</button>"
                f'<button type="button" class="deny" data-respond="denied" {attrs}>Deny</button>'
                "</span>"
            )
        else:
            actions = '<span class="muted">Not delivered to a person yet</span>'
        pending_rows.append(
            [
                when(item["requested_at"], now),
                esc(item["question"]),
                when(item["expires_at"], now),
                actions,
            ]
        )
    decision_rows = [
        [
            when(item["created_at"], now),
            badge(item["decision"]),
            f"<code>{esc(item['operation'])}</code>",
            esc(item["rationale"]),
            compact_json(item["scope"]),
            when(item["expires_at"], now),
        ]
        for item in data["decisions"]["items"]
    ]
    return (
        page_head(
            "Security", esc("Approvals waiting on a person, and WARDEN's recorded decisions.")
        )
        + section(
            "Pending approvals",
            table(
                ["Requested", "Question", "Expires", "Respond"],
                pending_rows,
                "Nothing is waiting for approval.",
                key="pending",
            ),
            count=len(pending_rows),
            key="pending",
        )
        + section(
            "Security decisions",
            table(
                ["When", "Decision", "Operation", "Rationale", "Scope", "Expires"],
                decision_rows,
                "No security decisions have been recorded.",
                key="decisions",
                row_attrs=[task_attr(item["task_id"]) for item in data["decisions"]["items"]],
            ),
            count=data["decisions"]["total"],
            key="decisions",
        )
    )


AUTH_PANEL = (
    '<section id="auth-panel" class="auth" aria-labelledby="auth-title">'
    '<div class="auth-text"><h2 id="auth-title">Approval authentication</h2>'
    '<p class="muted">The API token is kept in this browser tab only (sessionStorage) and is sent'
    " solely as a request header when you approve or deny.</p></div>"
    '<div class="auth-fields"><label>API token <input id="api-token" type="password"'
    ' autocomplete="off" spellcheck="false"></label>'
    '<label>Response note <input id="respond-note" maxlength="500"'
    ' placeholder="Optional note recorded with the decision"></label>'
    '<button type="button" id="forget-token">Forget token</button></div>'
    '<div id="respond-status" class="respond-status" role="status"></div></section>'
)


def sparkline(
    values: Sequence[float], *, low: float | None = None, high: float | None = None
) -> str:
    if not values:
        return empty("No measurements.")
    low = min(values) if low is None else low
    high = max(values) if high is None else high
    span = (high - low) or 1.0
    width, height = 300, 56
    points = [
        (
            width * index / max(len(values) - 1, 1),
            height - 3 - (height - 6) * (value - low) / span,
        )
        for index, value in enumerate(values)
    ]
    if len(points) == 1:
        points = [(0.0, points[0][1]), (float(width), points[0][1])]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    area = f"M0,{height} L{line.replace(' ', ' L')} L{width},{height} Z"
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        'role="img" aria-label="trend">'
        f'<path class="spark-area" d="{area}"/>'
        f'<polyline class="spark-line" points="{line}"/></svg>'
    )


def resources_page(data: dict[str, Any], now: datetime) -> str:
    items = data["items"]
    if not items:
        return page_head("Resources") + section(
            "Host resources",
            empty(
                "No resource measurements have been recorded yet.",
                "The runtime samples CPU, RAM and disk while agents run.",
            ),
        )
    latest = items[0]
    chronological = list(reversed(items))

    def series(key: str) -> list[float]:
        return [number(item[key]) for item in chronological]

    cpu, ram, disk = series("cpu_percent"), series("available_ram_bytes"), series("free_disk_bytes")
    cards = [
        ("cpu", "CPU", f"{number(latest['cpu_percent']):.1f}%", cpu, 0.0, 100.0, "%"),
        ("ram", "Available RAM", human_bytes(latest["available_ram_bytes"]), ram, None, None, "B"),
        ("disk", "Free disk", human_bytes(latest["free_disk_bytes"]), disk, None, None, "B"),
    ]
    body = "".join(
        f'<div class="metric" data-metric="{key}"><div class="metric-head">'
        f'<h3>{esc(name)}</h3><span class="metric-value">{esc(value)}</span></div>'
        f"{sparkline(values, low=low, high=high)}"
        '<div class="metric-range"><span>min '
        f"{esc(f'{min(values):.1f}%' if unit == '%' else human_bytes(min(values)))}</span><span>max "
        f"{esc(f'{max(values):.1f}%' if unit == '%' else human_bytes(max(values)))}</span></div>"
        "</div>"
        for key, name, value, values, low, high, unit in cards
    )
    rows = [
        [
            when(item["observed_at"], now),
            keyed(number(item["cpu_percent"]), f"{number(item['cpu_percent']):.1f}%"),
            keyed(number(item["available_ram_bytes"]), human_bytes(item["available_ram_bytes"])),
            keyed(number(item["free_disk_bytes"]), human_bytes(item["free_disk_bytes"])),
        ]
        for item in items[:20]
    ]
    return (
        page_head(
            "Resources",
            f"Latest measurement {when(latest['observed_at'], now)}; trend covers the last "
            f"{len(items)} of {data['total']} measurements.",
        )
        + f'<div class="metrics">{body}</div>'
        + section(
            "Recent measurements",
            table(
                ["Observed", ("CPU", "num"), ("Available RAM", "num"), ("Free disk", "num")],
                rows,
                "",
                key="measurements",
            ),
            key="measurements",
        )
    )


def budget(used: Any, limit: Any, *, money: bool = False) -> str:
    used_text = f"${number(used):.4f}" if money else count(used)
    if limit is None:
        return f'<span class="num">{esc(used_text)}</span> <span class="muted">no limit</span>'
    limit_text = f"${number(limit):.2f}" if money else count(limit)
    ratio = number(used) / number(limit) if number(limit) > 0 else 1.0
    level = tone(ratio)
    return (
        f'<span class="budget">{bar(ratio, f"{ratio:.0%} of budget", kind=level, marks=True)}'
        f'<span class="num">{esc(used_text)} / {esc(limit_text)}</span>'
        f'<span class="pct t-{level}">{ratio:.0%}</span></span>'
    )


def providers_page(data: dict[str, Any]) -> str:
    rows, attrs = [], []
    date = data["items"][0]["date"] if data["items"] else ""
    for item in data["items"]:
        today, policy = item["today"], item["budget"] or {}
        rows.append(
            [
                f"<strong>{esc(item['provider'])}</strong>",
                budget(today["requests"], policy.get("max_requests_per_day")),
                budget(today["tokens"], policy.get("max_tokens_per_day")),
                budget(today["cost_usd"], policy.get("max_cost_usd_per_day"), money=True),
                esc(today["active"]),
            ]
        )
        attrs.append("")
        for model in item["model_budgets"]:
            usage, limits = model["today"], model["budget"]
            rows.append(
                [
                    f'<span class="sub-row">{esc(item["provider"])}/{esc(model["model"])}</span>',
                    budget(usage["requests"], limits.get("max_requests_per_day")),
                    budget(usage["tokens"], limits.get("max_tokens_per_day")),
                    budget(usage["cost_usd"], limits.get("max_cost_usd_per_day"), money=True),
                    esc(usage["active"]),
                ]
            )
            attrs.append('class="sub"')
    return page_head(
        "Providers",
        esc(
            f"Bars turn amber at {WARN_RATIO:.0%} and red at {CRITICAL_RATIO:.0%} of a daily budget."
        ),
    ) + section(
        "Today's usage vs budgets (UTC day)",
        table(
            ["Provider", "Requests", "Tokens", "Cost", ("In flight", "num")],
            rows,
            "No provider usage or budgets are configured.",
            key="providers",
            row_attrs=attrs,
        ),
        key="providers",
        note=date,
    )


def events_page(data: dict[str, Any], event_type: str | None, now: datetime) -> str:
    form = (
        '<form method="get" class="filters" id="event-filters"><label>Event type '
        f'<input name="type" maxlength="64" value="{escape(event_type or "")}"'
        ' autocomplete="off"></label><button type="submit">Apply</button></form>'
    )
    rows = [
        [
            when(item["created_at"], now),
            f"<code>{esc(item['event_type'])}</code>",
            esc(item["category"]),
            esc(item["actor"]),
            f"{esc(item['aggregate_type'])} {short_id(item['aggregate_id'])}",
            compact_json(item["payload"]),
        ]
        for item in data["items"]
    ]
    return (
        page_head("Events", esc("The append-only event log, newest first."))
        + form
        + section(
            "Latest events",
            table(
                ["When", "Type", "Category", "Actor", "Subject", "Payload"],
                rows,
                "No events match.",
                key="events",
                row_attrs=[task_attr(item["task_id"]) for item in data["items"]],
            ),
            count=data["total"],
            key="events",
        )
    )
