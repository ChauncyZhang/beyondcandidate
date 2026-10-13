"""Store encrypted formal Offer attachments separately from approved content.

Existing Offers need no backfill. Application rollback ignores this additive table.
"""

from alembic import op
import sqlalchemy as sa


revision = "0042_offer_attachments"
down_revision = "0041_onboarding_approvers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "offer_attachments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("offer_id", sa.Uuid(), nullable=False),
        sa.Column("offer_version_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", "offer_version_id"),
        sa.ForeignKeyConstraint(["organization_id", "offer_version_id", "offer_id"], ["offer_versions.organization_id", "offer_versions.id", "offer_versions.offer_id"], ondelete="CASCADE"),
        sa.CheckConstraint("size_bytes > 0 and size_bytes <= 10485760", name="ck_offer_attachments_size"),
        sa.CheckConstraint("content_type = 'application/pdf' and length(filename) > 0", name="ck_offer_attachments_type"),
        sa.CheckConstraint("length(sha256) = 64 and sha256 = lower(sha256) and sha256 !~ '[^0-9a-f]'", name="ck_offer_attachments_sha256"),
        sa.CheckConstraint("length(ciphertext) > 0", name="ck_offer_attachments_ciphertext"),
    )
    # Existing app roles receive the same DML privileges as the parent Offers.
    # New installations also obtain these through normal role provisioning.
    op.execute("""
        DO $$
        DECLARE grant_row record;
        BEGIN
          FOR grant_row IN
            SELECT DISTINCT r.rolname, a.privilege_type
            FROM pg_class c
            CROSS JOIN LATERAL aclexplode(c.relacl) a
            JOIN pg_roles r ON r.oid = a.grantee
            WHERE c.oid = 'public.offers'::regclass
              AND a.privilege_type IN ('SELECT','INSERT','UPDATE','DELETE')
          LOOP
            EXECUTE format('GRANT %s ON TABLE public.offer_attachments TO %I', grant_row.privilege_type, grant_row.rolname);
          END LOOP;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION delete_candidate_offer_attachments() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL THEN
            DELETE FROM public.offer_attachments attachment
            USING public.offers offer, public.applications application
            WHERE attachment.organization_id = NEW.organization_id
              AND offer.organization_id = attachment.organization_id
              AND offer.id = attachment.offer_id
              AND application.organization_id = offer.organization_id
              AND application.id = offer.application_id
              AND application.candidate_id = NEW.id;
            UPDATE public.email_deliveries delivery
            SET attachment_ciphertext = NULL, attachment_filename = NULL,
                attachment_content_type = NULL
            FROM public.offer_access_tokens token, public.offers offer, public.applications application
            WHERE delivery.organization_id = NEW.organization_id
              AND delivery.resource_type = 'offer_access_token'
              AND delivery.resource_id = token.id
              AND token.organization_id = delivery.organization_id
              AND offer.organization_id = token.organization_id AND offer.id = token.offer_id
              AND application.organization_id = offer.organization_id
              AND application.id = offer.application_id AND application.candidate_id = NEW.id;
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER trg_delete_candidate_offer_attachments
        AFTER UPDATE OF deleted_at ON candidates FOR EACH ROW
        EXECUTE FUNCTION delete_candidate_offer_attachments()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_delete_candidate_offer_attachments ON candidates")
    op.execute("DROP FUNCTION delete_candidate_offer_attachments()")
    op.drop_table("offer_attachments")
