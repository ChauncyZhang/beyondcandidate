"""Phase 1 identity, sessions, roles, and grants."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0002_identity_boundary"
down_revision = "0001_empty_baseline"
branch_labels = None
depends_on = None


def common_columns(with_updated=True):
    columns = [sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)]
    if with_updated:
        columns.append(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    return columns


def upgrade() -> None:
    op.create_table("organizations", *common_columns(), sa.Column("slug", sa.String(100), nullable=False, unique=True), sa.Column("name", sa.String(200), nullable=False), sa.Column("status", sa.String(32), nullable=False))
    op.create_table("departments", *common_columns(), sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False), sa.Column("parent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("departments.id")), sa.Column("name", sa.String(200), nullable=False), sa.UniqueConstraint("organization_id", "parent_id", "name", postgresql_nulls_not_distinct=True))
    op.create_table("users", *common_columns(), sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False), sa.Column("department_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("departments.id")), sa.Column("email", sa.String(320), nullable=False), sa.Column("normalized_email", sa.String(320), nullable=False), sa.Column("display_name", sa.String(200), nullable=False), sa.Column("password_hash", sa.String(512), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("authorization_version", sa.Integer, nullable=False, server_default="1"), sa.Column("failed_login_count", sa.Integer, nullable=False, server_default="0"), sa.Column("failed_login_window_started_at", sa.DateTime(timezone=True)), sa.Column("locked_until", sa.DateTime(timezone=True)), sa.UniqueConstraint("organization_id", "normalized_email"))
    op.create_table("user_roles", *common_columns(), sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("role", sa.String(32), nullable=False), sa.CheckConstraint("role IN ('system_admin','recruiting_admin','recruiter','hiring_manager','interviewer')", name="ck_user_roles_role"), sa.UniqueConstraint("user_id", "role"))
    op.create_table("user_sessions", *common_columns(), sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False), sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("token_hash", sa.String(64), nullable=False, unique=True), sa.Column("csrf_token_hash", sa.String(64), nullable=False), sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("authorization_version", sa.Integer, nullable=False), sa.Column("revoked_at", sa.DateTime(timezone=True)), sa.Column("revocation_reason", sa.String(64)))
    op.create_index("ix_user_sessions_user_active", "user_sessions", ["user_id", "revoked_at"])
    op.create_table("jobs", *common_columns(), sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False), sa.Column("title", sa.String(200), nullable=False), sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False), sa.Column("status", sa.String(32), nullable=False))
    op.create_table("job_collaborators", *common_columns(), sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False), sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False), sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("access_role", sa.String(32), nullable=False), sa.CheckConstraint("access_role IN ('job_owner','job_recruiter','job_manager')", name="ck_job_collaborators_role"), sa.UniqueConstraint("job_id", "user_id", "access_role"))
    op.create_table("audit_logs", *common_columns(with_updated=False), sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="SET NULL")), sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")), sa.Column("event_type", sa.String(100), nullable=False), sa.Column("outcome", sa.String(32), nullable=False), sa.Column("trace_id", sa.String(64)), sa.Column("metadata_json", postgresql.JSONB, nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("job_collaborators")
    op.drop_table("jobs")
    op.drop_index("ix_user_sessions_user_active", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_table("user_roles")
    op.drop_table("users")
    op.drop_table("departments")
    op.drop_table("organizations")
