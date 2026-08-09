"""Add organization-scoped sender identity to email provider versions.

Revision ID: 0036_email_sender_identity
Revises: 0035_offer_access_tokens
"""

from alembic import op
import sqlalchemy as sa


revision = "0036_email_sender_identity"
down_revision = "0035_offer_access_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("email_provider_configs", sa.Column("sender_address", sa.String(320)))
    op.add_column("email_provider_configs", sa.Column("sender_name", sa.String(200)))
    op.create_check_constraint(
        "ck_email_provider_configs_sender_pair",
        "email_provider_configs",
        "(sender_address is null and sender_name is null) or "
        "(sender_address is not null and sender_name is not null)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_email_provider_configs_sender_pair",
        "email_provider_configs",
        type_="check",
    )
    op.drop_column("email_provider_configs", "sender_name")
    op.drop_column("email_provider_configs", "sender_address")
