import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine


@event.listens_for(Engine, "connect")
def _skip_sqlite_fsync(dbapi_connection, _record) -> None:
    """Test databases never need to survive power loss: skip the per-commit fsync.

    Locking, transactions and rollback behave exactly as with the default setting; only the
    flush to stable storage is skipped, which costs several milliseconds per commit on Windows.
    """
    if "sqlite" in type(dbapi_connection).__module__:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA synchronous=OFF")
        cursor.close()


@pytest.fixture(autouse=True)
def offline_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime tests never construct real providers, whatever the developer's .env holds."""
    monkeypatch.setenv("AETHER_PROVIDER_MOCK", "1")
