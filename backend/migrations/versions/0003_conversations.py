"""Conversations table and run/document foreign keys for chat threads.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )

    op.add_column("runs", sa.Column("conversation_id", sa.Uuid(), nullable=True))
    op.add_column("runs", sa.Column("parent_run_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_runs_conversation_id"), "runs", ["conversation_id"], unique=False)

    op.add_column("documents", sa.Column("conversation_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_documents_conversation_id"), "documents", ["conversation_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_documents_conversation_id"), table_name="documents")
    op.drop_column("documents", "conversation_id")
    op.drop_index(op.f("ix_runs_conversation_id"), table_name="runs")
    op.drop_column("runs", "parent_run_id")
    op.drop_column("runs", "conversation_id")
    op.drop_table("conversations")
