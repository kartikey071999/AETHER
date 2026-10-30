"""Command-line entry point for AETHER."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from aether import __version__
from aether.agents import AGENT_DEFINITIONS
from aether.config import load_settings


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
    return parser


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
        print(f"Database driver: {settings.database_url.get_secret_value().split(':', 1)[0]}")
        print(f"Maximum concurrent tasks: {settings.max_concurrent_tasks}")
        print(f"Minimum free disk: {settings.min_free_disk_gb:g} GB")
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
