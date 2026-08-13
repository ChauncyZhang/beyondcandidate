"""Add durable Feishu webhook event receipts.

Revision ID: 0038_feishu_event_receipts
Revises: 0037_fix_offer_approver_status
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0038_feishu_event_receipts"
down_revision = "0037_fix_offer_approver_status"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "feishu_event_receipts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("event_id", sa.String(512), nullable=False),
        sa.Column("event_type", sa.String(255), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "event_id", name="uq_feishu_event_receipts_org_event"),
    )
    op.create_index(
        "ix_feishu_event_receipts_received",
        "feishu_event_receipts",
        ["received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_feishu_event_receipts_received", table_name="feishu_event_receipts")
    op.drop_table("feishu_event_receipts")
