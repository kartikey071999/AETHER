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


def test_session_and_input_hash_migration_round_trips(tmp_path, monkeypatch):
    database_path = tmp_path / "session_columns.db"
    config = _alembic_config(monkeypatch, database_path)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        command.upgrade(config, "head")
        inspector = inspect(engine)
        event_columns = {column["name"] for column in inspector.get_columns("events")}
        assert {"session_id", "branch"} <= event_columns
        assert "ix_events_session_id" in {i["name"] for i in inspector.get_indexes("events")}
        auth_indexes = {i["name"] for i in inspector.get_indexes("authorization_requests")}
        assert "ix_authorization_requests_input_hash" in auth_indexes

        command.downgrade(config, "0002_storage_schema_expansion")
        inspector = inspect(engine)
        event_columns = {column["name"] for column in inspector.get_columns("events")}
        assert not {"session_id", "branch"} & event_columns
        auth_columns = {c["name"] for c in inspector.get_columns("authorization_requests")}
        assert "input_hash" not in auth_columns
        assert "ix_events_session_id" not in {i["name"] for i in inspector.get_indexes("events")}

        command.upgrade(config, "head")
        inspector = inspect(engine)
        assert "session_id" in {c["name"] for c in inspector.get_columns("events")}
        auth_columns = {c["name"] for c in inspector.get_columns("authorization_requests")}
        assert "input_hash" in auth_columns
    finally:
        engine.dispose()


def test_test_run_verdict_migration_preserves_existing_rows(tmp_path, monkeypatch):
    database_path = tmp_path / "test_verdict.db"
    config = _alembic_config(monkeypatch, database_path)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        command.upgrade(config, "0003_event_session_and_auth_hash")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO test_runs "
                    "(id, suite, status, passed, failed, errors, acceptance_results, started_at) "
                    "VALUES (:id, 'offline', 'running', 0, 0, 0, '[]', CURRENT_TIMESTAMP)"
                ),
                {"id": "00000000000040008000000000000001"},
            )
        command.upgrade(config, "head")
        with engine.begin() as connection:
            assert connection.execute(text("SELECT verdict FROM test_runs")).scalar_one() is None
            connection.execute(
                text("UPDATE test_runs SET verdict = 'inconclusive', status = 'failed'")
            )
        command.downgrade(config, "0003_event_session_and_auth_hash")
        assert "verdict" not in {c["name"] for c in inspect(engine).get_columns("test_runs")}
        with engine.connect() as connection:
            assert connection.execute(text("SELECT suite FROM test_runs")).scalar_one() == "offline"
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT verdict FROM test_runs")).scalar_one() is None
    finally:
        engine.dispose()


def test_provider_usage_objective_migration_keeps_history_and_round_trips(tmp_path, monkeypatch):
    database_path = tmp_path / "usage_objective.db"
    config = _alembic_config(monkeypatch, database_path)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        command.upgrade(config, "0004_test_run_verdict")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO provider_usage (id, provider, model, agent, request_count, "
                    "input_tokens, output_tokens, cost_usd, status, occurred_at) "
                    "VALUES (:id, 'mock', 'm0', 'FORGE', 1, 5, 7, 0, 'succeeded', CURRENT_TIMESTAMP)"
                ),
                {"id": "00000000000040008000000000000002"},
            )
        command.upgrade(config, "head")
        inspector = inspect(engine)
        assert "objective_id" in {c["name"] for c in inspector.get_columns("provider_usage")}
        indexes = {i["name"]: i["column_names"] for i in inspector.get_indexes("provider_usage")}
        assert indexes["ix_provider_usage_objective_agent"] == ["objective_id", "agent"]
        with engine.connect() as connection:
            row = connection.execute(text("SELECT objective_id, agent FROM provider_usage")).one()
        assert tuple(row) == (None, "FORGE")

        command.downgrade(config, "0004_test_run_verdict")
        inspector = inspect(engine)
        assert "objective_id" not in {c["name"] for c in inspector.get_columns("provider_usage")}
        assert "ix_provider_usage_objective_agent" not in {
            i["name"] for i in inspector.get_indexes("provider_usage")
        }
        with engine.connect() as connection:
            assert connection.execute(text("SELECT agent FROM provider_usage")).scalar_one() == (
                "FORGE"
            )
        command.upgrade(config, "head")
        assert "objective_id" in {c["name"] for c in inspect(engine).get_columns("provider_usage")}
    finally:
        engine.dispose()


def test_message_claims_migration_backfills_inconclusive_runs_and_round_trips(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "claims.db"
    config = _alembic_config(monkeypatch, database_path)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        command.upgrade(config, "0005_provider_usage_objective")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO messages (id, source_agent, target_agent, message_type, "
                    "correlation_id, timestamp, payload, priority, requires_response, status, "
                    "created_at) VALUES (:id, 'ORCHESTR', 'FORGE', 'task_assign', :id, "
                    "CURRENT_TIMESTAMP, '{}', 50, 0, 'processing', CURRENT_TIMESTAMP)"
                ),
                {"id": "00000000000040008000000000000003"},
            )
            for run_id, status, verdict in (
                ("00000000000040008000000000000004", "cancelled", "inconclusive"),
                ("00000000000040008000000000000005", "cancelled", None),
                ("00000000000040008000000000000006", "failed", "failed"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO test_runs (id, suite, status, verdict, passed, failed, "
                        "errors, acceptance_results, started_at) VALUES (:id, 'offline', "
                        ":status, :verdict, 0, 0, 0, '[]', CURRENT_TIMESTAMP)"
                    ),
                    {"id": run_id, "status": status, "verdict": verdict},
                )

        command.upgrade(config, "head")
        inspector = inspect(engine)
        message_columns = {c["name"] for c in inspector.get_columns("messages")}
        assert {"claimed_by", "claimed_at", "claim_expires_at"} <= message_columns
        with engine.connect() as connection:
            message = connection.execute(
                text("SELECT status, claimed_by, claim_expires_at FROM messages")
            ).one()
            statuses = dict(connection.execute(text("SELECT id, status FROM test_runs")).all())
        assert tuple(message) == ("processing", None, None)  # legacy rows count as expired
        assert statuses == {
            "00000000000040008000000000000004": "inconclusive",
            "00000000000040008000000000000005": "cancelled",
            "00000000000040008000000000000006": "failed",
        }

        command.downgrade(config, "0005_provider_usage_objective")
        message_columns = {c["name"] for c in inspect(engine).get_columns("messages")}
        assert not {"claimed_by", "claimed_at", "claim_expires_at"} & message_columns
        with engine.connect() as connection:
            assert connection.execute(text("SELECT status FROM messages")).scalar_one() == (
                "processing"
            )
            assert (
                connection.execute(
                    text("SELECT status FROM test_runs WHERE verdict = 'inconclusive'")
                ).scalar_one()
                == "cancelled"
            )

        command.upgrade(config, "head")
        assert "claim_expires_at" in {c["name"] for c in inspect(engine).get_columns("messages")}
    finally:
        engine.dispose()


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
