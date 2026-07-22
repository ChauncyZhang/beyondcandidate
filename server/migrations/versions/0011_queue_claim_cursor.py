"""Persist fair queue claim cursors."""

from alembic import op
import sqlalchemy as sa


revision = "0011_queue_claim_cursor"
down_revision = "0010_llm_screening_evaluations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "queue_claim_cursors",
        sa.Column("kind", sa.String(20), primary_key=True),
        sa.Column("last_organization_id", sa.Uuid()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind in ('job','outbox')", name="ck_queue_claim_cursors_kind"),
    )
    cursor_table = sa.table("queue_claim_cursors", sa.column("kind", sa.String), sa.column("last_organization_id", sa.Uuid))
    op.bulk_insert(cursor_table, [{"kind": "job", "last_organization_id": None}, {"kind": "outbox", "last_organization_id": None}])


def downgrade():
    op.drop_table("queue_claim_cursors")
