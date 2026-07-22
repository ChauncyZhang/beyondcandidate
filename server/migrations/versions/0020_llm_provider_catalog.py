"""Add organization-managed LLM providers."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision="0020_llm_provider_catalog"
down_revision="0019_feishu_integration"
branch_labels=None
depends_on=None

def upgrade()->None:
    op.create_table(
        "llm_providers",
        sa.Column("id",postgresql.UUID(as_uuid=True),primary_key=True),
        sa.Column("organization_id",postgresql.UUID(as_uuid=True),nullable=False),
        sa.Column("provider_id",sa.String(64),nullable=False),
        sa.Column("display_name",sa.String(100),nullable=False),
        sa.Column("base_url",sa.String(2048),nullable=False),
        sa.Column("models",postgresql.JSONB(),nullable=False),
        sa.Column("created_by",postgresql.UUID(as_uuid=True),nullable=False),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id","provider_id"),
        sa.UniqueConstraint("organization_id","id"),
        sa.ForeignKeyConstraint(["organization_id","created_by"],["users.organization_id","users.id"]),
        sa.CheckConstraint("jsonb_array_length(models)>0",name="ck_llm_providers_models"),
    )

def downgrade()->None:
    op.drop_table("llm_providers")
