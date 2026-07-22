"""Persist per-user versions of read workbench notifications."""

import sqlalchemy as sa
from alembic import op


revision = "0026_notification_reads"
down_revision = "0025_resume_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_reads",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("notification_version", sa.String(64), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["users.organization_id", "users.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "application_id"],
            ["applications.organization_id", "applications.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            "application_id",
            name="uq_notification_reads_user_application",
        ),
    )
    op.create_index(
        "ix_notification_reads_user",
        "notification_reads",
        ["organization_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_reads_user", table_name="notification_reads")
    op.drop_table("notification_reads")
