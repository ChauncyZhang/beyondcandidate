"""Add candidate contact provenance and confirmation state."""

from alembic import op
import sqlalchemy as sa


revision = "0030_candidate_contact_confirmation"
down_revision = "0029_separate_job_owners"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(length=32),
        type_=sa.String(length=128),
        existing_nullable=False,
    )
    op.add_column("candidate_contacts", sa.Column("source", sa.String(length=32), nullable=True))
    op.add_column("candidate_contacts", sa.Column("confirmation_status", sa.String(length=16), nullable=True))
    op.add_column("candidate_contacts", sa.Column("confirmed_by", sa.Uuid(), nullable=True))
    op.add_column("candidate_contacts", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("candidate_contacts", sa.Column("version", sa.Integer(), nullable=True))
    op.execute("UPDATE candidate_contacts SET source = 'legacy', confirmation_status = 'unconfirmed', version = 1")
    op.alter_column("candidate_contacts", "source", nullable=False, server_default="manual")
    op.alter_column("candidate_contacts", "confirmation_status", nullable=False, server_default="unconfirmed")
    op.alter_column("candidate_contacts", "version", nullable=False, server_default="1")
    op.create_foreign_key("fk_candidate_contacts_tenant_confirmer", "candidate_contacts", "users", ["organization_id", "confirmed_by"], ["organization_id", "id"])
    op.create_check_constraint("ck_candidate_contacts_source", "candidate_contacts", "source in ('legacy','manual','native','ocr')")
    op.create_check_constraint("ck_candidate_contacts_confirmation_status", "candidate_contacts", "confirmation_status in ('unconfirmed','confirmed')")


def downgrade() -> None:
    op.drop_constraint("ck_candidate_contacts_confirmation_status", "candidate_contacts", type_="check")
    op.drop_constraint("ck_candidate_contacts_source", "candidate_contacts", type_="check")
    op.drop_constraint("fk_candidate_contacts_tenant_confirmer", "candidate_contacts", type_="foreignkey")
    op.drop_column("candidate_contacts", "version")
    op.drop_column("candidate_contacts", "confirmed_at")
    op.drop_column("candidate_contacts", "confirmed_by")
    op.drop_column("candidate_contacts", "confirmation_status")
    op.drop_column("candidate_contacts", "source")
