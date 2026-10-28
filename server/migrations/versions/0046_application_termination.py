"""Persist application stop intent and external cleanup facts without deleting history."""

from alembic import op
import sqlalchemy as sa


revision = "0046_application_termination"
down_revision = "0045_member_offboarding"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "application_terminations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("source_stage", sa.String(32), nullable=False),
        sa.Column("reason_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("terminated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("external_status", sa.String(16), nullable=False, server_default="resolved"),
        sa.Column("external_items", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("resolution_history", sa.JSON(), nullable=False, server_default="[]"),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "application_id"),
        sa.ForeignKeyConstraint(["organization_id", "application_id", "candidate_id", "job_id"], ["applications.organization_id", "applications.id", "applications.candidate_id", "applications.job_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "actor_user_id"], ["users.organization_id", "users.id"]),
        sa.CheckConstraint("version >= 1", name="ck_application_terminations_version"),
        sa.CheckConstraint("length(trim(reason_text)) between 1 and 2000", name="ck_application_terminations_reason"),
        sa.CheckConstraint("external_status in ('pending','resolved')", name="ck_application_terminations_external_status"),
    )
    op.create_index("ix_application_terminations_pending", "application_terminations", ["organization_id", "external_status", "terminated_at"])
    op.drop_constraint("ck_offer_approvals_status", "offer_approvals", type_="check")
    op.create_check_constraint("ck_offer_approvals_status", "offer_approvals", "status in ('waiting','pending','approved','rejected','cancelled')")
    op.execute("""
        CREATE OR REPLACE FUNCTION protect_offer_approval_history() RETURNS trigger LANGUAGE plpgsql AS $$
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
             AND NEW.reason IS NULL AND NEW.decided_at IS NULL THEN RETURN NEW; END IF;
          IF OLD.status = 'pending' AND NEW.status = 'approved'
             AND NEW.reason IS NULL AND NEW.decided_at IS NOT NULL THEN RETURN NEW; END IF;
          IF OLD.status = 'pending' AND NEW.status = 'rejected'
             AND NEW.reason IS NOT NULL AND length(trim(NEW.reason)) > 0
             AND NEW.decided_at IS NOT NULL THEN RETURN NEW; END IF;
          IF OLD.status IN ('waiting','pending') AND NEW.status = 'cancelled'
             AND NEW.reason IS NULL AND NEW.decided_at IS NULL THEN RETURN NEW; END IF;
          RAISE EXCEPTION 'illegal offer approval transition';
        END $$
    """)
    op.execute("""
        CREATE FUNCTION delete_candidate_termination_records() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL THEN
            DELETE FROM public.application_terminations
            WHERE organization_id = NEW.organization_id AND candidate_id = NEW.id;
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER trg_delete_candidate_termination_records
        AFTER UPDATE OF deleted_at ON candidates FOR EACH ROW
        EXECUTE FUNCTION delete_candidate_termination_records()
    """)
    op.execute("""
        DO $$
        DECLARE grant_row record;
        BEGIN
          FOR grant_row IN
            SELECT DISTINCT r.rolname, a.privilege_type
            FROM pg_class c
            CROSS JOIN LATERAL aclexplode(c.relacl) a
            JOIN pg_roles r ON r.oid = a.grantee
            WHERE c.oid = 'public.applications'::regclass
              AND a.privilege_type IN ('SELECT','INSERT','UPDATE','DELETE')
          LOOP
            EXECUTE format('GRANT %s ON TABLE public.application_terminations TO %I', grant_row.privilege_type, grant_row.rolname);
          END LOOP;
        END $$
    """)


def downgrade():
    raise RuntimeError("Forward-only migration; retain application termination history during rollback.")
