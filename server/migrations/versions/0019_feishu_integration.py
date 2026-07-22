"""Add disabled-by-default Feishu integration state."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0019_feishu_integration"
down_revision = "0018_password_invitations"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "feishu_organization_configs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("organization_id", UUID, nullable=False, unique=True),
        sa.Column("app_id", sa.String(128), nullable=False),
        sa.Column("encrypted_app_secret", sa.LargeBinary(), nullable=True),
        sa.Column("redirect_uri", sa.String(2048), nullable=False),
        sa.Column("calendar_id", sa.String(512), nullable=False, server_default="primary"),
        sa.Column("encrypted_verification_token", sa.LargeBinary(), nullable=True),
        sa.Column("encrypted_encrypt_key", sa.LargeBinary(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_test_status", sa.String(32), nullable=True),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_error_code", sa.String(100), nullable=True),
        sa.Column("created_by", UUID, nullable=False),
        sa.Column("updated_by", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "created_by"], ["users.organization_id", "users.id"]),
        sa.ForeignKeyConstraint(["organization_id", "updated_by"], ["users.organization_id", "users.id"]),
        sa.CheckConstraint("version >= 1", name="ck_feishu_configs_version"),
        sa.CheckConstraint("not enabled or encrypted_app_secret is not null", name="ck_feishu_configs_enabled_secret"),
        sa.CheckConstraint("last_test_status is null or last_test_status in ('succeeded','failed')", name="ck_feishu_configs_test_status"),
    )
    op.create_table(
        "feishu_oauth_states",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("initiating_user_id", UUID, nullable=True),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "initiating_user_id"], ["users.organization_id", "users.id"], ondelete="CASCADE"),
        sa.CheckConstraint("purpose in ('login','bind')", name="ck_feishu_oauth_states_purpose"),
    )
    op.create_index("ix_feishu_oauth_states_expiry", "feishu_oauth_states", ["expires_at"])
    op.create_table(
        "feishu_identity_bindings",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("union_id", sa.String(128), nullable=True),
        sa.Column("open_id", sa.String(128), nullable=False),
        sa.Column("tenant_key", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id", "user_id"], ["users.organization_id", "users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "user_id"),
        sa.UniqueConstraint("organization_id", "union_id"),
        sa.UniqueConstraint("organization_id", "open_id"),
    )
    op.create_table(
        "feishu_interview_syncs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("interview_id", UUID, nullable=False),
        sa.Column("external_calendar_id", sa.String(512), nullable=True),
        sa.Column("external_event_id", sa.String(512), nullable=True),
        sa.Column("desired_action", sa.String(16), nullable=False, server_default="create"),
        sa.Column("sync_status", sa.String(32), nullable=False, server_default="disabled"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", UUID, nullable=False),
        sa.Column("last_error_code", sa.String(100), nullable=True),
        sa.Column("provider_revision", sa.String(255), nullable=True),
        sa.Column("provider_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id", "interview_id"], ["interviews.organization_id", "interviews.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "interview_id"),
        sa.CheckConstraint("desired_action in ('create','update','cancel')", name="ck_feishu_sync_action"),
        sa.CheckConstraint("sync_status in ('disabled','pending','syncing','synced','failed','pending_confirmation','cancelled')", name="ck_feishu_sync_status"),
        sa.CheckConstraint("attempts >= 0", name="ck_feishu_sync_attempts"),
    )
    op.create_index("ix_feishu_sync_external_event", "feishu_interview_syncs", ["organization_id", "external_event_id"])


def downgrade() -> None:
    op.drop_index("ix_feishu_sync_external_event", table_name="feishu_interview_syncs")
    op.drop_table("feishu_interview_syncs")
    op.drop_table("feishu_identity_bindings")
    op.drop_index("ix_feishu_oauth_states_expiry", table_name="feishu_oauth_states")
    op.drop_table("feishu_oauth_states")
    op.drop_table("feishu_organization_configs")
