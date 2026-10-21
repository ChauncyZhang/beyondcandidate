"""Durable anonymous recovery intake, separate from account-dependent work.

No backfill is needed. Older applications ignore this additive table; pending
jobs require a recovery-capable worker or must be drained before rollback.
"""

from alembic import op
import sqlalchemy as sa


revision = "0044_password_recovery"
down_revision = "0043_password_reset"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "password_recovery_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("email_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("network_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "id"),
        sa.CheckConstraint(
            "(processed_at IS NULL AND email_ciphertext IS NOT NULL) OR "
            "(processed_at IS NOT NULL AND email_ciphertext IS NULL)",
            name="ck_password_recovery_requests_processed",
        ),
    )
    op.execute("""
        DO $$
        DECLARE grant_row record;
        BEGIN
          FOR grant_row IN
            SELECT DISTINCT r.rolname, a.privilege_type
            FROM pg_class c
            CROSS JOIN LATERAL aclexplode(c.relacl) a
            JOIN pg_roles r ON r.oid = a.grantee
            WHERE c.oid = 'public.password_invitations'::regclass
              AND a.privilege_type IN ('SELECT','INSERT','UPDATE','DELETE')
          LOOP
            EXECUTE format('GRANT %s ON TABLE public.password_recovery_requests TO %I', grant_row.privilege_type, grant_row.rolname);
          END LOOP;
        END $$
    """)


def downgrade() -> None:
    raise RuntimeError("Forward-only migration; drain recovery jobs before application rollback.")
