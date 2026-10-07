"""Phase 1 event store: runs and append-only events.

Revision ID: 0001
Revises:
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept inline (not imported from app code) so this migration stays a fixed snapshot.
REJECT_FUNCTION = """
CREATE OR REPLACE FUNCTION nexus_reject_event_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING MESSAGE = 'events are append-only: ' || TG_OP || ' is not allowed';
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "last_sequence >= 0", name=op.f("ck_runs_last_sequence_non_negative")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("agent_id", sa.String(length=128), nullable=True),
        sa.Column("task_id", sa.String(length=128), nullable=True),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.CheckConstraint("sequence >= 1", name=op.f("ck_events_sequence_positive")),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_events_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.UniqueConstraint("run_id", "sequence", name=op.f("uq_events_run_id_sequence")),
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(REJECT_FUNCTION)
        op.execute(
            "CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE ON events "
            "FOR EACH ROW EXECUTE FUNCTION nexus_reject_event_mutation()"
        )
        op.execute(
            "CREATE TRIGGER events_no_truncate BEFORE TRUNCATE ON events "
            "FOR EACH STATEMENT EXECUTE FUNCTION nexus_reject_event_mutation()"
        )


def downgrade() -> None:
    op.drop_table("events")  # drops its triggers too
    op.drop_table("runs")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS nexus_reject_event_mutation()")
