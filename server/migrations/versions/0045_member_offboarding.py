"""Preserve removed member identity and durable unified handover intent."""

from alembic import op
import sqlalchemy as sa


revision = "0045_member_offboarding"
down_revision = "0044_password_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "member_offboardings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("successor_user_id", sa.Uuid(), nullable=True),
        sa.Column("removed_by", sa.Uuid(), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("unresolved", sa.JSON(), nullable=False, server_default="[]"),
        sa.UniqueConstraint("organization_id", "user_id"),
        sa.UniqueConstraint("organization_id", "id"),
        sa.ForeignKeyConstraint(["organization_id", "user_id"], ["users.organization_id", "users.id"]),
        sa.ForeignKeyConstraint(["organization_id", "successor_user_id"], ["users.organization_id", "users.id"]),
        sa.ForeignKeyConstraint(["organization_id", "removed_by"], ["users.organization_id", "users.id"]),
        sa.CheckConstraint("user_id != successor_user_id", name="ck_member_offboardings_distinct_successor"),
        sa.CheckConstraint("status in ('pending','completed')", name="ck_member_offboardings_status"),
        sa.CheckConstraint("version >= 1", name="ck_member_offboardings_version"),
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
            WHERE c.oid = 'public.users'::regclass
              AND a.privilege_type IN ('SELECT','INSERT','UPDATE','DELETE')
          LOOP
            EXECUTE format('GRANT %s ON TABLE public.member_offboardings TO %I', grant_row.privilege_type, grant_row.rolname);
          END LOOP;
        END $$
    """)


def downgrade() -> None:
    raise RuntimeError("Forward-only migration; retain removed member tombstones during application rollback.")
