"""Add fixed onboarding approvers and validated node metadata.

Legacy metadata remains NULL and requires revalidation before submission.
"""

import sqlalchemy as sa
from alembic import op


revision = "0041_onboarding_approvers"
down_revision = "0040_feishu_onboarding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("feishu_onboarding_configs", sa.Column("node_approvers", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("feishu_onboarding_configs", sa.Column("approval_nodes", sa.JSON(none_as_null=True), nullable=True))


def downgrade() -> None:
    op.drop_column("feishu_onboarding_configs", "approval_nodes")
    op.drop_column("feishu_onboarding_configs", "node_approvers")
