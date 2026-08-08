"""Add immutable email delivery attachment snapshots.

Revision ID: 0032_interview_email_attachment
Revises: 0031_email_delivery
"""

from alembic import op
import sqlalchemy as sa


revision = "0032_interview_email_attachment"
down_revision = "0031_email_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("email_deliveries", sa.Column("attachment_filename", sa.String(255)))
    op.add_column("email_deliveries", sa.Column("attachment_content_type", sa.String(255)))
    op.add_column("email_deliveries", sa.Column("attachment_ciphertext", sa.LargeBinary()))
    op.create_check_constraint(
        "ck_email_deliveries_attachment_triplet",
        "email_deliveries",
        "(attachment_filename is null and attachment_content_type is null and attachment_ciphertext is null) or "
        "(attachment_filename is not null and attachment_content_type is not null and attachment_ciphertext is not null)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_email_deliveries_attachment_triplet", "email_deliveries", type_="check")
    op.drop_column("email_deliveries", "attachment_ciphertext")
    op.drop_column("email_deliveries", "attachment_content_type")
    op.drop_column("email_deliveries", "attachment_filename")
