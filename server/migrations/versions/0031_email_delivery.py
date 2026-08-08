"""Add tenant-scoped transactional email configuration and delivery."""

from alembic import op
import sqlalchemy as sa


revision = "0031_email_delivery"
down_revision = "0030_candidate_contact_confirmation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_provider_configs",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("host", sa.String(253), nullable=False), sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("tls_mode", sa.String(16), nullable=False), sa.Column("username", sa.String(320), nullable=False),
        sa.Column("encrypted_password", sa.LargeBinary(), nullable=False),
        sa.Column("default_reply_to_email", sa.String(320), nullable=False), sa.Column("default_reply_to_name", sa.String(200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"), sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("organization_id", "version"),
        sa.UniqueConstraint("organization_id", "id", "version"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "created_by"], ["users.organization_id", "users.id"]),
        sa.ForeignKeyConstraint(["organization_id", "updated_by"], ["users.organization_id", "users.id"]),
        sa.CheckConstraint("port between 1 and 65535", name="ck_email_provider_configs_port"),
        sa.CheckConstraint("tls_mode in ('starttls','tls')", name="ck_email_provider_configs_tls_mode"),
        sa.CheckConstraint("version >= 1", name="ck_email_provider_configs_version"),
    )
    op.create_table(
        "email_templates",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(100), nullable=False), sa.Column("subject_template", sa.String(998), nullable=False),
        sa.Column("body_template", sa.Text(), nullable=False), sa.Column("variable_allowlist", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()), sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.Uuid(), nullable=False), sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("organization_id", "key"), sa.UniqueConstraint("organization_id", "id"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "created_by"], ["users.organization_id", "users.id"]),
        sa.ForeignKeyConstraint(["organization_id", "updated_by"], ["users.organization_id", "users.id"]),
        sa.CheckConstraint("version >= 1", name="ck_email_templates_version"),
    )
    op.create_table(
        "email_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("provider_config_id", sa.Uuid(), nullable=False), sa.Column("provider_config_version", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.Uuid()), sa.Column("template_version", sa.Integer()),
        sa.Column("recipient_ciphertext", sa.LargeBinary(), nullable=False), sa.Column("recipient_masked", sa.String(320), nullable=False),
        sa.Column("sender_email", sa.String(320), nullable=False), sa.Column("sender_name", sa.String(200), nullable=False),
        sa.Column("reply_to_email", sa.String(320), nullable=False), sa.Column("reply_to_name", sa.String(200), nullable=False),
        sa.Column("rendered_subject", sa.String(998), nullable=False), sa.Column("rendered_body", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False), sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("business_dedupe_key", sa.String(255), nullable=False), sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("parent_delivery_id", sa.Uuid()),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"), sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("safe_error_code", sa.String(64)), sa.Column("provider_receipt_id", sa.String(255)),
        sa.Column("sent_at", sa.DateTime(timezone=True)), sa.Column("failed_at", sa.DateTime(timezone=True)), sa.Column("created_by", sa.Uuid()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("organization_id", "id"), sa.UniqueConstraint("organization_id", "business_dedupe_key"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["organization_id", "provider_config_id", "provider_config_version"],
            ["email_provider_configs.organization_id", "email_provider_configs.id", "email_provider_configs.version"],
        ),
        sa.ForeignKeyConstraint(["organization_id", "template_id"], ["email_templates.organization_id", "email_templates.id"]),
        sa.ForeignKeyConstraint(["organization_id", "parent_delivery_id"], ["email_deliveries.organization_id", "email_deliveries.id"]),
        sa.ForeignKeyConstraint(["organization_id", "created_by"], ["users.organization_id", "users.id"]),
        sa.CheckConstraint("status in ('queued','sent','failed')", name="ck_email_deliveries_status"),
        sa.CheckConstraint("attempts between 0 and 3", name="ck_email_deliveries_attempts"),
        sa.CheckConstraint("provider_config_version >= 1", name="ck_email_deliveries_provider_version"),
        sa.CheckConstraint("version >= 1", name="ck_email_deliveries_version"),
        sa.CheckConstraint(
            "(template_id is null and template_version is null) or (template_id is not null and template_version is not null)",
            name="ck_email_deliveries_template_version_pair",
        ),
    )
    op.create_index("ix_email_deliveries_history", "email_deliveries", ["organization_id", "created_at"])
    op.create_table(
        "user_notifications",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False), sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False), sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_masked", sa.String(320), nullable=False), sa.Column("safe_error_code", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("read_at", sa.DateTime(timezone=True)), sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "user_id", "event_type", "resource_type", "resource_id", name="uq_user_notifications_event_resource"),
        sa.ForeignKeyConstraint(["organization_id", "user_id"], ["users.organization_id", "users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_user_notifications_inbox", "user_notifications", ["organization_id", "user_id", "read_at", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_user_notifications_inbox", table_name="user_notifications")
    op.drop_table("user_notifications")
    op.drop_index("ix_email_deliveries_history", table_name="email_deliveries")
    op.drop_table("email_deliveries")
    op.drop_table("email_templates")
    op.drop_table("email_provider_configs")
