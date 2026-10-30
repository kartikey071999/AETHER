from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from aether.storage.models import Base

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _alembic_config(monkeypatch, database_path: Path) -> Config:
    monkeypatch.setenv("AETHER_DATABASE_URL", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def test_migration_history_has_a_single_head():
    script = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))

    assert len(script.get_heads()) == 1
    revision = script.get_revision("0002_storage_schema_expansion")
    assert revision.down_revision == "0001_runtime_foundation"


def test_migrations_upgrade_to_models_and_downgrade_cleanly(tmp_path, monkeypatch):
    database_path = tmp_path / "migrations.db"
    config = _alembic_config(monkeypatch, database_path)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        command.upgrade(config, "head")

        inspector = inspect(engine)
        migrated_tables = set(inspector.get_table_names()) - {"alembic_version"}
        assert migrated_tables == set(Base.metadata.tables)
        for table in Base.metadata.sorted_tables:
            migrated_columns = {column["name"] for column in inspector.get_columns(table.name)}
            assert migrated_columns == {column.name for column in table.columns}, table.name
        with engine.connect() as connection:
            agents = connection.execute(text("SELECT agent_id FROM agents")).scalars().all()
        assert set(agents) == {"ORCHESTR", "SCOUT", "ROGUE", "FORGE", "LEDGER", "WARDEN", "MUSE"}

        command.downgrade(config, "0001_runtime_foundation")
        inspector = inspect(engine)
        assert "agents" not in inspector.get_table_names()
        event_columns = {column["name"] for column in inspector.get_columns("events")}
        assert "category" not in event_columns

        command.downgrade(config, "base")
        assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    finally:
        engine.dispose()
