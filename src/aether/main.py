"""Command-line entry point for AETHER."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from aether import __version__
from aether.agents import AGENT_DEFINITIONS
from aether.config import load_settings
from aether.storage import bootstrap
from aether.storage.database import Database
from aether.storage.models import EventRecord

if TYPE_CHECKING:
    from aether.runtime.models import ModelServices, ProviderStatus


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number") from None
    if not seconds > 0 or seconds == float("inf"):
        raise argparse.ArgumentTypeError("must be a positive, finite number")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aether",
        description="A local-first, persistent multi-agent runtime.",
    )
    parser.add_argument("--version", action="version", version=f"AETHER {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("agents", help="list the configured agent responsibilities")
    commands.add_parser(
        "check-config", help="validate configuration without connecting to services"
    )
    run = commands.add_parser("run", help="start the runtime until interrupted")
    run.add_argument("--once", action="store_true", help="start, run one scheduler tick, and stop")
    run.add_argument(
        "--duration",
        type=_positive_seconds,
        default=None,
        help="stop gracefully after this many seconds",
    )
    events = commands.add_parser("events", help="inspect the durable event ledger")
    events_commands = events.add_subparsers(dest="events_command", required=True)
    tail = events_commands.add_parser("tail", help="print the most recent events")
    tail.add_argument("--limit", type=_positive_int, default=20, help="number of events to show")
    security = commands.add_parser("security", help="inspect the runtime security policy")
    security_commands = security.add_subparsers(dest="security_command", required=True)
    security_commands.add_parser(
        "show", help="print the effective, redacted security policy and its fingerprint"
    )
    models = commands.add_parser("models", help="inspect and exercise the model router")
    models_commands = models.add_subparsers(dest="models_command", required=True)
    models_commands.add_parser(
        "status", help="show configured providers, their models, and today's usage vs budget"
    )
    model_test = models_commands.add_parser(
        "test", help="send one tiny completion through the router (respects budgets)"
    )
    model_test.add_argument(
        "--tier",
        choices=("light", "standard", "reasoning", "premium"),
        default="light",
        help="model tier to request (default: light)",
    )
    web = commands.add_parser("web", help="inspect the web research tools")
    web_commands = web.add_subparsers(dest="web_command", required=True)
    web_commands.add_parser(
        "status", help="show whether a GitHub token is configured and the current API rate limit"
    )
    database = commands.add_parser("db", help="manage the PostgreSQL schema")
    database_commands = database.add_subparsers(dest="database_command", required=True)
    database_commands.add_parser("upgrade", help="apply pending database migrations")
    database_commands.add_parser("check", help="verify connectivity to the configured database")
    database_commands.add_parser("create", help="create the configured database if it is missing")
    api = commands.add_parser("api", help="serve the local persistent-state HTTP API")
    api_commands = api.add_subparsers(dest="api_command", required=True)
    api_commands.add_parser("serve", help="serve the API using AETHER_API_HOST and AETHER_API_PORT")
    demo = commands.add_parser("demo", help="run a scripted end-to-end scenario")
    demo_commands = demo.add_subparsers(dest="demo_command", required=True)
    demo_capability = demo_commands.add_parser(
        "capability",
        help="run the capability workflow against the configured database and print its trace",
    )
    demo_capability.add_argument(
        "--live",
        action="store_true",
        help="use the configured model router instead of the scripted mock provider",
    )
    demo_capability.add_argument(
        "--timeout",
        type=_positive_seconds,
        default=180.0,
        help="give up after this many seconds (default: 180)",
    )
    return parser


def _describe_database(url: str) -> str:
    try:
        parsed = make_url(url)
    except (ArgumentError, ValueError):
        return "<invalid database url>"
    host = parsed.host or "(local socket)"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{host}{port}/{parsed.database or ''}"


def _run_database_command(command: str) -> int:
    url = load_settings().database_url_for_alembic
    try:
        if command == "check":
            print(f"Database URL: {bootstrap.redact_url(url)}")
            info = asyncio.run(bootstrap.check_database(url))
            print(f"Server version: {info['server_version']}")
            print(f"Database: {info['database']}")
            print(f"User: {info['user']}")
        else:
            created = asyncio.run(bootstrap.create_database_if_missing(url))
            print("Database created." if created else "Database already exists.")
    except Exception as error:  # noqa: BLE001 - report any failure without a traceback
        print(f"Database {command} failed: {type(error).__name__}", file=sys.stderr)
        print(f"Database URL: {bootstrap.redact_url(url)}", file=sys.stderr)
        return 1
    return 0


async def _run_runtime(once: bool, duration: float | None) -> None:
    from aether.runtime.bootstrap import build_runtime

    settings = load_settings()
    print(f"Database URL: {bootstrap.redact_url(settings.database_url_for_alembic)}")
    runtime = build_runtime(settings)
    try:
        await runtime.run(once=once, duration=duration)
    finally:
        await runtime.close()


def _run_command(once: bool, duration: float | None) -> int:
    try:
        asyncio.run(_run_runtime(once, duration))
    except KeyboardInterrupt:
        print("Interrupted; runtime stopped.")
    except Exception as error:  # noqa: BLE001 - report any failure without a traceback
        print(f"Runtime failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


def _format_event(record: EventRecord) -> str:
    from aether.observability.recorder import split_legacy_payload
    from aether.observability.redaction import redact

    _, data = split_legacy_payload(record.payload)
    created = record.created_at.isoformat() if record.created_at else "-"
    details = json.dumps(redact(data), sort_keys=True, default=str)
    return (
        f"{created} {record.event_type} actor={record.actor} source={record.source or '-'} "
        f"{record.aggregate_type}={record.aggregate_id} {details}"
    )


async def _tail_events(limit: int) -> list[str]:
    settings = load_settings()
    database = Database.from_settings(settings)
    try:
        async with database.sessions() as session:
            rows = (
                await session.scalars(
                    select(EventRecord)
                    .order_by(EventRecord.created_at.desc(), EventRecord.id)
                    .limit(limit)
                )
            ).all()
    finally:
        await database.close()
    return [_format_event(row) for row in reversed(rows)]


def _tail_command(limit: int) -> int:
    try:
        lines = asyncio.run(_tail_events(limit))
    except Exception as error:  # noqa: BLE001 - report any failure without a traceback
        print(f"Reading events failed: {type(error).__name__}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


def _security_show_command() -> int:
    from aether.models import ProviderSettings
    from aether.runtime.models import allowlist_hosts
    from aether.runtime.security import build_security_policy
    from aether.security import describe_policy, policy_fingerprint

    try:
        # Same construction as the runtime, so the fingerprint matches runtime.security_initialized.
        policy = build_security_policy(
            load_settings(), extra_hosts=allowlist_hosts(ProviderSettings())
        )
    except Exception as error:  # noqa: BLE001 - report any failure without a traceback
        print(f"Loading the security policy failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(f"Policy fingerprint: {policy_fingerprint(policy)}")
    print(json.dumps(describe_policy(policy), indent=2, sort_keys=True))
    return 0


@asynccontextmanager
async def _model_services() -> AsyncIterator[ModelServices]:
    from aether.models import ProviderSettings
    from aether.observability.recorder import EventRecorder
    from aether.runtime.models import allowlist_hosts, build_model_services
    from aether.runtime.security import build_security_services
    from aether.storage.repositories.provider_usage import ProviderUsageRepository

    settings = load_settings()
    provider_settings = ProviderSettings()
    database = Database.from_settings(settings)
    recorder = EventRecorder()
    try:
        security = build_security_services(
            settings,
            database.sessions,
            recorder,
            extra_hosts=allowlist_hosts(provider_settings),
        )
        services = build_model_services(
            provider_settings,
            settings,
            security.permissions,
            ProviderUsageRepository(database.sessions),
            recorder,
        )
        try:
            yield services
        finally:
            await services.aclose()
    finally:
        await database.close()


def _limit(value: object, *, money: bool = False) -> str:
    if value is None:
        return "unlimited"
    return f"${float(value):.2f}" if money else str(value)


def _format_provider(status: ProviderStatus) -> list[str]:
    models = ", ".join(status.models) or "-"
    lines = [
        (
            f"{status.name}: configured={'yes' if status.configured else 'no'} "
            f"available={'yes' if status.available else 'no'} models={models}"
        )
    ]
    if status.usage is not None:
        usage = status.usage
        lines.append(
            f"  today: requests {usage.requests}/{_limit(usage.max_requests)}, "
            f"tokens {usage.tokens}/{_limit(usage.max_tokens)}, "
            f"cost ${float(usage.cost_usd):.6f}/{_limit(usage.max_cost_usd, money=True)}"
        )
    return lines


async def _models_status() -> int:
    async with _model_services() as services:
        try:
            statuses = await services.status()
        except Exception as error:  # noqa: BLE001 - report any failure without a traceback
            print(f"Reading provider usage failed: {type(error).__name__}", file=sys.stderr)
            statuses = await services.describe()
            failed = 1
        else:
            failed = 0
        if services.settings.mock:
            print("Mock providers only (AETHER_PROVIDER_MOCK is set).")
        for status in statuses:
            for line in _format_provider(status):
                print(line)
        return failed


async def _models_test(tier: str) -> int:
    from aether.communication.messages import AgentId
    from aether.models import CompletionRequest, ModelTier, NoModelAvailable

    request = CompletionRequest(
        messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        max_tokens=16,
        tier=ModelTier(tier),
        agent=AgentId.ORCHESTR,
    )
    async with _model_services() as services:
        try:
            response = await services.router.complete(request)
        except NoModelAvailable as error:
            print(
                f"No model available for tier {tier}: {'; '.join(error.reasons)}", file=sys.stderr
            )
            return 1
    print(f"Provider: {response.provider}")
    print(f"Model: {response.model}")
    print(f"Tokens: {response.input_tokens} in, {response.output_tokens} out")
    print(f"Cost: ${response.cost_usd:.6f}")
    return 0


def _models_command(command: str, tier: str) -> int:
    try:
        if command == "status":
            return asyncio.run(_models_status())
        return asyncio.run(_models_test(tier))
    except Exception as error:  # noqa: BLE001 - report any failure without a traceback
        print(f"Models {command} failed: {type(error).__name__}", file=sys.stderr)
        return 1


async def _web_status() -> int:
    from aether.tools.web import WebSettings, fetch_github_status

    status = await fetch_github_status(WebSettings())
    print(f"GitHub token configured: {'yes' if status.token_configured else 'no'}")
    if not status.reachable:
        print(f"GitHub API: unreachable ({status.error})", file=sys.stderr)
        return 1
    if status.status_code != 200 or not status.buckets:
        detail = status.error or f"HTTP {status.status_code}"
        print(f"GitHub rate limit: unavailable ({detail})", file=sys.stderr)
        return 1
    for name, bucket in status.buckets.items():
        reset = bucket.reset.strftime("%Y-%m-%d %H:%M:%S UTC") if bucket.reset else "unknown"
        print(
            f"GitHub {name} rate limit: {bucket.remaining}/{bucket.limit} remaining, resets {reset}"
        )
    return 0


def _web_command() -> int:
    try:
        return asyncio.run(_web_status())
    except Exception as error:  # noqa: BLE001 - report any failure without a traceback
        print(f"Web status failed: {type(error).__name__}", file=sys.stderr)
        return 1


async def _demo_capability(live: bool, timeout: float) -> int:
    from aether.models import ProviderSettings
    from aether.runtime.scenario import (
        CapabilityScenario,
        ScenarioTimeout,
        ScenarioTrace,
        ScriptedCapabilityProvider,
        capability_finished,
        format_trace,
        verification_failed,
    )

    settings = load_settings()
    print(f"Database URL: {bootstrap.redact_url(settings.database_url_for_alembic)}")
    if live:
        print("Models: the configured model router (real providers, budgets apply)")
    else:
        print("Models: scripted mock provider (no network)")
    print(
        "Note: the Python interpreter running AETHER is added to the executable allowlist "
        "for this run; WARDEN still decides every operation."
    )

    def finished(trace: ScenarioTrace) -> bool:
        return capability_finished(trace) or verification_failed(trace)

    scenario = CapabilityScenario(
        settings,
        model_providers=None if live else [ScriptedCapabilityProvider()],
        provider_settings=ProviderSettings() if live else None,
        create_schema=False,
    )
    async with scenario:
        objective_id = await scenario.submit_objective(tag=f"demo {uuid4().hex[:8]}")
        print(f"Objective submitted: {objective_id}")
        try:
            trace = await scenario.run_until(objective_id, finished, timeout=timeout)
        except ScenarioTimeout as error:
            trace = error.trace
            print(f"The workflow did not finish within {timeout:g}s.", file=sys.stderr)
    print()
    for line in format_trace(trace):
        print(line)
    return 0 if capability_finished(trace) else 1


def _demo_command(live: bool, timeout: float) -> int:
    try:
        return asyncio.run(_demo_capability(live, timeout))
    except KeyboardInterrupt:
        print("Interrupted; the demo runtime stopped.")
        return 1
    except Exception as error:  # noqa: BLE001 - report any failure without a traceback
        print(f"Demo failed: {type(error).__name__}", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    arguments = parser.parse_args(argv)

    if arguments.command == "api" and arguments.api_command == "serve":
        import uvicorn

        from aether.api import create_app

        settings = load_settings()
        uvicorn.run(
            create_app(settings),
            host=settings.api_host,
            port=settings.api_port,
            access_log=False,
        )
        return

    if arguments.command == "agents":
        for definition in AGENT_DEFINITIONS:
            print(f"{definition.agent_id.value}: {definition.responsibility}")
        return

    if arguments.command == "check-config":
        settings = load_settings()
        print("Configuration is valid.")
        print(f"Database: {_describe_database(settings.database_url_for_alembic)}")
        print(f"Workspace directory: {settings.workspace_dir}")
        print(f"Maximum concurrent tasks: {settings.max_concurrent_tasks}")
        print(f"Minimum free disk: {settings.min_free_disk_gb:g} GB")
        return

    if arguments.command == "run":
        status = _run_command(arguments.once, arguments.duration)
        if status:
            raise SystemExit(status)
        return

    if arguments.command == "events" and arguments.events_command == "tail":
        status = _tail_command(arguments.limit)
        if status:
            raise SystemExit(status)
        return

    if arguments.command == "security" and arguments.security_command == "show":
        status = _security_show_command()
        if status:
            raise SystemExit(status)
        return

    if arguments.command == "models":
        status = _models_command(arguments.models_command, getattr(arguments, "tier", "light"))
        if status:
            raise SystemExit(status)
        return

    if arguments.command == "web" and arguments.web_command == "status":
        status = _web_command()
        if status:
            raise SystemExit(status)
        return

    if arguments.command == "demo" and arguments.demo_command == "capability":
        status = _demo_command(arguments.live, arguments.timeout)
        if status:
            raise SystemExit(status)
        return

    if arguments.command == "db" and arguments.database_command in {"check", "create"}:
        status = _run_database_command(arguments.database_command)
        if status:
            raise SystemExit(status)
        return

    if arguments.command == "db" and arguments.database_command == "upgrade":
        from alembic import command
        from alembic.config import Config

        project_root = Path(__file__).resolve().parents[2]
        command.upgrade(Config(str(project_root / "alembic.ini")), "head")
        print("Database migrations applied.")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
