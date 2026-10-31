"""Deterministic resource-aware selection for persistent ready tasks."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.observability.recorder import stage_event
from aether.runtime.resource_monitor import HostResources


@dataclass(frozen=True, slots=True)
class SchedulingCandidate:
    task_id: UUID
    priority: int
    waiting_since: datetime
    state: str = "ready"
    deadline: datetime | None = None
    estimated_ram_bytes: int = 0
    estimated_disk_bytes: int = 0
    estimated_cpu_percent: float = 25.0
    is_build: bool = False
    dependencies_satisfied: bool = True


class SchedulerPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_concurrent_tasks: int = Field(default=2, ge=1)
    max_concurrent_builds: int = Field(default=1, ge=1)
    max_cpu_percent: float = Field(default=85.0, gt=0, le=100)
    min_free_disk_bytes: int = Field(default=5 * 1024**3, ge=0)
    priority_aging_points_per_hour: float = Field(default=1.0, ge=0)
    max_aging_points: float = Field(default=30.0, ge=0)


@dataclass(frozen=True, slots=True)
class SchedulerResources:
    host: HostResources
    running_tasks: int = 0
    running_builds: int = 0


@dataclass(frozen=True, slots=True)
class DeferredTask:
    task_id: UUID
    reason: str


@dataclass(frozen=True, slots=True)
class SchedulingDecision:
    selected_task_id: UUID | None
    score: float | None
    reason: str
    deferred: tuple[DeferredTask, ...]
    decided_at: datetime


class Scheduler:
    def __init__(
        self,
        policy: SchedulerPolicy,
        sessions: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._policy = policy
        self._sessions = sessions

    @property
    def policy(self) -> SchedulerPolicy:
        return self._policy

    def choose_next(
        self,
        candidates: Iterable[SchedulingCandidate],
        resources: SchedulerResources,
        *,
        now: datetime | None = None,
    ) -> SchedulingDecision:
        decided_at = now or datetime.now(UTC)
        eligible: list[tuple[float, datetime, SchedulingCandidate]] = []
        deferred: list[DeferredTask] = []

        for candidate in candidates:
            reason = self._ineligibility(candidate, resources)
            if reason:
                deferred.append(DeferredTask(candidate.task_id, reason))
                continue
            score = self._score(candidate, decided_at)
            deadline_sort = candidate.deadline or candidate.waiting_since
            eligible.append((score, deadline_sort, candidate))

        if not eligible:
            return SchedulingDecision(
                selected_task_id=None,
                score=None,
                reason="No ready task fits current dependencies and resource limits.",
                deferred=tuple(deferred),
                decided_at=decided_at,
            )

        eligible.sort(key=lambda item: (-item[0], item[1], str(item[2].task_id)))
        score, _, selected = eligible[0]
        deferred.extend(
            DeferredTask(candidate.task_id, "Lower effective priority than selected work.")
            for _, _, candidate in eligible[1:]
        )
        return SchedulingDecision(
            selected_task_id=selected.task_id,
            score=score,
            reason="Highest-priority eligible task under current resource limits.",
            deferred=tuple(deferred),
            decided_at=decided_at,
        )

    async def record_decision(self, decision: SchedulingDecision) -> None:
        if self._sessions is None:
            raise RuntimeError("Scheduler has no database session factory for decision recording")
        aggregate_id = decision.selected_task_id or UUID(int=0)
        payload = {
            "selected_task_id": decision.selected_task_id,
            "score": decision.score,
            "reason": decision.reason,
            "deferred": {item.task_id: item.reason for item in decision.deferred},
        }
        async with self._sessions() as session, session.begin():
            stage_event(
                session,
                "scheduler.decision",
                source="runtime.scheduler",
                actor="ORCHESTR",
                aggregate_type="scheduler",
                aggregate_id=aggregate_id,
                payload=payload,
                task_id=decision.selected_task_id,
            )

    def _ineligibility(
        self,
        candidate: SchedulingCandidate,
        resources: SchedulerResources,
    ) -> str | None:
        if candidate.state != "ready":
            return "Task is not in the ready state."
        if not candidate.dependencies_satisfied:
            return "Dependencies are not complete."
        if resources.running_tasks >= self._policy.max_concurrent_tasks:
            return "Concurrent task limit reached."
        if candidate.is_build and resources.running_builds >= self._policy.max_concurrent_builds:
            return "Concurrent build limit reached."
        if (
            resources.host.cpu_percent + candidate.estimated_cpu_percent
            > self._policy.max_cpu_percent
        ):
            return "Estimated CPU use exceeds the configured limit."
        if candidate.estimated_ram_bytes > resources.host.available_ram_bytes:
            return "Insufficient available RAM for the estimate."
        if (
            resources.host.free_disk_bytes - candidate.estimated_disk_bytes
            < self._policy.min_free_disk_bytes
        ):
            return "Starting this task would breach the minimum free-disk reserve."
        return None

    def _score(self, candidate: SchedulingCandidate, now: datetime) -> float:
        age_hours = max(0.0, (now - candidate.waiting_since).total_seconds() / 3600)
        aging = min(
            age_hours * self._policy.priority_aging_points_per_hour,
            self._policy.max_aging_points,
        )
        urgency = 0.0
        if candidate.deadline is not None:
            hours_until_deadline = (candidate.deadline - now).total_seconds() / 3600
            if hours_until_deadline <= 0:
                urgency = 50.0
            elif hours_until_deadline <= 1:
                urgency = 30.0
            elif hours_until_deadline <= 24:
                urgency = 10.0
        return candidate.priority + aging + urgency
