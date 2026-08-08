"""Add public offer capabilities and terminal response snapshots.

Revision ID: 0035_offer_access_tokens
Revises: 0034_offer_version_pdf_receipts
"""

from alembic import op
import sqlalchemy as sa


revision = "0035_offer_access_tokens"
down_revision = "0034_offer_version_pdf_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "offer_access_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("offer_id", sa.Uuid(), nullable=False),
        sa.Column("offer_version_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("organization_id", "id"), sa.UniqueConstraint("token_hash"),
        sa.ForeignKeyConstraint(["organization_id", "offer_id"], ["offers.organization_id", "offers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "offer_version_id", "offer_id"], ["offer_versions.organization_id", "offer_versions.id", "offer_versions.offer_id"]),
        sa.CheckConstraint("length(token_hash) = 64 and token_hash = lower(token_hash) and token_hash !~ '[^0-9a-f]'", name="ck_offer_access_tokens_hash"),
    )
    op.create_index("ix_offer_access_tokens_lookup", "offer_access_tokens", ["token_hash"])
    op.create_index("ix_offer_access_tokens_offer_current", "offer_access_tokens", ["organization_id", "offer_id", "revoked_at"])
    op.create_index(
        "uq_offer_access_tokens_active_offer",
        "offer_access_tokens",
        ["organization_id", "offer_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at is null"),
    )
    op.add_column("offer_responses", sa.Column("offer_version_id", sa.Uuid()))
    op.add_column("offer_responses", sa.Column("version_number", sa.Integer()))
    op.add_column("offer_responses", sa.Column("source", sa.String(16)))
    op.add_column("offer_responses", sa.Column("actor_user_id", sa.Uuid()))
    op.add_column("offer_responses", sa.Column("expected_start_date", sa.Date()))
    op.add_column("offer_responses", sa.Column("reason_text", sa.Text()))
    op.add_column("offer_responses", sa.Column("communication_channel", sa.String(16)))
    op.add_column("offer_responses", sa.Column("communicated_at", sa.DateTime(timezone=True)))
    op.add_column("offer_responses", sa.Column("note", sa.Text()))
    op.add_column("offer_responses", sa.Column("request_hash", sa.String(64)))
    op.create_foreign_key(
        "fk_offer_responses_version",
        "offer_responses", "offer_versions",
        ["organization_id", "offer_id", "offer_version_id", "version_number"],
        ["organization_id", "offer_id", "id", "version_number"],
    )
    op.create_foreign_key("fk_offer_responses_actor", "offer_responses", "users", ["organization_id", "actor_user_id"], ["organization_id", "id"])
    op.execute("ALTER TABLE offers DROP CONSTRAINT ck_offers_status")
    op.create_check_constraint("ck_offers_status", "offers", "status in ('draft','pending_approval','changes_requested','ready_to_send','sent','accepted','declined','withdrawn','expired')")
    op.create_check_constraint("ck_offer_responses_source", "offer_responses", "(source is null and offer_version_id is null and version_number is null and actor_user_id is null and expected_start_date is null and reason_text is null and communication_channel is null and communicated_at is null and note is null and request_hash is null) or (source = 'candidate' and offer_version_id is not null and version_number >= 1 and actor_user_id is null and communication_channel is null and communicated_at is null and note is null and request_hash is not null) or (source = 'hr_proxy' and offer_version_id is not null and version_number >= 1 and actor_user_id is not null and communication_channel in ('phone','wechat','email','other') and communicated_at is not null and request_hash is not null)")
    op.create_check_constraint("ck_offer_responses_payload", "offer_responses", "source is null or (status = 'accepted' and expected_start_date is not null and reason_text is null) or (status = 'declined' and expected_start_date is null)")
    # Existing rows predate public response capabilities and remain readable; new rows are complete.


def downgrade() -> None:
    op.drop_constraint("ck_offer_responses_payload", "offer_responses", type_="check")
    op.drop_constraint("ck_offer_responses_source", "offer_responses", type_="check")
    op.drop_constraint("fk_offer_responses_actor", "offer_responses", type_="foreignkey")
    op.drop_constraint("fk_offer_responses_version", "offer_responses", type_="foreignkey")
    op.execute("ALTER TABLE offers DROP CONSTRAINT ck_offers_status")
    op.create_check_constraint("ck_offers_status", "offers", "status in ('draft','pending_approval','changes_requested','ready_to_send','sent','withdrawn','expired')")
    op.drop_column("offer_responses", "request_hash")
    op.drop_column("offer_responses", "note")
    op.drop_column("offer_responses", "communicated_at")
    op.drop_column("offer_responses", "communication_channel")
    op.drop_column("offer_responses", "reason_text")
    op.drop_column("offer_responses", "expected_start_date")
    op.drop_column("offer_responses", "actor_user_id")
    op.drop_column("offer_responses", "source")
    op.drop_column("offer_responses", "version_number")
    op.drop_column("offer_responses", "offer_version_id")
    op.drop_index("ix_offer_access_tokens_offer_current", table_name="offer_access_tokens")
    op.drop_index("uq_offer_access_tokens_active_offer", table_name="offer_access_tokens")
    op.drop_index("ix_offer_access_tokens_lookup", table_name="offer_access_tokens")
    op.drop_table("offer_access_tokens")
