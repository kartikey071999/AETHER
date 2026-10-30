"""Helpers to verify and create the PostgreSQL database AETHER runs against."""

from __future__ import annotations

from typing import Any

import asyncpg
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from aether.config import normalize_database_url

MAINTENANCE_DATABASE = "postgres"


def redact_url(url: str) -> str:
    """Render ``url`` with the password masked; never raises or echoes the input."""
    try:
        return make_url(url).render_as_string(hide_password=True)
    except (ArgumentError, ValueError):
        return "<invalid database url>"


def quote_identifier(name: str) -> str:
    """Quote ``name`` as a PostgreSQL identifier."""
    if not name or "\x00" in name:
        raise ValueError("Invalid database name.")
    return '"' + name.replace('"', '""') + '"'


def _parse(url: str) -> URL:
    parsed = make_url(normalize_database_url(url))
    if not parsed.drivername.startswith("postgresql"):
        raise ValueError("Only PostgreSQL database URLs are supported.")
    if not parsed.database:
        raise ValueError("The database URL does not name a database.")
    return parsed


def _connect_kwargs(parsed: URL, database: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "host": parsed.host,
        "port": parsed.port,
        "user": parsed.username,
        "password": parsed.password,
        "database": database,
    }
    if "ssl" in parsed.query:
        kwargs["ssl"] = parsed.query["ssl"]
    return {key: value for key, value in kwargs.items() if value is not None}


async def check_database(url: str) -> dict[str, str]:
    """Connect to the configured database and report server version, database and user."""
    parsed = _parse(url)
    connection = await asyncpg.connect(**_connect_kwargs(parsed, parsed.database))
    try:
        row = await connection.fetchrow(
            "SELECT current_setting('server_version') AS server_version, "
            "current_database() AS database, current_user AS user"
        )
    finally:
        await connection.close()
    return {
        "server_version": str(row["server_version"]),
        "database": str(row["database"]),
        "user": str(row["user"]),
    }


async def create_database_if_missing(url: str) -> bool:
    """Create the database named in ``url``; return True only if it was created."""
    parsed = _parse(url)
    name = parsed.database
    connection = await asyncpg.connect(**_connect_kwargs(parsed, MAINTENANCE_DATABASE))
    try:
        exists = await connection.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name)
        if exists:
            return False
        try:
            await connection.execute(f"CREATE DATABASE {quote_identifier(name)}")
        except asyncpg.DuplicateDatabaseError:
            return False
        return True
    finally:
        await connection.close()
