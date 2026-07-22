"""Persist versioned resume profiles and remove them during candidate redaction."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0025_resume_profiles"
down_revision = "0024_ocr_provider_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "resume_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("resume_id", sa.Uuid(), nullable=False),
        sa.Column("data", json_document, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("profile_version", sa.String(64), nullable=False),
        sa.Column("safe_error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["organization_id", "resume_id"],
            ["resumes.organization_id", "resumes.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "resume_id"),
        sa.CheckConstraint("status in ('ready','partial','unavailable')", name="ck_resume_profiles_status"),
        sa.CheckConstraint("source in ('rules','llm','ocr_rules','ocr_llm')", name="ck_resume_profiles_source"),
    )
    op.execute(
        """
        CREATE FUNCTION delete_resume_profile_on_redaction() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
          IF OLD.parsed_text IS NOT NULL AND NEW.parsed_text IS NULL THEN
            DELETE FROM public.resume_profiles
            WHERE organization_id = OLD.organization_id AND resume_id = OLD.id;
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER resumes_delete_profile_on_redaction
        AFTER UPDATE OF parsed_text ON resumes
        FOR EACH ROW EXECUTE FUNCTION delete_resume_profile_on_redaction()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER resumes_delete_profile_on_redaction ON resumes")
    op.execute("DROP FUNCTION delete_resume_profile_on_redaction()")
    op.drop_table("resume_profiles")
