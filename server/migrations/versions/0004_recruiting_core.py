"""recruiting core

Revision ID: 0004_recruiting_core
Revises: 0003_identity_security_hardening
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_recruiting_core"
down_revision = "0003_identity_security_hardening"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("department_id", sa.Uuid(), nullable=True))
    op.add_column("jobs", sa.Column("headcount", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("jobs", sa.Column("priority", sa.String(16), nullable=False, server_default="normal"))
    op.add_column("jobs", sa.Column("hiring_owner_id", sa.Uuid(), nullable=True))
    op.add_column("jobs", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.create_check_constraint("ck_jobs_headcount", "jobs", "headcount > 0")
    op.create_foreign_key("fk_jobs_department_tenant", "jobs", "departments", ["organization_id", "department_id"], ["organization_id", "id"])
    op.create_foreign_key("fk_jobs_hiring_owner_tenant", "jobs", "users", ["organization_id", "hiring_owner_id"], ["organization_id", "id"])

    def common():
        return [sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("organization_id", sa.Uuid(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())]
    op.create_table("candidates", *common(), sa.Column("display_name", sa.String(200), nullable=False), sa.Column("current_title", sa.String(200)), sa.Column("location", sa.String(200)), sa.Column("owner_id", sa.Uuid()), sa.Column("version", sa.Integer(), nullable=False, server_default="1"), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), sa.UniqueConstraint("organization_id", "id"), sa.ForeignKeyConstraint(["organization_id", "owner_id"], ["users.organization_id", "users.id"]))
    op.create_table("candidate_contacts", *common(), sa.Column("candidate_id", sa.Uuid(), nullable=False), sa.Column("kind", sa.String(20), nullable=False), sa.Column("ciphertext", sa.LargeBinary(), nullable=False), sa.Column("lookup_hash", sa.String(64), nullable=False), sa.Column("masked_value", sa.String(320), nullable=False), sa.UniqueConstraint("organization_id", "kind", "lookup_hash"), sa.ForeignKeyConstraint(["organization_id", "candidate_id"], ["candidates.organization_id", "candidates.id"], ondelete="CASCADE"))
    op.create_table("file_objects", *common(), sa.Column("storage_key", sa.String(512), nullable=False), sa.Column("original_filename", sa.String(255), nullable=False), sa.Column("mime_type", sa.String(100), nullable=False), sa.Column("size_bytes", sa.BigInteger(), nullable=False), sa.Column("sha256", sa.String(64), nullable=False), sa.Column("uploaded_by", sa.Uuid(), nullable=False), sa.UniqueConstraint("organization_id", "id"), sa.UniqueConstraint("organization_id", "storage_key"), sa.ForeignKeyConstraint(["organization_id", "uploaded_by"], ["users.organization_id", "users.id"]))
    op.create_table("resumes", *common(), sa.Column("candidate_id", sa.Uuid(), nullable=False), sa.Column("file_object_id", sa.Uuid(), nullable=False), sa.Column("version_number", sa.Integer(), nullable=False), sa.Column("parsed_text", sa.Text()), sa.UniqueConstraint("organization_id", "id"), sa.UniqueConstraint("organization_id", "candidate_id", "version_number"), sa.UniqueConstraint("organization_id", "id", "candidate_id"), sa.ForeignKeyConstraint(["organization_id", "candidate_id"], ["candidates.organization_id", "candidates.id"]), sa.ForeignKeyConstraint(["organization_id", "file_object_id"], ["file_objects.organization_id", "file_objects.id"]))
    for table in ("job_jd_versions", "screening_rule_versions"):
        op.create_table(table, *common(), sa.Column("job_id", sa.Uuid(), nullable=False), sa.Column("version_number", sa.Integer(), nullable=False), sa.Column("content", postgresql.JSONB(), nullable=False), sa.Column("created_by", sa.Uuid(), nullable=False), sa.UniqueConstraint("organization_id", "job_id", "version_number"), sa.ForeignKeyConstraint(["organization_id", "job_id"], ["jobs.organization_id", "jobs.id"]), sa.ForeignKeyConstraint(["organization_id", "created_by"], ["users.organization_id", "users.id"]))
    op.create_table("applications", *common(), sa.Column("candidate_id", sa.Uuid(), nullable=False), sa.Column("job_id", sa.Uuid(), nullable=False), sa.Column("resume_id", sa.Uuid(), nullable=False), sa.Column("source_application_id", sa.Uuid()), sa.Column("owner_id", sa.Uuid(), nullable=False), sa.Column("stage", sa.String(32), nullable=False, server_default="new"), sa.Column("source", sa.String(64), nullable=False, server_default="manual"), sa.Column("human_conclusion", sa.Text()), sa.Column("version", sa.Integer(), nullable=False, server_default="1"), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), sa.UniqueConstraint("organization_id", "id"), sa.UniqueConstraint("organization_id", "id", "candidate_id", "job_id"), sa.ForeignKeyConstraint(["organization_id", "candidate_id"], ["candidates.organization_id", "candidates.id"]), sa.ForeignKeyConstraint(["organization_id", "job_id"], ["jobs.organization_id", "jobs.id"]), sa.ForeignKeyConstraint(["organization_id", "resume_id", "candidate_id"], ["resumes.organization_id", "resumes.id", "resumes.candidate_id"]), sa.ForeignKeyConstraint(["organization_id", "source_application_id", "candidate_id", "job_id"], ["applications.organization_id", "applications.id", "applications.candidate_id", "applications.job_id"]), sa.ForeignKeyConstraint(["organization_id", "owner_id"], ["users.organization_id", "users.id"]), sa.CheckConstraint("stage in ('new','review','contact','interview_pending','interviewing','decision','passed','hired','rejected','withdrawn')"))
    op.create_index("uq_applications_active", "applications", ["organization_id", "candidate_id", "job_id"], unique=True, postgresql_where=sa.text("stage not in ('hired','rejected','withdrawn')"))
    op.execute("""
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
    """)
    op.execute("CREATE TRIGGER applications_source_valid BEFORE INSERT OR UPDATE OF source_application_id, candidate_id, job_id, organization_id ON applications FOR EACH ROW EXECUTE FUNCTION validate_application_source()")
    for table, parent, fk in (("application_stage_events", "applications", "application_id"), ("candidate_notes", "candidates", "candidate_id"), ("candidate_events", "candidates", "candidate_id")):
        columns = common() + [sa.Column(fk, sa.Uuid(), nullable=False), sa.Column("actor_user_id", sa.Uuid(), nullable=False), sa.Column("event_type", sa.String(64), nullable=False), sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}")]
        op.create_table(table, *columns, sa.ForeignKeyConstraint(["organization_id", fk], [f"{parent}.organization_id", f"{parent}.id"]), sa.ForeignKeyConstraint(["organization_id", "actor_user_id"], ["users.organization_id", "users.id"]))
    op.create_table("download_tickets", *common(), sa.Column("token_hash", sa.String(64), nullable=False, unique=True), sa.Column("user_id", sa.Uuid(), nullable=False), sa.Column("resume_id", sa.Uuid(), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("consumed_at", sa.DateTime(timezone=True)), sa.ForeignKeyConstraint(["organization_id", "user_id"], ["users.organization_id", "users.id"]), sa.ForeignKeyConstraint(["organization_id", "resume_id"], ["resumes.organization_id", "resumes.id"]))
    op.create_table("idempotency_records", *common(), sa.Column("user_id", sa.Uuid(), nullable=False), sa.Column("operation", sa.String(64), nullable=False), sa.Column("idempotency_key", sa.String(255), nullable=False), sa.Column("request_hash", sa.String(64), nullable=False), sa.Column("status_code", sa.Integer(), nullable=False), sa.Column("response_json", postgresql.JSONB(), nullable=False), sa.UniqueConstraint("organization_id", "user_id", "operation", "idempotency_key"), sa.ForeignKeyConstraint(["organization_id", "user_id"], ["users.organization_id", "users.id"]))
    for table in ("job_jd_versions", "screening_rule_versions", "resumes", "application_stage_events", "candidate_notes", "candidate_events"):
        op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_audit_log_mutation()")


def downgrade():
    for table in ("idempotency_records", "download_tickets", "candidate_events", "candidate_notes", "application_stage_events", "applications", "screening_rule_versions", "job_jd_versions", "resumes", "file_objects", "candidate_contacts", "candidates"):
        op.drop_table(table)
    op.execute("DROP FUNCTION validate_application_source()")
    op.drop_constraint("fk_jobs_hiring_owner_tenant", "jobs", type_="foreignkey")
    op.drop_constraint("fk_jobs_department_tenant", "jobs", type_="foreignkey")
    op.drop_constraint("ck_jobs_headcount", "jobs", type_="check")
    for column in ("version", "hiring_owner_id", "priority", "headcount", "department_id"):
        op.drop_column("jobs", column)
