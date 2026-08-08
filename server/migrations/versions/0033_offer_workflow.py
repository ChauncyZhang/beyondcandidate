"""Add versioned offer workflow foundation.

Revision ID: 0033_offer_workflow
Revises: 0032_interview_email_attachment
"""

from alembic import op
import sqlalchemy as sa


revision = "0033_offer_workflow"
down_revision = "0032_interview_email_attachment"
branch_labels = None
depends_on = None


def _record_columns():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade() -> None:
    op.create_table(
        "offer_templates",
        *_record_columns(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "name"),
        sa.CheckConstraint("status in ('active','inactive')", name="ck_offer_templates_status"),
        sa.CheckConstraint("version >= 1", name="ck_offer_templates_version"),
    )
    op.add_column("jobs", sa.Column("offer_approver_id", sa.Uuid()))
    op.add_column("jobs", sa.Column("offer_template_id", sa.Uuid()))
    op.create_foreign_key("fk_jobs_offer_approver", "jobs", "users", ["organization_id", "offer_approver_id"], ["organization_id", "id"])
    op.create_foreign_key("fk_jobs_offer_template", "jobs", "offer_templates", ["organization_id", "offer_template_id"], ["organization_id", "id"])
    op.create_unique_constraint("uq_applications_tenant_id_job", "applications", ["organization_id", "id", "job_id"])
    op.create_table(
        "organization_special_offer_approvers",
        *_record_columns(),
        sa.Column("approver_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "position"),
        sa.UniqueConstraint("organization_id", "approver_id"),
        sa.CheckConstraint("position >= 1", name="ck_organization_special_offer_approvers_position"),
        sa.ForeignKeyConstraint(["organization_id", "approver_id"], ["users.organization_id", "users.id"]),
    )
    op.create_table(
        "offers",
        *_record_columns(),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid()),
        sa.Column("current_version_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("is_special", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("special_reason", sa.Text()),
        sa.Column("candidate_response_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("organization_id", "id"),
        sa.CheckConstraint("status in ('draft','pending_approval','changes_requested','ready_to_send','sent','withdrawn','expired')", name="ck_offers_status"),
        sa.CheckConstraint("version >= 1", name="ck_offers_version"),
        sa.CheckConstraint("(is_special and special_reason is not null and length(trim(special_reason)) > 0) or (not is_special and special_reason is null)", name="ck_offers_special_reason"),
        sa.ForeignKeyConstraint(["organization_id", "application_id", "job_id"], ["applications.organization_id", "applications.id", "applications.job_id"]),
        sa.ForeignKeyConstraint(["organization_id", "job_id"], ["jobs.organization_id", "jobs.id"]),
        sa.ForeignKeyConstraint(["organization_id", "template_id"], ["offer_templates.organization_id", "offer_templates.id"]),
    )
    op.create_index("ix_offers_expiry", "offers", ["organization_id", "status", "candidate_response_deadline"])
    op.create_index(
        "uq_offers_active_application",
        "offers",
        ["organization_id", "application_id"],
        unique=True,
        postgresql_where=sa.text("status in ('draft','pending_approval','changes_requested','ready_to_send','sent')"),
    )
    op.create_table(
        "offer_versions",
        *_record_columns(),
        sa.Column("offer_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("template_id", sa.Uuid()),
        sa.Column("candidate_response_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_special", sa.Boolean(), nullable=False),
        sa.Column("special_reason", sa.Text()),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "offer_id", "version_number"),
        sa.UniqueConstraint("organization_id", "id", "offer_id"),
        sa.UniqueConstraint("organization_id", "offer_id", "id", "version_number"),
        sa.ForeignKeyConstraint(["organization_id", "offer_id"], ["offers.organization_id", "offers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "created_by"], ["users.organization_id", "users.id"]),
        sa.ForeignKeyConstraint(["organization_id", "template_id"], ["offer_templates.organization_id", "offer_templates.id"]),
    )
    op.create_foreign_key("fk_offers_current_version", "offers", "offer_versions", ["organization_id", "current_version_id", "id"], ["organization_id", "id", "offer_id"], deferrable=True, initially="DEFERRED")
    op.create_table(
        "offer_approvals",
        *_record_columns(),
        sa.Column("offer_id", sa.Uuid(), nullable=False),
        sa.Column("offer_version_id", sa.Uuid(), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("assignee_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "offer_id", "round_number", "sequence"),
        sa.CheckConstraint("round_number >= 1 and version_number >= 1 and sequence >= 1", name="ck_offer_approvals_order"),
        sa.CheckConstraint("status in ('waiting','pending','approved','rejected')", name="ck_offer_approvals_status"),
        sa.CheckConstraint("status != 'rejected' or (reason is not null and length(trim(reason)) > 0)", name="ck_offer_approvals_rejection_reason"),
        sa.ForeignKeyConstraint(["organization_id", "offer_id"], ["offers.organization_id", "offers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "offer_id", "offer_version_id", "version_number"], ["offer_versions.organization_id", "offer_versions.offer_id", "offer_versions.id", "offer_versions.version_number"]),
        sa.ForeignKeyConstraint(["organization_id", "assignee_id"], ["users.organization_id", "users.id"]),
    )
    op.create_table(
        "offer_responses",
        *_record_columns(),
        sa.Column("offer_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "offer_id"),
        sa.CheckConstraint("status in ('accepted','declined')", name="ck_offer_responses_status"),
        sa.ForeignKeyConstraint(["organization_id", "offer_id"], ["offers.organization_id", "offers.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "offer_events",
        *_record_columns(),
        sa.Column("offer_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid()),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.UniqueConstraint("organization_id", "id"),
        sa.ForeignKeyConstraint(["organization_id", "offer_id"], ["offers.organization_id", "offers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "actor_user_id"], ["users.organization_id", "users.id"]),
    )
    op.create_index("ix_offer_events_offer_created", "offer_events", ["organization_id", "offer_id", "created_at"])
    op.execute("""
        CREATE FUNCTION prevent_submitted_offer_version_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' AND OLD.submitted_at IS NOT NULL THEN
            RAISE EXCEPTION 'submitted offer versions are immutable';
          END IF;
          IF TG_OP = 'UPDATE' AND OLD.submitted_at IS NOT NULL THEN
            RAISE EXCEPTION 'submitted offer versions are immutable';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER offer_versions_immutable_after_submission BEFORE UPDATE OR DELETE ON offer_versions FOR EACH ROW EXECUTE FUNCTION prevent_submitted_offer_version_mutation()")
    op.execute("""
        CREATE FUNCTION protect_offer_approval_history() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'offer approval history is immutable';
          END IF;
          IF NEW.id IS DISTINCT FROM OLD.id
             OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
             OR NEW.offer_id IS DISTINCT FROM OLD.offer_id
             OR NEW.offer_version_id IS DISTINCT FROM OLD.offer_version_id
             OR NEW.round_number IS DISTINCT FROM OLD.round_number
             OR NEW.version_number IS DISTINCT FROM OLD.version_number
             OR NEW.sequence IS DISTINCT FROM OLD.sequence
             OR NEW.assignee_id IS DISTINCT FROM OLD.assignee_id
             OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'offer approval identity is immutable';
          END IF;
          IF OLD.status = 'waiting' AND NEW.status = 'pending'
             AND NEW.reason IS NULL AND NEW.decided_at IS NULL THEN
            RETURN NEW;
          END IF;
          IF OLD.status = 'pending' AND NEW.status = 'approved'
             AND NEW.reason IS NULL AND NEW.decided_at IS NOT NULL THEN
            RETURN NEW;
          END IF;
          IF OLD.status = 'pending' AND NEW.status = 'rejected'
             AND NEW.reason IS NOT NULL AND length(trim(NEW.reason)) > 0
             AND NEW.decided_at IS NOT NULL THEN
            RETURN NEW;
          END IF;
          RAISE EXCEPTION 'illegal offer approval transition';
        END $$
    """)
    op.execute("CREATE TRIGGER offer_approvals_history_guard BEFORE UPDATE OR DELETE ON offer_approvals FOR EACH ROW EXECUTE FUNCTION protect_offer_approval_history()")
    op.execute("""
        CREATE FUNCTION protect_offer_response() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'offer responses are immutable';
        END $$
    """)
    op.execute("CREATE TRIGGER offer_responses_immutable BEFORE UPDATE OR DELETE ON offer_responses FOR EACH ROW EXECUTE FUNCTION protect_offer_response()")
    op.execute("""
        CREATE FUNCTION validate_special_offer_approver() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM users u
            WHERE u.organization_id = NEW.organization_id
              AND u.id = NEW.approver_id
              AND u.status = 'active'
              AND EXISTS (
                SELECT 1 FROM user_roles ur
                WHERE ur.user_id = u.id
                  AND ur.role IN ('recruiting_admin','hiring_manager')
              )
          ) THEN
            RAISE EXCEPTION 'special offer approver is not eligible';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER organization_special_offer_approvers_eligible BEFORE INSERT OR UPDATE ON organization_special_offer_approvers FOR EACH ROW EXECUTE FUNCTION validate_special_offer_approver()")
    op.execute("""
        CREATE FUNCTION validate_job_offer_defaults() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.offer_approver_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM users u
            WHERE u.organization_id = NEW.organization_id
              AND u.id = NEW.offer_approver_id
              AND u.status = 'active'
              AND EXISTS (
                SELECT 1 FROM user_roles ur
                WHERE ur.user_id = u.id
                  AND ur.role IN ('recruiting_admin','hiring_manager')
              )
          ) THEN
            RAISE EXCEPTION 'job offer approver is not eligible';
          END IF;
          IF NEW.offer_template_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM offer_templates t
            WHERE t.organization_id = NEW.organization_id
              AND t.id = NEW.offer_template_id
              AND t.status = 'active'
          ) THEN
            RAISE EXCEPTION 'job offer template is not active';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER jobs_offer_defaults_eligible BEFORE INSERT OR UPDATE OF offer_approver_id, offer_template_id ON jobs FOR EACH ROW EXECUTE FUNCTION validate_job_offer_defaults()")


def downgrade() -> None:
    op.execute("DROP TRIGGER jobs_offer_defaults_eligible ON jobs")
    op.execute("DROP FUNCTION validate_job_offer_defaults()")
    op.execute("DROP TRIGGER organization_special_offer_approvers_eligible ON organization_special_offer_approvers")
    op.execute("DROP FUNCTION validate_special_offer_approver()")
    op.execute("DROP TRIGGER offer_responses_immutable ON offer_responses")
    op.execute("DROP FUNCTION protect_offer_response()")
    op.execute("DROP TRIGGER offer_approvals_history_guard ON offer_approvals")
    op.execute("DROP FUNCTION protect_offer_approval_history()")
    op.execute("DROP TRIGGER offer_versions_immutable_after_submission ON offer_versions")
    op.execute("DROP FUNCTION prevent_submitted_offer_version_mutation()")
    op.drop_index("ix_offer_events_offer_created", table_name="offer_events")
    op.drop_table("offer_events")
    op.drop_table("offer_responses")
    op.drop_table("offer_approvals")
    op.drop_constraint("fk_offers_current_version", "offers", type_="foreignkey")
    op.drop_table("offer_versions")
    op.drop_index("uq_offers_active_application", table_name="offers")
    op.drop_index("ix_offers_expiry", table_name="offers")
    op.drop_table("offers")
    op.drop_table("organization_special_offer_approvers")
    op.drop_constraint("fk_jobs_offer_template", "jobs", type_="foreignkey")
    op.drop_constraint("fk_jobs_offer_approver", "jobs", type_="foreignkey")
    op.drop_column("jobs", "offer_template_id")
    op.drop_column("jobs", "offer_approver_id")
    op.drop_table("offer_templates")
    op.drop_constraint("uq_applications_tenant_id_job", "applications", type_="unique")
