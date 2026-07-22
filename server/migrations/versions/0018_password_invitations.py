"""Add invited users and one-time password invitations."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0018_password_invitations"
down_revision = "0017_governance_deletion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "password_invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_password_invitations_organization",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["users.organization_id", "users.id"],
            name="fk_password_invitations_user",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'",
            name="ck_password_invitations_token_hash",
        ),
    )
    op.create_index(
        "ix_password_invitations_user",
        "password_invitations",
        ["organization_id", "user_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("LOCK TABLE password_invitations, users IN ACCESS EXCLUSIVE MODE")
    )
    evidence_exists = connection.scalar(
        sa.text(
            """
            SELECT EXISTS (SELECT 1 FROM password_invitations)
                OR EXISTS (SELECT 1 FROM users WHERE status = 'invited')
            """
        )
    )
    if evidence_exists:
        raise RuntimeError(
            "refusing 0018 downgrade: invited users or password invitations exist"
        )
    op.drop_index(
        "ix_password_invitations_user", table_name="password_invitations"
    )
    op.drop_table("password_invitations")
