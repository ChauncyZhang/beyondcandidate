"""Add encrypted onboarding records and Feishu approval configuration.

Revision ID: 0040_feishu_onboarding
Revises: 0039_email_delivery_cancelled
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0040_feishu_onboarding"
down_revision = "0039_email_delivery_cancelled"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "feishu_onboarding_configs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("organization_id", UUID, nullable=False, unique=True),
        sa.Column("approval_code", sa.String(255), nullable=False),
        sa.Column("field_mapping", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("validation_status", sa.String(16), nullable=False, server_default="unvalidated"),
        sa.Column("validated_at", sa.DateTime(timezone=True)),
        sa.Column("validation_safe_error_code", sa.String(100)),
        sa.Column("definition_fingerprint", sa.String(64)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("updated_by", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "created_by"], ["users.organization_id", "users.id"]),
        sa.ForeignKeyConstraint(["organization_id", "updated_by"], ["users.organization_id", "users.id"]),
        sa.CheckConstraint("version >= 1", name="ck_feishu_onboarding_configs_version"),
        sa.CheckConstraint("validation_status in ('unvalidated','valid','invalid')", name="ck_feishu_onboarding_configs_validation"),
        sa.CheckConstraint("not enabled or validation_status = 'valid'", name="ck_feishu_onboarding_configs_enabled"),
    )
    op.create_table(
        "feishu_department_mappings",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("department_id", UUID, nullable=False),
        sa.Column("feishu_department_id", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "department_id"], ["departments.organization_id", "departments.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "department_id"),
    )
    op.create_table(
        "onboarding_records",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("offer_response_id", UUID, nullable=False),
        sa.Column("offer_id", UUID, nullable=False),
        sa.Column("application_id", UUID, nullable=False),
        sa.Column("candidate_id", UUID, nullable=False),
        sa.Column("job_id", UUID, nullable=False),
        sa.Column("department_id", UUID, nullable=False),
        sa.Column("job_title", sa.String(200), nullable=False),
        sa.Column("department_name", sa.String(200), nullable=False),
        sa.Column("expected_start_date", sa.Date(), nullable=False),
        sa.Column("pii_schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("pii_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="ready"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("generation", UUID),
        sa.Column("started_by", UUID),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("safe_error_code", sa.String(100)),
        sa.Column("feishu_instance_code", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "offer_response_id"),
        sa.UniqueConstraint("organization_id", "application_id"),
        sa.ForeignKeyConstraint(["organization_id", "offer_response_id"], ["offer_responses.organization_id", "offer_responses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "offer_id"], ["offers.organization_id", "offers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "application_id", "candidate_id", "job_id"], ["applications.organization_id", "applications.id", "applications.candidate_id", "applications.job_id"]),
        sa.ForeignKeyConstraint(["organization_id", "job_id"], ["jobs.organization_id", "jobs.id"]),
        sa.ForeignKeyConstraint(["organization_id", "department_id"], ["departments.organization_id", "departments.id"]),
        sa.ForeignKeyConstraint(["organization_id", "started_by"], ["users.organization_id", "users.id"]),
        sa.CheckConstraint("status in ('ready','submitting','submitted','failed')", name="ck_onboarding_records_status"),
        sa.CheckConstraint("version >= 1 and pii_schema_version = 1", name="ck_onboarding_records_version"),
        sa.CheckConstraint(
            "(status = 'ready' and generation is null and started_by is null and started_at is null and submitted_at is null) or "
            "(status in ('submitting','failed') and generation is not null and started_by is not null and started_at is not null and submitted_at is null) or "
            "(status = 'submitted' and generation is not null and started_by is not null and started_at is not null and submitted_at is not null and feishu_instance_code is not null)",
            name="ck_onboarding_records_lifecycle",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION delete_candidate_onboarding_records()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL THEN
            DELETE FROM public.onboarding_records
            WHERE organization_id = NEW.organization_id
              AND candidate_id = NEW.id;
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_delete_candidate_onboarding_records
        AFTER UPDATE OF deleted_at ON candidates
        FOR EACH ROW
        EXECUTE FUNCTION delete_candidate_onboarding_records()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_delete_candidate_onboarding_records ON candidates")
    op.execute("DROP FUNCTION IF EXISTS delete_candidate_onboarding_records()")
    op.drop_table("onboarding_records")
    op.drop_table("feishu_department_mappings")
    op.drop_table("feishu_onboarding_configs")
