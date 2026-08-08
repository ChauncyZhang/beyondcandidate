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
    op.add_column("offer_responses", sa.Column("offer_version_id", sa.Uuid()))
    op.add_column("offer_responses", sa.Column("expected_start_date", sa.DateTime(timezone=True)))
    op.add_column("offer_responses", sa.Column("reason_text", sa.Text()))
    op.add_column("offer_responses", sa.Column("request_hash", sa.String(64)))
    op.create_foreign_key(
        "fk_offer_responses_version",
        "offer_responses", "offer_versions",
        ["organization_id", "offer_version_id", "offer_id"],
        ["organization_id", "id", "offer_id"],
    )
    op.execute("ALTER TABLE offers DROP CONSTRAINT ck_offers_status")
    op.create_check_constraint("ck_offers_status", "offers", "status in ('draft','pending_approval','changes_requested','ready_to_send','sent','accepted','declined','withdrawn','expired')")
    op.create_check_constraint("ck_offer_responses_payload", "offer_responses", "(status = 'accepted' and expected_start_date is not null and reason_text is null) or (status = 'declined' and expected_start_date is null)")
    # Existing rows predate public response capabilities and remain readable; new rows are complete.


def downgrade() -> None:
    op.drop_constraint("ck_offer_responses_payload", "offer_responses", type_="check")
    op.drop_constraint("fk_offer_responses_version", "offer_responses", type_="foreignkey")
    op.execute("ALTER TABLE offers DROP CONSTRAINT ck_offers_status")
    op.create_check_constraint("ck_offers_status", "offers", "status in ('draft','pending_approval','changes_requested','ready_to_send','sent','withdrawn','expired')")
    op.drop_column("offer_responses", "request_hash")
    op.drop_column("offer_responses", "reason_text")
    op.drop_column("offer_responses", "expected_start_date")
    op.drop_column("offer_responses", "offer_version_id")
    op.drop_index("ix_offer_access_tokens_offer_current", table_name="offer_access_tokens")
    op.drop_index("ix_offer_access_tokens_lookup", table_name="offer_access_tokens")
    op.drop_table("offer_access_tokens")
