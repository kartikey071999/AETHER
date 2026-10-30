"""Command-line entry point for AETHER."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from aether import __version__
from aether.agents import AGENT_DEFINITIONS
from aether.config import load_settings
from aether.storage import bootstrap


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
