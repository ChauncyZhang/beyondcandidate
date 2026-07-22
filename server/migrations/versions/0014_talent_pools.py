"""persist talent pools and allow cross-job application reactivation

Revision ID: 0014_talent_pools
Revises: 0013_interview_calendar_contacts
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0014_talent_pools"
down_revision = "0013_interview_calendar_contacts"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP TRIGGER applications_source_valid ON applications")
    op.execute("DROP FUNCTION validate_application_source()")
    op.create_unique_constraint(
        "uq_applications_tenant_id_candidate",
        "applications",
        ["organization_id", "id", "candidate_id"],
    )
    op.drop_constraint(
        "applications_organization_id_source_application_id_candida_fkey",
        "applications",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_applications_source_candidate",
        "applications",
        "applications",
        ["organization_id", "source_application_id", "candidate_id"],
        ["organization_id", "id", "candidate_id"],
    )
    op.execute(
        """
        CREATE FUNCTION validate_application_source() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.source_application_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM applications source
                WHERE source.organization_id = NEW.organization_id
                  AND source.id = NEW.source_application_id
                  AND source.candidate_id = NEW.candidate_id
                  AND source.stage IN ('hired','rejected','withdrawn')
            ) THEN
                RAISE EXCEPTION 'source application must belong to the candidate and be terminal' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER applications_source_valid "
        "BEFORE INSERT OR UPDATE OF source_application_id, candidate_id, organization_id ON applications "
        "FOR EACH ROW EXECUTE FUNCTION validate_application_source()"
    )

    op.create_table(
        "talent_pools",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("visibility", sa.String(32), nullable=False, server_default="recruiting_team"),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("suitable_roles", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="730"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "name", name="uq_talent_pools_tenant_name"),
        sa.CheckConstraint("visibility in ('private','recruiting_team','granted')", name="ck_talent_pools_visibility"),
        sa.CheckConstraint("retention_days between 30 and 3650", name="ck_talent_pools_retention"),
        sa.CheckConstraint("version >= 1", name="ck_talent_pools_version"),
        sa.ForeignKeyConstraint(["organization_id", "owner_id"], ["users.organization_id", "users.id"]),
    )
    op.create_index("ix_talent_pools_tenant_updated", "talent_pools", ["organization_id", "updated_at", "id"])

    op.create_table(
        "talent_pool_grants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("pool_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("access_role", sa.String(20), nullable=False, server_default="viewer"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", "pool_id", "user_id", name="uq_talent_pool_grant_user"),
        sa.CheckConstraint("access_role in ('viewer','manager')", name="ck_talent_pool_grants_access"),
        sa.ForeignKeyConstraint(["organization_id", "pool_id"], ["talent_pools.organization_id", "talent_pools.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "user_id"], ["users.organization_id", "users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_talent_pool_grants_tenant_user", "talent_pool_grants", ["organization_id", "user_id", "pool_id"])

    op.create_table(
        "talent_pool_memberships",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("pool_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("source_application_id", sa.Uuid()),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("suitable_roles", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("tags", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("next_contact_at", sa.DateTime(timezone=True)),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "pool_id", "candidate_id", name="uq_talent_pool_membership_candidate"),
        sa.CheckConstraint("status in ('active','do_not_contact','blocked')", name="ck_talent_pool_memberships_status"),
        sa.CheckConstraint("version >= 1", name="ck_talent_pool_memberships_version"),
        sa.ForeignKeyConstraint(["organization_id", "pool_id"], ["talent_pools.organization_id", "talent_pools.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "candidate_id"], ["candidates.organization_id", "candidates.id"]),
        sa.ForeignKeyConstraint(
            ["organization_id", "source_application_id", "candidate_id"],
            ["applications.organization_id", "applications.id", "applications.candidate_id"],
        ),
        sa.ForeignKeyConstraint(["organization_id", "owner_id"], ["users.organization_id", "users.id"]),
    )
    op.create_index("ix_talent_memberships_tenant_pool_updated", "talent_pool_memberships", ["organization_id", "pool_id", "updated_at", "id"])
    op.create_index("ix_talent_memberships_tenant_followup", "talent_pool_memberships", ["organization_id", "next_contact_at"])
    op.create_index("ix_talent_memberships_tenant_retention", "talent_pool_memberships", ["organization_id", "retention_until"])


def downgrade():
    op.drop_table("talent_pool_memberships")
    op.drop_table("talent_pool_grants")
    op.drop_table("talent_pools")

    op.execute("DROP TRIGGER applications_source_valid ON applications")
    op.execute("DROP FUNCTION validate_application_source()")
    op.drop_constraint("fk_applications_source_candidate", "applications", type_="foreignkey")
    op.drop_constraint("uq_applications_tenant_id_candidate", "applications", type_="unique")
    # Revision 0013 cannot represent cross-job source links. Preserve that
    # provenance in the application history before clearing the incompatible FK.
    op.execute(
        """
        INSERT INTO application_stage_events(
            id, organization_id, application_id, actor_user_id,
            event_type, payload, created_at
        )
        SELECT
            gen_random_uuid(), target.organization_id, target.id, target.owner_id,
            'application.source_detached_for_downgrade',
            jsonb_build_object(
                'source_application_id', target.source_application_id::text,
                'reason', 'cross_job_source_not_supported_before_0014'
            ),
            now()
        FROM applications target
        JOIN applications source
          ON source.organization_id = target.organization_id
         AND source.id = target.source_application_id
        WHERE source.job_id <> target.job_id
        """
    )
    op.execute(
        """
        UPDATE applications target
        SET source_application_id = NULL
        FROM applications source
        WHERE source.organization_id = target.organization_id
          AND source.id = target.source_application_id
          AND source.job_id <> target.job_id
        """
    )
    op.create_foreign_key(
        "applications_organization_id_source_application_id_candida_fkey",
        "applications",
        "applications",
        ["organization_id", "source_application_id", "candidate_id", "job_id"],
        ["organization_id", "id", "candidate_id", "job_id"],
    )
    op.execute(
        """
        CREATE FUNCTION validate_application_source() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.source_application_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM applications source
                WHERE source.organization_id = NEW.organization_id
                  AND source.id = NEW.source_application_id
                  AND source.candidate_id = NEW.candidate_id
                  AND source.job_id = NEW.job_id
                  AND source.stage IN ('hired','rejected','withdrawn')
            ) THEN
                RAISE EXCEPTION 'source application must match aggregate and be terminal' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER applications_source_valid "
        "BEFORE INSERT OR UPDATE OF source_application_id, candidate_id, job_id, organization_id ON applications "
        "FOR EACH ROW EXECUTE FUNCTION validate_application_source()"
    )
