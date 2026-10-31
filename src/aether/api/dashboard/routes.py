"""Read-only dashboard routes; the only write is the browser calling the token-guarded API."""

from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import HTMLResponse

from aether.api.dashboard import data, render
from aether.api.services import Services
from aether.storage.models import TaskStatus

dashboard = APIRouter(prefix="/dashboard")
_STATIC = Path(__file__).parent / "static"
_ASSETS = {
    "dashboard.css": "text/css; charset=utf-8",
    "dashboard.js": "text/javascript; charset=utf-8",
}
_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
Fragment = Annotated[bool, Query(description="Return only the refreshable content")]


def _page(
    title: str, active: str, content: str, fragment: bool, *, auth_panel: str = ""
) -> HTMLResponse:
    html = content if fragment else render.layout(title, active, content, auth_panel=auth_panel)
    return HTMLResponse(html, headers=_HEADERS)


@dashboard.get("/static/{name}", include_in_schema=False)
async def asset(name: str) -> Response:
    if name not in _ASSETS:
        raise HTTPException(404, "Unknown asset")
    return Response(
        (_STATIC / name).read_bytes(),
        media_type=_ASSETS[name],
        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
    )


@dashboard.get("", response_class=HTMLResponse)
@dashboard.get("/", response_class=HTMLResponse, include_in_schema=False)
async def overview(api: Services, fragment: Fragment = False) -> HTMLResponse:
    content = render.overview_page(await data.overview(api), api.clock())
    return _page("Overview", "overview", content, fragment)


@dashboard.get("/objectives", response_class=HTMLResponse)
async def objectives(api: Services, fragment: Fragment = False) -> HTMLResponse:
    content = render.objectives_page(await data.objectives(api))
    return _page("Objectives", "objectives", content, fragment)


@dashboard.get("/objectives/{objective_id}", response_class=HTMLResponse)
async def objective(objective_id: UUID, api: Services, fragment: Fragment = False) -> HTMLResponse:
    content = render.objective_page(await data.objective_detail(api, objective_id))
    return _page("Objective", "objectives", content, fragment)


@dashboard.get("/tasks", response_class=HTMLResponse)
async def tasks(
    api: Services,
    state: Annotated[str | None, Query(max_length=32)] = None,
    agent: Annotated[str | None, Query(max_length=24)] = None,
    fragment: Fragment = False,
) -> HTMLResponse:
    # A blank filter field means "all"; an unknown state is a client error.
    try:
        status = TaskStatus(state) if state else None
    except ValueError:
        raise HTTPException(422, "Unknown task state") from None
    agent = agent or None
    page = await data.tasks(api, state=status, agent=agent)
    content = render.tasks_page(
        page, [item.value for item in TaskStatus], status.value if status else None, agent
    )
    return _page("Tasks", "tasks", content, fragment)


@dashboard.get("/security", response_class=HTMLResponse)
async def security(api: Services, fragment: Fragment = False) -> HTMLResponse:
    content = render.security_page(await data.security(api))
    return _page("Security", "security", content, fragment, auth_panel=render.AUTH_PANEL)


@dashboard.get("/resources", response_class=HTMLResponse)
async def resources(api: Services, fragment: Fragment = False) -> HTMLResponse:
    content = render.resources_page(await data.resources(api))
    return _page("Resources", "resources", content, fragment)


@dashboard.get("/providers", response_class=HTMLResponse)
async def providers(api: Services, fragment: Fragment = False) -> HTMLResponse:
    content = render.providers_page(await data.providers(api))
    return _page("Providers", "providers", content, fragment)


@dashboard.get("/events", response_class=HTMLResponse)
async def events(
    api: Services,
    event_type: Annotated[str | None, Query(alias="type", max_length=64)] = None,
    fragment: Fragment = False,
) -> HTMLResponse:
    event_type = event_type or None
    content = render.events_page(await data.events(api, event_type=event_type), event_type)
    return _page("Events", "events", content, fragment)
