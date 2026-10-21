"""Separate activation invitations from administrator-issued password resets.

Existing rows retain invitation purpose; existing table grants remain unchanged.
Do not roll back to old application code with outstanding reset links: older
activation code does not enforce token purpose. Expire reset links first.
"""

from alembic import op
import sqlalchemy as sa


revision = "0043_password_reset"
down_revision = "0042_offer_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "password_invitations",
        sa.Column("purpose", sa.String(32), nullable=False, server_default="invitation"),
    )
    op.add_column(
        "password_invitations",
        sa.Column("reset_authorization_version", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_password_invitations_purpose", "password_invitations",
        "purpose IN ('invitation','password_reset')",
    )


def downgrade() -> None:
    raise RuntimeError("Forward-only migration; retain the purpose discriminator.")
