from pathlib import Path
from typing import Any

import asyncpg
import pytest

from aether import main as cli
from aether.storage import bootstrap

URL = 'postgresql://aether:hunter2pw@dbhost:5432/my"db'


class FakeConnection:
    def __init__(self, *, exists: bool = False, duplicate: bool = False) -> None:
        self.exists = exists
        self.duplicate = duplicate
        self.executed: list[str] = []
        self.lookup: tuple[Any, ...] = ()
        self.closed = False

    async def fetchrow(self, query: str) -> dict[str, str]:
        return {"server_version": "16.2", "database": "aether", "user": "aether"}

    async def fetchval(self, query: str, *args: Any) -> int | None:
        self.lookup = args
        return 1 if self.exists else None

    async def execute(self, query: str) -> None:
        if self.duplicate:
            raise asyncpg.DuplicateDatabaseError("exists")
        self.executed.append(query)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Run in an empty directory so a developer's real .env is never read.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AETHER_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture
def connect(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, Any]] = []
    holder: dict[str, FakeConnection] = {"connection": FakeConnection()}

    async def fake_connect(**kwargs: Any) -> FakeConnection:
        calls.append(kwargs)
        return holder["connection"]

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    return calls, holder


def test_redact_url_hides_password() -> None:
    redacted = bootstrap.redact_url("postgresql+asyncpg://aether:hunter2pw@dbhost:5432/aether")
    assert "hunter2pw" not in redacted
    assert "dbhost:5432/aether" in redacted
    assert bootstrap.redact_url("not a url") == "<invalid database url>"


def test_quote_identifier_escapes_quotes() -> None:
    assert bootstrap.quote_identifier('my"db') == '"my""db"'
    assert bootstrap.quote_identifier("x; DROP DATABASE y") == '"x; DROP DATABASE y"'
    with pytest.raises(ValueError):
        bootstrap.quote_identifier("")


async def test_check_database(connect) -> None:
    calls, holder = connect
    info = await bootstrap.check_database("postgresql://u:pw@dbhost:5432/aether")
    assert info == {"server_version": "16.2", "database": "aether", "user": "aether"}
    assert calls[0]["database"] == "aether"
    assert calls[0]["host"] == "dbhost"
    assert holder["connection"].closed


async def test_create_database_if_missing_creates_with_quoting(connect) -> None:
    calls, holder = connect
    assert await bootstrap.create_database_if_missing(URL) is True
    assert calls[0]["database"] == "postgres"
    assert holder["connection"].lookup == ('my"db',)
    assert holder["connection"].executed == ['CREATE DATABASE "my""db"']
    assert holder["connection"].closed


async def test_create_database_already_exists(connect) -> None:
    _, holder = connect
    holder["connection"] = FakeConnection(exists=True)
    assert await bootstrap.create_database_if_missing(URL) is False
    assert holder["connection"].executed == []
    assert holder["connection"].closed


async def test_create_database_race_is_treated_as_existing(connect) -> None:
    _, holder = connect
    holder["connection"] = FakeConnection(duplicate=True)
    assert await bootstrap.create_database_if_missing(URL) is False
    assert holder["connection"].closed


async def test_rejects_non_postgres_url() -> None:
    with pytest.raises(ValueError):
        await bootstrap.check_database("sqlite+aiosqlite:///:memory:")


def test_cli_db_check_output_has_no_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://aether:hunter2pw@dbhost:5432/aether")

    async def fake_check(url: str) -> dict[str, str]:
        return {"server_version": "16.2", "database": "aether", "user": "aether"}

    monkeypatch.setattr(bootstrap, "check_database", fake_check)
    cli.main(["db", "check"])
    output = capsys.readouterr().out
    assert "hunter2pw" not in output
    assert "dbhost:5432/aether" in output
    assert "16.2" in output


def test_cli_db_check_failure_is_redacted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AETHER_DATABASE_URL", "postgresql://aether:hunter2pw@dbhost:5432/aether")

    async def failing_check(url: str) -> dict[str, str]:
        raise ConnectionRefusedError(f"cannot connect with {url}")

    monkeypatch.setattr(bootstrap, "check_database", failing_check)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["db", "check"])
    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "hunter2pw" not in captured.out + captured.err


@pytest.mark.parametrize(("created", "text"), [(True, "created"), (False, "already exists")])
def test_cli_db_create(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    created: bool,
    text: str,
) -> None:
    monkeypatch.setenv("AETHER_DATABASE_URL", "postgresql://aether:hunter2pw@dbhost:5432/aether")

    async def fake_create(url: str) -> bool:
        return created

    monkeypatch.setattr(bootstrap, "create_database_if_missing", fake_create)
    cli.main(["db", "create"])
    assert text in capsys.readouterr().out


def test_cli_check_config_hides_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AETHER_DATABASE_URL", "postgresql://aether:hunter2pw@dbhost:5432/aether")
    cli.main(["check-config"])
    output = capsys.readouterr().out
    assert "hunter2pw" not in output
    assert "aether:" not in output
    assert "dbhost:5432/aether" in output
