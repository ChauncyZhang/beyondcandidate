"""Add reversible department lifecycle state."""

import sqlalchemy as sa
from alembic import op


revision = "0022_department_lifecycle"
down_revision = "0021_llm_only_auto_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "departments",
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
    )
    op.create_check_constraint(
        "ck_departments_status",
        "departments",
        "status in ('active','inactive')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_departments_status", "departments", type_="check")
    op.drop_column("departments", "status")
