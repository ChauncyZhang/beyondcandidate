"""Persist controlled report exports and one-time download tickets.

Revision ID: 0015_reports_exports
Revises: 0014_talent_pools
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0015_reports_exports"
down_revision = "0014_talent_pools"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "report_exports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("background_job_id", sa.Uuid(), nullable=False),
        sa.Column("format", sa.String(16), nullable=False, server_default="csv"),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("filters", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("object_key", sa.String(512)),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("safe_error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "background_job_id", name="uq_report_exports_background_job"),
        sa.CheckConstraint("format = 'csv'", name="ck_report_exports_format"),
        sa.CheckConstraint("status in ('queued','running','succeeded','failed')", name="ck_report_exports_status"),
        sa.CheckConstraint("row_count >= 0", name="ck_report_exports_row_count"),
        sa.CheckConstraint(
            "(status = 'succeeded' and object_key is not null and completed_at is not null) or (status <> 'succeeded')",
            name="ck_report_exports_artifact",
        ),
        sa.ForeignKeyConstraint(["organization_id", "requested_by"], ["users.organization_id", "users.id"]),
        sa.ForeignKeyConstraint(
            ["organization_id", "background_job_id"],
            ["background_jobs.organization_id", "background_jobs.id"],
        ),
    )
    op.create_index(
        "ix_report_exports_requester_created",
        "report_exports",
        ["organization_id", "requested_by", "created_at"],
    )
    op.create_table(
        "report_export_download_tickets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("export_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["organization_id", "export_id"],
            ["report_exports.organization_id", "report_exports.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["organization_id", "user_id"], ["users.organization_id", "users.id"]),
    )
    op.create_index(
        "ix_report_export_tickets_expiry",
        "report_export_download_tickets",
        ["organization_id", "expires_at"],
    )


def downgrade():
    op.drop_table("report_export_download_tickets")
    op.drop_table("report_exports")
