"""Read-only per-agent queue views for operators (CLI, HTTP API, dashboard panels).

This is the one place that turns a work queue into plain data. It lists exactly what the agent's
own queue lists (open tasks by effective priority, or finished tasks on request) and never task
content such as descriptions, results or errors. It has no write path and no LLM.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.agents.work_queue import DEFAULT_LIMIT, WorkItem, WorkQueueService
from aether.communication.messages import AgentId
from aether.observability.redaction import redact_string


def work_item_data(item: WorkItem) -> dict[str, Any]:
    """A queue item as plain data; JSON-friendly apart from datetimes and UUIDs."""
    return {
        "id": item.id,
        "objective_id": item.objective_id,
        "title": item.title,
        "state": item.state.value,
        "assigned_agent": item.assigned_agent,
        "priority": item.priority,
        "effective_priority": round(item.effective_priority, 3),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "deadline": item.deadline,
        "completed_at": item.completed_at,
        "attempt": item.attempt,
        "max_attempts": item.max_attempts,
        "parent_task_id": item.parent_task_id,
        "requirement_id": item.requirement_id,
        "depends_on": [
            {
                "task_id": dependency.task_id,
                "state": dependency.state.value if dependency.state else None,
                "assigned_agent": dependency.assigned_agent,
            }
            for dependency in item.depends_on
        ],
        "blocked_by": list(item.blocked_by),
        "ready_to_run": item.ready_to_run,
    }


async def agent_queue_panel(
    sessions: async_sessionmaker[AsyncSession],
    agent_id: AgentId,
    *,
    finished: bool = False,
    limit: int = DEFAULT_LIMIT,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """``agent_id``'s queue: open tasks by default, finished (incl. cancelled) when asked for."""
    agent = AgentId(agent_id)
    service = WorkQueueService(sessions, clock=clock)
    if finished:
        items = await service.finished_tasks(agent, limit=limit, include_cancelled=True)
    else:
        items = await service.open_tasks(agent, limit=limit)
    return {
        "agent": agent.value,
        "view": "finished" if finished else "open",
        "limit": limit,
        "count": len(items),
        "items": [work_item_data(item) for item in items],
    }


def format_queue_lines(panel: dict[str, Any]) -> list[str]:
    """Plain-text rendering of ``agent_queue_panel`` for terminals."""
    lines = [f"{panel['agent']} {panel['view']} queue: {panel['count']} task(s)"]
    for item in panel["items"]:
        waiting = f" blocked_by={len(item['blocked_by'])}" if item["blocked_by"] else ""
        ready = " ready" if item["ready_to_run"] else ""
        lines.append(
            f"{item['id']} {item['state']:<24} priority={item['priority']} "
            f"effective={item['effective_priority']:g}{ready}{waiting} {redact_string(item['title'])}"
        )
    return lines


__all__ = ["agent_queue_panel", "format_queue_lines", "work_item_data"]
