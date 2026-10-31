"""Read-only dashboard routes; the only write is the browser calling the token-guarded API.

HTML pages are complete without JavaScript. The ``/dashboard/data`` views return the same
sanitized state as JSON so the browser can refresh a page without reloading it.
"""

from typing import Annotated, Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from aether.api.dashboard import assets, data, render
from aether.api.services import ApiServices, Services
from aether.storage.models import TaskStatus

dashboard = APIRouter(prefix="/dashboard")
_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
_JSON_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
Fragment = Annotated[bool, Query(description="Return only the refreshable content")]
StateFilter = Annotated[str | None, Query(max_length=32)]
AgentFilter = Annotated[str | None, Query(max_length=24)]
EventFilter = Annotated[str | None, Query(alias="type", max_length=64)]


def _page(
    title: str,
    active: str,
    content: str,
    fragment: bool,
    *,
    source: str,
    auth_panel: str = "",
) -> HTMLResponse:
    html = (
        content
        if fragment
        else render.layout(title, active, content, auth_panel=auth_panel, source=source)
    )
    return HTMLResponse(html, headers=_HEADERS)


def _json(api: ApiServices, view: str, payload: dict[str, Any]) -> JSONResponse:
    # The browser computes ages against the server clock, not its own.
    response = api.response(
        {
            "view": view,
            "generated_at": api.clock(),
            "stale_heartbeat_seconds": render.STALE_HEARTBEAT_SECONDS,
            **payload,
        }
    )
    response.headers.update(_JSON_HEADERS)
    return response


def _source(path: str, **params: str | None) -> str:
    query = urlencode({key: value for key, value in params.items() if value})
    return f"/dashboard/data/{path}{f'?{query}' if query else ''}"


def _task_state(state: str | None) -> TaskStatus | None:
    # A blank filter field means "all"; an unknown state is a client error.
    try:
        return TaskStatus(state) if state else None
    except ValueError:
        raise HTTPException(422, "Unknown task state") from None


@dashboard.get("/static/{name}", include_in_schema=False)
async def asset(name: str, request: Request, v: str | None = None) -> Response:
    item = assets.asset(name)
    if item is None:
        raise HTTPException(404, "Unknown asset")
    # Versioned URLs change whenever the content does, so they may be cached for good.
    headers = {
        "Cache-Control": assets.IMMUTABLE if v == item.version else assets.REVALIDATE,
        "ETag": item.etag,
        "X-Content-Type-Options": "nosniff",
    }
    if request.headers.get("if-none-match") == item.etag:
        return Response(status_code=304, headers=headers)
    return Response(item.body, media_type=item.media_type, headers=headers)


@dashboard.get("", response_class=HTMLResponse)
@dashboard.get("/", response_class=HTMLResponse, include_in_schema=False)
async def overview(api: Services, fragment: Fragment = False) -> HTMLResponse:
    content = render.overview_page(await data.overview(api), api.clock())
    return _page("Overview", "overview", content, fragment, source=_source("overview"))


@dashboard.get("/objectives", response_class=HTMLResponse)
async def objectives(api: Services, fragment: Fragment = False) -> HTMLResponse:
    content = render.objectives_page(await data.objectives(api), api.clock())
    return _page("Objectives", "objectives", content, fragment, source=_source("objectives"))


@dashboard.get("/objectives/{objective_id}", response_class=HTMLResponse)
async def objective(objective_id: UUID, api: Services, fragment: Fragment = False) -> HTMLResponse:
    detail = await data.objective_detail(api, objective_id)
    content = render.objective_page(detail, api.clock())
    return _page(
        "Objective",
        "objectives",
        content,
        fragment,
        source=_source(f"objectives/{objective_id}"),
    )


@dashboard.get("/tasks", response_class=HTMLResponse)
async def tasks(
    api: Services,
    state: StateFilter = None,
    agent: AgentFilter = None,
    fragment: Fragment = False,
) -> HTMLResponse:
    status = _task_state(state)
    agent = agent or None
    page = await data.tasks(api, state=status, agent=agent)
    content = render.tasks_page(
        page,
        [item.value for item in TaskStatus],
        status.value if status else None,
        agent,
        api.clock(),
    )
    source = _source("tasks", state=status.value if status else None, agent=agent)
    return _page("Tasks", "tasks", content, fragment, source=source)


@dashboard.get("/security", response_class=HTMLResponse)
async def security(api: Services, fragment: Fragment = False) -> HTMLResponse:
    content = render.security_page(await data.security(api), api.clock())
    return _page(
        "Security",
        "security",
        content,
        fragment,
        source=_source("security"),
        auth_panel=render.AUTH_PANEL,
    )


@dashboard.get("/resources", response_class=HTMLResponse)
async def resources(api: Services, fragment: Fragment = False) -> HTMLResponse:
    content = render.resources_page(await data.resources(api), api.clock())
    return _page("Resources", "resources", content, fragment, source=_source("resources"))


@dashboard.get("/providers", response_class=HTMLResponse)
async def providers(api: Services, fragment: Fragment = False) -> HTMLResponse:
    content = render.providers_page(await data.providers(api))
    return _page("Providers", "providers", content, fragment, source=_source("providers"))


@dashboard.get("/events", response_class=HTMLResponse)
async def events(
    api: Services, event_type: EventFilter = None, fragment: Fragment = False
) -> HTMLResponse:
    event_type = event_type or None
    content = render.events_page(
        await data.events(api, event_type=event_type), event_type, api.clock()
    )
    return _page("Events", "events", content, fragment, source=_source("events", type=event_type))


@dashboard.get("/data/overview")
async def overview_data(api: Services) -> JSONResponse:
    return _json(api, "overview", await data.overview(api))


@dashboard.get("/data/objectives")
async def objectives_data(api: Services) -> JSONResponse:
    return _json(api, "objectives", await data.objectives(api))


@dashboard.get("/data/objectives/{objective_id}")
async def objective_data(objective_id: UUID, api: Services) -> JSONResponse:
    return _json(api, "objective", await data.objective_detail(api, objective_id))


@dashboard.get("/data/tasks")
async def tasks_data(
    api: Services, state: StateFilter = None, agent: AgentFilter = None
) -> JSONResponse:
    status = _task_state(state)
    agent = agent or None
    page = await data.tasks(api, state=status, agent=agent)
    return _json(
        api,
        "tasks",
        {
            **page,
            "states": [item.value for item in TaskStatus],
            "filters": {"state": status.value if status else None, "agent": agent},
        },
    )


@dashboard.get("/data/tasks/{task_id}")
async def task_data(task_id: UUID, api: Services) -> JSONResponse:
    return _json(api, "task", await data.task_detail(api, task_id))


@dashboard.get("/data/security")
async def security_data(api: Services) -> JSONResponse:
    return _json(api, "security", await data.security(api))


@dashboard.get("/data/resources")
async def resources_data(api: Services) -> JSONResponse:
    return _json(api, "resources", await data.resources(api))


@dashboard.get("/data/providers")
async def providers_data(api: Services) -> JSONResponse:
    return _json(
        api,
        "providers",
        {
            **await data.providers(api),
            "thresholds": {"warn": render.WARN_RATIO, "critical": render.CRITICAL_RATIO},
        },
    )


@dashboard.get("/data/events")
async def events_data(api: Services, event_type: EventFilter = None) -> JSONResponse:
    event_type = event_type or None
    return _json(
        api,
        "events",
        {**await data.events(api, event_type=event_type), "filters": {"type": event_type}},
    )
