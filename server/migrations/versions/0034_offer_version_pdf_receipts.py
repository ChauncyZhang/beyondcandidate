"""Add immutable private PDF receipts to offer versions.

Revision ID: 0034_offer_version_pdf_receipts
Revises: 0033_offer_workflow
"""

from alembic import op
import sqlalchemy as sa


revision = "0034_offer_version_pdf_receipts"
down_revision = "0033_offer_workflow"
branch_labels = None
depends_on = None


_ORIGINAL_SNAPSHOT_UNCHANGED = """
    NEW.id IS NOT DISTINCT FROM OLD.id
    AND NEW.organization_id IS NOT DISTINCT FROM OLD.organization_id
    AND NEW.offer_id IS NOT DISTINCT FROM OLD.offer_id
    AND NEW.version_number IS NOT DISTINCT FROM OLD.version_number
    AND NEW.content IS NOT DISTINCT FROM OLD.content
    AND NEW.template_id IS NOT DISTINCT FROM OLD.template_id
    AND NEW.candidate_response_deadline IS NOT DISTINCT FROM OLD.candidate_response_deadline
    AND NEW.is_special IS NOT DISTINCT FROM OLD.is_special
    AND NEW.special_reason IS NOT DISTINCT FROM OLD.special_reason
    AND NEW.created_by IS NOT DISTINCT FROM OLD.created_by
    AND NEW.submitted_at IS NOT DISTINCT FROM OLD.submitted_at
    AND NEW.created_at IS NOT DISTINCT FROM OLD.created_at
"""


def upgrade() -> None:
    op.add_column("offer_versions", sa.Column("pdf_object_key", sa.String(512)))
    op.add_column("offer_versions", sa.Column("pdf_sha256", sa.String(64)))
    op.add_column("offer_versions", sa.Column("pdf_size_bytes", sa.Integer()))
    op.add_column("offer_versions", sa.Column("pdf_rendered_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_offer_versions_pdf_receipt",
        "offer_versions",
        "(pdf_object_key is null and pdf_sha256 is null and pdf_size_bytes is null and pdf_rendered_at is null) or "
        "(pdf_object_key is not null and pdf_sha256 is not null and length(pdf_sha256) = 64 and "
        "pdf_sha256 = lower(pdf_sha256) and pdf_sha256 !~ '[^0-9a-f]' and "
        "pdf_size_bytes > 0 and pdf_rendered_at is not null)",
    )
    op.execute("DROP TRIGGER offer_versions_immutable_after_submission ON offer_versions")
    op.execute("DROP FUNCTION prevent_submitted_offer_version_mutation()")
    op.execute(f"""
        CREATE FUNCTION prevent_submitted_offer_version_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            IF OLD.submitted_at IS NOT NULL THEN
              RAISE EXCEPTION 'submitted offer versions are immutable';
            END IF;
            RETURN OLD;
          END IF;
          IF OLD.submitted_at IS NOT NULL THEN
            IF OLD.pdf_object_key IS NULL
               AND NEW.pdf_object_key IS NOT NULL
               AND NEW.pdf_sha256 IS NOT NULL
               AND NEW.pdf_size_bytes IS NOT NULL
               AND NEW.pdf_rendered_at IS NOT NULL
               AND {_ORIGINAL_SNAPSHOT_UNCHANGED} THEN
              RETURN NEW;
            END IF;
            RAISE EXCEPTION 'submitted offer versions are immutable';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER offer_versions_immutable_after_submission BEFORE UPDATE OR DELETE ON offer_versions FOR EACH ROW EXECUTE FUNCTION prevent_submitted_offer_version_mutation()")


def downgrade() -> None:
    op.execute("DROP TRIGGER offer_versions_immutable_after_submission ON offer_versions")
    op.execute("DROP FUNCTION prevent_submitted_offer_version_mutation()")
    op.execute("""
        CREATE FUNCTION prevent_submitted_offer_version_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            IF OLD.submitted_at IS NOT NULL THEN
              RAISE EXCEPTION 'submitted offer versions are immutable';
            END IF;
            RETURN OLD;
          END IF;
          IF OLD.submitted_at IS NOT NULL THEN
            RAISE EXCEPTION 'submitted offer versions are immutable';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER offer_versions_immutable_after_submission BEFORE UPDATE OR DELETE ON offer_versions FOR EACH ROW EXECUTE FUNCTION prevent_submitted_offer_version_mutation()")
    op.drop_constraint("ck_offer_versions_pdf_receipt", "offer_versions", type_="check")
    op.drop_column("offer_versions", "pdf_rendered_at")
    op.drop_column("offer_versions", "pdf_size_bytes")
    op.drop_column("offer_versions", "pdf_sha256")
    op.drop_column("offer_versions", "pdf_object_key")
