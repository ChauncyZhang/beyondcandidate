"""Enforce tenant relationships and append-only audit logs."""

from alembic import op


revision = "0003_identity_security_hardening"
down_revision = "0002_identity_boundary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_departments_org_id", "departments", ["organization_id", "id"])
    op.create_unique_constraint("uq_users_org_id", "users", ["organization_id", "id"])
    op.create_unique_constraint("uq_jobs_org_id", "jobs", ["organization_id", "id"])

    op.drop_constraint("departments_parent_id_fkey", "departments", type_="foreignkey")
    op.create_foreign_key("fk_departments_parent_same_org", "departments", "departments", ["organization_id", "parent_id"], ["organization_id", "id"])
    op.drop_constraint("users_department_id_fkey", "users", type_="foreignkey")
    op.create_foreign_key("fk_users_department_same_org", "users", "departments", ["organization_id", "department_id"], ["organization_id", "id"])
    op.drop_constraint("jobs_owner_id_fkey", "jobs", type_="foreignkey")
    op.create_foreign_key("fk_jobs_owner_same_org", "jobs", "users", ["organization_id", "owner_id"], ["organization_id", "id"])
    op.drop_constraint("job_collaborators_job_id_fkey", "job_collaborators", type_="foreignkey")
    op.drop_constraint("job_collaborators_user_id_fkey", "job_collaborators", type_="foreignkey")
    op.create_foreign_key("fk_collaborators_job_same_org", "job_collaborators", "jobs", ["organization_id", "job_id"], ["organization_id", "id"], ondelete="CASCADE")
    op.create_foreign_key("fk_collaborators_user_same_org", "job_collaborators", "users", ["organization_id", "user_id"], ["organization_id", "id"], ondelete="CASCADE")
    op.drop_constraint("user_sessions_user_id_fkey", "user_sessions", type_="foreignkey")
    op.create_foreign_key("fk_sessions_user_same_org", "user_sessions", "users", ["organization_id", "user_id"], ["organization_id", "id"], ondelete="CASCADE")

    op.execute("""
        CREATE FUNCTION reject_audit_log_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_logs are append-only' USING ERRCODE = '55000';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER audit_logs_append_only
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION reject_audit_log_mutation()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_logs_append_only ON audit_logs")
    op.execute("DROP FUNCTION reject_audit_log_mutation()")
    op.drop_constraint("fk_sessions_user_same_org", "user_sessions", type_="foreignkey")
    op.create_foreign_key("user_sessions_user_id_fkey", "user_sessions", "users", ["user_id"], ["id"], ondelete="CASCADE")
    op.drop_constraint("fk_collaborators_user_same_org", "job_collaborators", type_="foreignkey")
    op.drop_constraint("fk_collaborators_job_same_org", "job_collaborators", type_="foreignkey")
    op.create_foreign_key("job_collaborators_user_id_fkey", "job_collaborators", "users", ["user_id"], ["id"], ondelete="CASCADE")
    op.create_foreign_key("job_collaborators_job_id_fkey", "job_collaborators", "jobs", ["job_id"], ["id"], ondelete="CASCADE")
    op.drop_constraint("fk_jobs_owner_same_org", "jobs", type_="foreignkey")
    op.create_foreign_key("jobs_owner_id_fkey", "jobs", "users", ["owner_id"], ["id"])
    op.drop_constraint("fk_users_department_same_org", "users", type_="foreignkey")
    op.create_foreign_key("users_department_id_fkey", "users", "departments", ["department_id"], ["id"])
    op.drop_constraint("fk_departments_parent_same_org", "departments", type_="foreignkey")
    op.create_foreign_key("departments_parent_id_fkey", "departments", "departments", ["parent_id"], ["id"])
    op.drop_constraint("uq_jobs_org_id", "jobs", type_="unique")
    op.drop_constraint("uq_users_org_id", "users", type_="unique")
    op.drop_constraint("uq_departments_org_id", "departments", type_="unique")
