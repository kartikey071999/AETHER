"""Expand the storage schema for agents, research, capabilities, verification, and MUSE.

Revision ID: 0002_storage_schema_expansion
Revises: 0001_runtime_foundation
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_storage_schema_expansion"
down_revision = "0001_runtime_foundation"
branch_labels = None
depends_on = None

UUID = sa.Uuid(as_uuid=True)
JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
EMPTY_LIST = sa.text("'[]'")

CAPABILITY_STATUSES = (
    "discovered",
    "proposed",
    "approved",
    "installed",
    "tested",
    "verified",
    "disabled",
    "deprecated",
)
FINDING_CLASSIFICATIONS = ("verified", "hypothesis", "assumption", "untested")
FINDING_STATUSES = ("submitted", "consolidated", "adopted", "rejected", "needs_evidence")
AUTHORIZATION_STATUSES = ("pending", "approved", "denied", "expired")
PREFERENCE_SOURCES = ("explicit", "feedback")
INTERACTION_DIRECTIONS = ("inbound", "outbound")
RESULT_OUTCOMES = ("passed", "failed", "error", "skipped")

# Snapshot of aether.agents.AGENT_DEFINITIONS at the time of this migration.
AGENT_SEED = (
    (
        "ORCHESTR",
        "Coordinates objectives, task delegation, priorities, and resource-aware scheduling.",
    ),
    (
        "SCOUT",
        "Researches capabilities, evaluates evidence, and owns the consolidated roadmap.",
    ),
    (
        "ROGUE",
        "Explores unconventional approaches and submits evidence-backed findings to SCOUT.",
    ),
    (
        "FORGE",
        "Implements approved requirements through explicitly authorized execution tools.",
    ),
    (
        "LEDGER",
        "Maintains operational records, tracks requirements, and independently verifies outcomes.",
    ),
    (
        "WARDEN",
        "Evaluates security risks and records scoped authorization decisions.",
    ),
    (
        "MUSE",
        "Personalizes final user-facing communication from ORCHESTR's structured handoff.",
    ),
)


def _in(column: str, values: tuple[str, ...]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    _upgrade_existing_tables()
    _create_agent_tables()
    _create_research_tables()

    with op.batch_alter_table("requirements") as batch_op:
        batch_op.add_column(sa.Column("source_finding_id", UUID, nullable=True))
        batch_op.add_column(
            sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("50"))
        )
        batch_op.add_column(
            sa.Column(
                "security_review_required",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            )
        )
        batch_op.create_foreign_key(
            "fk_requirements_source_finding_id",
            "research_findings",
            ["source_finding_id"],
            ["id"],
            ondelete="SET NULL",
        )

    _create_capability_tables()
    _create_error_tables()
    _create_personalization_tables()
    _create_provider_usage_table()


def _upgrade_existing_tables() -> None:
    with op.batch_alter_table("resource_measurements") as batch_op:
        batch_op.alter_column(
            "available_ram_bytes",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "free_disk_bytes",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=False,
        )

    with op.batch_alter_table("objectives") as batch_op:
        batch_op.add_column(sa.Column("parent_objective_id", UUID, nullable=True))
        batch_op.add_column(sa.Column("deadline", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("review_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("expected_benefit", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("estimated_cost", sa.Float(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "stopping_conditions",
                JSON_DOCUMENT,
                nullable=False,
                server_default=EMPTY_LIST,
            )
        )
        batch_op.add_column(
            sa.Column(
                "generated_by_agent",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column("base_priority", sa.Integer(), nullable=False, server_default=sa.text("50"))
        )
        batch_op.add_column(
            sa.Column("last_priority_change_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_objectives_parent_objective_id",
            "objectives",
            ["parent_objective_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("parent_task_id", UUID, nullable=True))
        batch_op.add_column(sa.Column("requirement_id", UUID, nullable=True))
        batch_op.add_column(sa.Column("deadline", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("interruptible", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch_op.add_column(sa.Column("estimated_cpu_percent", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("result", JSON_DOCUMENT, nullable=True))
        batch_op.add_column(sa.Column("last_error", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("idempotency_key", sa.String(length=200), nullable=True))
        batch_op.create_foreign_key(
            "fk_tasks_parent_task_id",
            "tasks",
            ["parent_task_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_tasks_requirement_id",
            "requirements",
            ["requirement_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_unique_constraint("uq_tasks_idempotency_key", ["idempotency_key"])

    with op.batch_alter_table("events") as batch_op:
        batch_op.add_column(
            sa.Column(
                "category",
                sa.String(length=32),
                nullable=False,
                server_default="general",
            )
        )
        batch_op.add_column(sa.Column("source", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("correlation_id", UUID, nullable=True))
        batch_op.add_column(sa.Column("objective_id", UUID, nullable=True))
        batch_op.add_column(sa.Column("task_id", UUID, nullable=True))
        batch_op.create_index("ix_events_category", ["category"])
        batch_op.create_index("ix_events_correlation_id", ["correlation_id"])
        batch_op.create_index("ix_events_objective_id", ["objective_id"])
        batch_op.create_index("ix_events_task_id", ["task_id"])


def _create_agent_tables() -> None:
    agents = op.create_table(
        "agents",
        sa.Column("agent_id", sa.String(length=24), nullable=False),
        sa.Column("responsibility", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("model_preference", sa.String(length=200), nullable=True),
        sa.Column("config", JSON_DOCUMENT, nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    seeded_at = datetime.now(UTC)
    op.bulk_insert(
        agents,
        [
            {
                "agent_id": agent_id,
                "responsibility": responsibility,
                "enabled": True,
                "model_preference": None,
                "config": {},
                "created_at": seeded_at,
                "updated_at": seeded_at,
            }
            for agent_id, responsibility in AGENT_SEED
        ],
    )

    op.create_table(
        "agent_heartbeats",
        sa.Column("id", UUID, nullable=False),
        sa.Column("agent_id", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("current_task_id", UUID, nullable=True),
        sa.Column("details", JSON_DOCUMENT, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["current_task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_heartbeats_agent_observed",
        "agent_heartbeats",
        ["agent_id", "observed_at"],
    )

    op.create_table(
        "task_dependencies",
        sa.Column("task_id", UUID, nullable=False),
        sa.Column("depends_on_task_id", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("task_id <> depends_on_task_id", name="ck_task_dependencies_not_self"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["depends_on_task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id", "depends_on_task_id", name="pk_task_dependencies"),
    )
    op.create_index(
        "ix_task_dependencies_depends_on",
        "task_dependencies",
        ["depends_on_task_id"],
    )


def _create_research_tables() -> None:
    op.create_table(
        "research_findings",
        sa.Column("id", UUID, nullable=False),
        sa.Column("objective_id", UUID, nullable=True),
        sa.Column("task_id", UUID, nullable=True),
        sa.Column("agent", sa.String(length=24), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("findings", sa.Text(), nullable=False),
        sa.Column("candidate_approaches", JSON_DOCUMENT, nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("classification", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("consolidated_by", sa.String(length=24), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_research_findings_confidence"
        ),
        sa.CheckConstraint(
            _in("classification", FINDING_CLASSIFICATIONS),
            name="ck_research_findings_classification",
        ),
        sa.CheckConstraint(_in("status", FINDING_STATUSES), name="ck_research_findings_status"),
        sa.ForeignKeyConstraint(["objective_id"], ["objectives.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_research_findings_status", "research_findings", ["status"])
    op.create_index("ix_research_findings_objective_id", "research_findings", ["objective_id"])

    op.create_table(
        "finding_evidence",
        sa.Column("id", UUID, nullable=False),
        sa.Column("finding_id", UUID, nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=True),
        sa.Column("trust_level", sa.String(length=24), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["finding_id"], ["research_findings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_finding_evidence_finding_id", "finding_evidence", ["finding_id"])


def _create_capability_tables() -> None:
    op.create_table(
        "capabilities",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=24), nullable=False),
        sa.Column("provider", sa.String(length=200), nullable=True),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column("entrypoint", sa.String(length=500), nullable=True),
        sa.Column("input_schema", JSON_DOCUMENT, nullable=False),
        sa.Column("output_schema", JSON_DOCUMENT, nullable=False),
        sa.Column("required_permissions", JSON_DOCUMENT, nullable=False),
        sa.Column("dependencies", JSON_DOCUMENT, nullable=False),
        sa.Column("resource_estimates", JSON_DOCUMENT, nullable=False),
        sa.Column("verification_status", sa.String(length=24), nullable=False),
        sa.Column("known_limitations", JSON_DOCUMENT, nullable=False),
        sa.Column("requirement_id", UUID, nullable=True),
        sa.Column("objective_id", UUID, nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            _in("lifecycle_status", CAPABILITY_STATUSES),
            name="ck_capabilities_lifecycle_status",
        ),
        sa.ForeignKeyConstraint(["requirement_id"], ["requirements.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["objective_id"], ["objectives.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_capabilities_name"),
    )
    op.create_index("ix_capabilities_lifecycle_status", "capabilities", ["lifecycle_status"])

    op.create_table(
        "installations",
        sa.Column("id", UUID, nullable=False),
        sa.Column("capability_id", UUID, nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("integrity_hash", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("security_decision_id", UUID, nullable=True),
        sa.Column("artifact_path", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["capability_id"], ["capabilities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["security_decision_id"], ["security_decisions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_installations_capability_id", "installations", ["capability_id"])

    op.create_table(
        "execution_runs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("task_id", UUID, nullable=True),
        sa.Column("agent", sa.String(length=24), nullable=False),
        sa.Column("operation", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("stdout_path", sa.String(length=1024), nullable=True),
        sa.Column("stderr_path", sa.String(length=1024), nullable=True),
        sa.Column("security_decision_id", UUID, nullable=True),
        sa.Column("resource_usage", JSON_DOCUMENT, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["security_decision_id"], ["security_decisions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_execution_runs_task_started", "execution_runs", ["task_id", "started_at"])

    op.create_table(
        "test_runs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("task_id", UUID, nullable=True),
        sa.Column("capability_id", UUID, nullable=True),
        sa.Column("execution_run_id", UUID, nullable=True),
        sa.Column("suite", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("passed", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("acceptance_results", JSON_DOCUMENT, nullable=False),
        sa.Column("report_path", sa.String(length=1024), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "passed >= 0 AND failed >= 0 AND errors >= 0", name="ck_test_runs_counts"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["capability_id"], ["capabilities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["execution_run_id"], ["execution_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_test_runs_capability_id", "test_runs", ["capability_id"])
    op.create_index("ix_test_runs_task_id", "test_runs", ["task_id"])

    op.create_table(
        "test_results",
        sa.Column("id", UUID, nullable=False),
        sa.Column("test_run_id", UUID, nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.CheckConstraint(_in("outcome", RESULT_OUTCOMES), name="ck_test_results_outcome"),
        sa.ForeignKeyConstraint(["test_run_id"], ["test_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_test_results_test_run_id", "test_results", ["test_run_id"])


def _create_error_tables() -> None:
    op.create_table(
        "errors",
        sa.Column("id", UUID, nullable=False),
        sa.Column("aggregate_type", sa.String(length=32), nullable=True),
        sa.Column("aggregate_id", UUID, nullable=True),
        sa.Column("agent", sa.String(length=24), nullable=True),
        sa.Column("error_type", sa.String(length=100), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", JSON_DOCUMENT, nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_errors_resolved_occurred", "errors", ["resolved", "occurred_at"])
    op.create_index("ix_errors_aggregate", "errors", ["aggregate_type", "aggregate_id"])

    op.create_table(
        "recovery_attempts",
        sa.Column("id", UUID, nullable=False),
        sa.Column("error_id", UUID, nullable=False),
        sa.Column("action", sa.String(length=200), nullable=False),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("details", JSON_DOCUMENT, nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["error_id"], ["errors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recovery_attempts_error_id", "recovery_attempts", ["error_id"])


def _create_personalization_tables() -> None:
    op.create_table(
        "authorization_requests",
        sa.Column("id", UUID, nullable=False),
        sa.Column("objective_id", UUID, nullable=True),
        sa.Column("task_id", UUID, nullable=True),
        sa.Column("security_decision_id", UUID, nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            _in("status", AUTHORIZATION_STATUSES), name="ck_authorization_requests_status"
        ),
        sa.ForeignKeyConstraint(["objective_id"], ["objectives.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["security_decision_id"], ["security_decisions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_authorization_requests_status", "authorization_requests", ["status"])

    op.create_table(
        "persons",
        sa.Column("id", UUID, nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("relationship", sa.String(length=64), nullable=True),
        sa.Column("access_level", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "user_preferences",
        sa.Column("id", UUID, nullable=False),
        sa.Column("person_id", UUID, nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("value", JSON_DOCUMENT, nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_in("source", PREFERENCE_SOURCES), name="ck_user_preferences_source"),
        sa.ForeignKeyConstraint(["person_id"], ["persons.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("person_id", "key", name="uq_user_preferences_person_key"),
    )

    op.create_table(
        "interactions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("person_id", UUID, nullable=True),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("objective_id", UUID, nullable=True),
        sa.Column("task_id", UUID, nullable=True),
        sa.Column("authorization_request_id", UUID, nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            _in("direction", INTERACTION_DIRECTIONS), name="ck_interactions_direction"
        ),
        sa.ForeignKeyConstraint(["person_id"], ["persons.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["objective_id"], ["objectives.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["authorization_request_id"], ["authorization_requests.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_interactions_person_occurred", "interactions", ["person_id", "occurred_at"])

    op.create_table(
        "kg_entities",
        sa.Column("id", UUID, nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("attributes", JSON_DOCUMENT, nullable=False),
        sa.Column("person_id", UUID, nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["person_id"], ["persons.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entity_type", "name", name="uq_kg_entities_type_name"),
    )

    op.create_table(
        "kg_relations",
        sa.Column("id", UUID, nullable=False),
        sa.Column("source_entity_id", UUID, nullable=False),
        sa.Column("target_entity_id", UUID, nullable=False),
        sa.Column("relation_type", sa.String(length=64), nullable=False),
        sa.Column("attributes", JSON_DOCUMENT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_entity_id"], ["kg_entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_entity_id"], ["kg_entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_entity_id",
            "target_entity_id",
            "relation_type",
            name="uq_kg_relations_triple",
        ),
    )
    op.create_index("ix_kg_relations_target", "kg_relations", ["target_entity_id"])


def _create_provider_usage_table() -> None:
    op.create_table(
        "provider_usage",
        sa.Column("id", UUID, nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("agent", sa.String(length=24), nullable=True),
        sa.Column("task_id", UUID, nullable=True),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "request_count >= 0 AND input_tokens >= 0 AND output_tokens >= 0",
            name="ck_provider_usage_counts",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_provider_usage_provider_occurred",
        "provider_usage",
        ["provider", "occurred_at"],
    )


def downgrade() -> None:
    # Columns on pre-existing tables go first: requirements/tasks reference new tables.
    with op.batch_alter_table("requirements") as batch_op:
        batch_op.drop_column("updated_at")
        batch_op.drop_column("security_review_required")
        batch_op.drop_column("priority")
        batch_op.drop_column("source_finding_id")

    with op.batch_alter_table("tasks") as batch_op:
        for column in (
            "idempotency_key",
            "last_error",
            "result",
            "estimated_cpu_percent",
            "interruptible",
            "completed_at",
            "started_at",
            "scheduled_at",
            "deadline",
            "requirement_id",
            "parent_task_id",
        ):
            batch_op.drop_column(column)

    with op.batch_alter_table("objectives") as batch_op:
        for column in (
            "last_priority_change_at",
            "base_priority",
            "generated_by_agent",
            "stopping_conditions",
            "estimated_cost",
            "expected_benefit",
            "review_at",
            "deadline",
            "parent_objective_id",
        ):
            batch_op.drop_column(column)

    with op.batch_alter_table("events") as batch_op:
        batch_op.drop_index("ix_events_task_id")
        batch_op.drop_index("ix_events_objective_id")
        batch_op.drop_index("ix_events_correlation_id")
        batch_op.drop_index("ix_events_category")
        for column in ("task_id", "objective_id", "correlation_id", "source", "category"):
            batch_op.drop_column(column)

    with op.batch_alter_table("resource_measurements") as batch_op:
        batch_op.alter_column(
            "free_disk_bytes",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "available_ram_bytes",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
        )

    op.drop_index("ix_provider_usage_provider_occurred", table_name="provider_usage")
    op.drop_table("provider_usage")
    op.drop_index("ix_kg_relations_target", table_name="kg_relations")
    op.drop_table("kg_relations")
    op.drop_table("kg_entities")
    op.drop_index("ix_interactions_person_occurred", table_name="interactions")
    op.drop_table("interactions")
    op.drop_table("user_preferences")
    op.drop_table("persons")
    op.drop_index("ix_authorization_requests_status", table_name="authorization_requests")
    op.drop_table("authorization_requests")
    op.drop_index("ix_recovery_attempts_error_id", table_name="recovery_attempts")
    op.drop_table("recovery_attempts")
    op.drop_index("ix_errors_aggregate", table_name="errors")
    op.drop_index("ix_errors_resolved_occurred", table_name="errors")
    op.drop_table("errors")
    op.drop_index("ix_test_results_test_run_id", table_name="test_results")
    op.drop_table("test_results")
    op.drop_index("ix_test_runs_task_id", table_name="test_runs")
    op.drop_index("ix_test_runs_capability_id", table_name="test_runs")
    op.drop_table("test_runs")
    op.drop_index("ix_execution_runs_task_started", table_name="execution_runs")
    op.drop_table("execution_runs")
    op.drop_index("ix_installations_capability_id", table_name="installations")
    op.drop_table("installations")
    op.drop_index("ix_capabilities_lifecycle_status", table_name="capabilities")
    op.drop_table("capabilities")
    op.drop_index("ix_finding_evidence_finding_id", table_name="finding_evidence")
    op.drop_table("finding_evidence")
    op.drop_index("ix_research_findings_objective_id", table_name="research_findings")
    op.drop_index("ix_research_findings_status", table_name="research_findings")
    op.drop_table("research_findings")
    op.drop_index("ix_task_dependencies_depends_on", table_name="task_dependencies")
    op.drop_table("task_dependencies")
    op.drop_index("ix_agent_heartbeats_agent_observed", table_name="agent_heartbeats")
    op.drop_table("agent_heartbeats")
    op.drop_table("agents")
