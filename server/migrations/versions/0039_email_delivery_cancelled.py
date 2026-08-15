"""Allow superseded email deliveries to be cancelled.

Revision ID: 0039_email_delivery_cancelled
Revises: 0038_feishu_event_receipts
"""

from alembic import op


revision = "0039_email_delivery_cancelled"
down_revision = "0038_feishu_event_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("email_deliveries") as batch:
        batch.drop_constraint("ck_email_deliveries_status", type_="check")
        batch.create_check_constraint(
            "ck_email_deliveries_status",
            "status in ('queued','sent','failed','cancelled')",
        )


def downgrade() -> None:
    op.execute(
        "UPDATE email_deliveries SET status = 'failed', "
        "safe_error_code = COALESCE(safe_error_code, 'interview_message_superseded') "
        "WHERE status = 'cancelled'"
    )
    with op.batch_alter_table("email_deliveries") as batch:
        batch.drop_constraint("ck_email_deliveries_status", type_="check")
        batch.create_check_constraint(
            "ck_email_deliveries_status",
            "status in ('queued','sent','failed')",
        )
