"""Command-line entry point for AETHER."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from aether import __version__
from aether.agents import AGENT_DEFINITIONS
from aether.config import load_settings
from aether.storage import bootstrap
from aether.storage.database import Database
from aether.storage.models import EventRecord


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
    database = commands.add_parser("db", help="manage the PostgreSQL schema")
    database_commands = database.add_subparsers(dest="database_command", required=True)
    database_commands.add_parser("upgrade", help="apply pending database migrations")
    database_commands.add_parser("check", help="verify connectivity to the configured database")
    database_commands.add_parser("create", help="create the configured database if it is missing")
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
    from aether.security import describe_policy, load_security_policy, policy_fingerprint

    try:
        policy = load_security_policy(load_settings())
    except Exception as error:  # noqa: BLE001 - report any failure without a traceback
        print(f"Loading the security policy failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(f"Policy fingerprint: {policy_fingerprint(policy)}")
    print(json.dumps(describe_policy(policy), indent=2, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    arguments = parser.parse_args(argv)

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
