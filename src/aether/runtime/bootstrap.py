"""Assemble the runtime from settings: logging, durable events, spool replay, and the engine."""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from aether.agents.base import BaseAgent
from aether.agents.factory import build_agents
from aether.config import AetherSettings
from aether.models import ModelProvider, ProviderSettings
from aether.observability.logging import configure_logging
from aether.observability.recorder import EventRecorder
from aether.observability.retention import RetentionResult, apply_retention
from aether.observability.spool import EventSpool, ReplayResult
from aether.runtime.engine import Engine, EngineLimits
from aether.runtime.models import ModelServices, allowlist_hosts, build_model_services
from aether.runtime.resource_monitor import ResourceMonitor
from aether.runtime.scheduler import Scheduler, SchedulerPolicy
from aether.runtime.security import SecurityServices, build_security_services
from aether.security.policy import SecuritySettings
from aether.storage.database import Database
from aether.storage.repositories.provider_usage import ProviderUsageRepository

_LOGGER = logging.getLogger("aether.error.runtime")


def spool_path(settings: AetherSettings) -> Path:
    return settings.data_dir / "spool" / "events.jsonl"


@dataclass(slots=True)
class Runtime:
    """Everything one `aether run` process owns; `start` must precede accepting any work."""

    settings: AetherSettings
    database: Database
    recorder: EventRecorder
    engine: Engine
    security: SecurityServices
    models: ModelServices | None = None
    spool_replay: ReplayResult | None = None
    retention: RetentionResult | None = None
    started: bool = field(default=False)

    async def start(self) -> None:
        """Configure logging, apply retention, replay the spool, then start the engine."""
        settings = self.settings
        configure_logging(
            settings.log_dir,
            max_bytes=settings.log_max_bytes,
            backup_count=max(1, settings.log_backup_count),
        )
        self.retention = apply_retention(
            settings.log_dir,
            archive_after_days=settings.log_retention_days,
            delete_archives_after_days=settings.log_retention_days,
        )
        spool = self.recorder.spool
        if spool is not None:
            self.spool_replay = await spool.replay(self.database.sessions)
            if self.spool_replay.failed:
                _LOGGER.error(
                    "Event spool entries could not be replayed",
                    extra={"failed": self.spool_replay.failed, "spool": str(spool.path)},
                )
            self.engine.startup_details = {
                "spool_replayed": self.spool_replay.replayed,
                "spool_failed": self.spool_replay.failed,
            }
        await self.engine.start()
        self.started = True

    async def run(self, *, once: bool = False, duration: float | None = None) -> None:
        """Run until stopped by signal or `duration`; `once` performs a single scheduler tick."""
        try:
            await self.start()
            if once:
                await self.engine.schedule_once()
                return
            timer = None
            if duration is not None:
                timer = asyncio.get_running_loop().call_later(duration, self.engine.request_stop)
            try:
                await self.engine.run_forever()
            finally:
                if timer is not None:
                    timer.cancel()
        finally:
            try:
                await self.engine.stop()
            finally:
                if self.models is not None:
                    await self.models.aclose()

    async def close(self) -> None:
        try:
            if self.models is not None:
                await self.models.aclose()
        finally:
            await self.database.close()


def build_runtime(
    settings: AetherSettings,
    agents: Sequence[BaseAgent] | None = None,
    *,
    security_settings: SecuritySettings | None = None,
    provider_settings: ProviderSettings | None = None,
    model_providers: Sequence[ModelProvider] | None = None,
) -> Runtime:
    """Construct the runtime; nothing touches the database until `Runtime.start`.

    ``model_providers`` replaces the providers built from ``provider_settings`` (used by tests).
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    provider_settings = provider_settings or ProviderSettings()
    database = Database.from_settings(settings)
    recorder = EventRecorder(EventSpool(spool_path(settings)))
    # The only security services of the process; provider hosts join the allowlist before the
    # policy is frozen, and agents receive the services through their contexts.
    security = build_security_services(
        settings,
        database.sessions,
        recorder,
        security_settings=security_settings,
        extra_hosts=allowlist_hosts(provider_settings),
    )
    models = build_model_services(
        provider_settings,
        settings,
        security.permissions,
        ProviderUsageRepository(database.sessions),
        recorder,
        providers=model_providers,
    )
    limits = EngineLimits.from_settings(settings)
    scheduler = Scheduler(
        SchedulerPolicy(
            max_concurrent_tasks=settings.max_concurrent_tasks,
            max_concurrent_builds=settings.max_concurrent_builds,
            min_free_disk_bytes=int(settings.min_free_disk_gb * 1024**3),
        ),
        database.sessions,
    )
    engine = Engine(
        database.sessions,
        agents if agents is not None else build_agents(),
        scheduler,
        ResourceMonitor(),
        limits,
        security=security,
        models=models,
    )
    return Runtime(
        settings=settings,
        database=database,
        recorder=recorder,
        engine=engine,
        security=security,
        models=models,
    )
