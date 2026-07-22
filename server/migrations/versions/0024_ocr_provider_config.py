"""Add organization OCR provider configuration."""

import sqlalchemy as sa
from alembic import op


revision = "0024_ocr_provider_config"
down_revision = "0023_workflow_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ocr_provider_configs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("base_url", sa.String(2048), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("encrypted_api_key", sa.LargeBinary(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_test_status", sa.String(20), nullable=True),
        sa.Column("last_test_error_code", sa.String(64), nullable=True),
        sa.Column("last_test_latency_ms", sa.Integer(), nullable=True),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id"),
        sa.UniqueConstraint("organization_id", "id"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "created_by"], ["users.organization_id", "users.id"]),
        sa.ForeignKeyConstraint(["organization_id", "updated_by"], ["users.organization_id", "users.id"]),
        sa.CheckConstraint("version >= 1", name="ck_ocr_provider_configs_version"),
        sa.CheckConstraint("not enabled or encrypted_api_key is not null", name="ck_ocr_provider_configs_enabled_key"),
        sa.CheckConstraint(
            "last_test_status is null or last_test_status in ('succeeded','failed')",
            name="ck_ocr_provider_configs_test_status",
        ),
        sa.CheckConstraint(
            "last_test_latency_ms is null or last_test_latency_ms >= 0",
            name="ck_ocr_provider_configs_latency",
        ),
    )


def downgrade() -> None:
    op.drop_table("ocr_provider_configs")
