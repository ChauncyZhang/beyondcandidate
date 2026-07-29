"""Persist per-user recruiting job visibility scopes."""

import sqlalchemy as sa
from alembic import op


revision = "0027_recruiting_user_scopes"
down_revision = "0026_notification_reads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "recruiting_scope_type",
            sa.String(16),
            nullable=False,
            server_default="jobs",
        ),
    )
    op.create_check_constraint(
        "ck_users_recruiting_scope_type",
        "users",
        "recruiting_scope_type IN ('jobs','departments','organization')",
    )
    op.create_table(
        "user_recruiting_department_scopes",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["users.organization_id", "users.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "department_id"],
            ["departments.organization_id", "departments.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "department_id"),
    )
    op.create_index(
        "ix_user_recruiting_department_scopes_department",
        "user_recruiting_department_scopes",
        ["organization_id", "department_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_user_recruiting_department_scopes_department",
        table_name="user_recruiting_department_scopes",
    )
    op.drop_table("user_recruiting_department_scopes")
    op.drop_constraint("ck_users_recruiting_scope_type", "users", type_="check")
    op.drop_column("users", "recruiting_scope_type")
