"""Pre-built SQLite databases copied per test instead of rebuilding the schema every time.

``Base.metadata.create_all`` costs ~0.4s per database on Windows (every DDL statement is its
own fsync'd commit) and ``alembic upgrade head`` costs seconds; copying a file built once per
test session costs ~1ms. A copy is a byte-identical SQLite database, so tests see exactly the
schema ``create_all`` (or the migrations) would build.
"""

import atexit
import shutil
import tempfile
from functools import cache
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine.url import make_url

from aether.storage.database import Database
from aether.storage.models import Base

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@cache
def _template_dir() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="aether-test-schema-"))
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    return directory


@cache
def schema_template() -> Path:
    """The empty ``create_all`` database, built once per process and removed at exit."""
    path = _template_dir() / "schema.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(connection)
    finally:
        engine.dispose()
    return path


@cache
def migrated_template() -> Path:
    """An empty database upgraded with ``alembic upgrade head``, built once per process."""
    path = _template_dir() / "migrated.db"
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(_template_dir())  # never read a developer .env
        patch.delenv("DATABASE_URL", raising=False)
        patch.setenv("AETHER_DATABASE_URL", sqlite_url(path))
        command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")
    return path


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _copy(template: Path, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, path)
    return path


def copy_schema(path: Path) -> Path:
    """Write an empty SQLite database holding every table of ``Base.metadata`` at ``path``."""
    return _copy(schema_template(), path)


def copy_migrated(path: Path) -> Path:
    """Write an empty SQLite database migrated to alembic ``head`` at ``path``."""
    return _copy(migrated_template(), path)


def copy_schema_to(database_url: str) -> None:
    """Install the schema at the file a ``sqlite+aiosqlite`` settings URL points to."""
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or not url.database:
        raise ValueError("the schema template only applies to file-based SQLite URLs")
    copy_schema(Path(url.database))


def schema_database(path: Path) -> Database:
    """A ``Database`` on a fresh copy of the schema template at ``path``."""
    return Database(sqlite_url(copy_schema(path)))
