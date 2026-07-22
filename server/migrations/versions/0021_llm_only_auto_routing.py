"""Persist LLM-only screening routing state."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0021_llm_only_auto_routing"
down_revision = "0020_llm_provider_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("applications_stage_check", "applications", type_="check")
    op.create_check_constraint(
        "ck_applications_stage",
        "applications",
        "stage in ('new','review','deferred','contact','interview_pending','interviewing','decision','passed','hired','rejected','withdrawn')",
    )
    op.drop_constraint(
        "ck_llm_screening_evaluations_recommendation",
        "llm_screening_evaluations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_llm_screening_evaluations_recommendation",
        "llm_screening_evaluations",
        "recommendation in ('优先沟通','可沟通','暂缓','需人工复核','优先评审','建议评审')",
    )
    op.add_column(
        "llm_screening_evaluations",
        sa.Column(
            "dimensions",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_table(
        "application_review_tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("assignee_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("ai_status", sa.String(16), nullable=False),
        sa.Column("safe_error_code", sa.String(64), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", "id"),
        sa.ForeignKeyConstraint(["organization_id", "application_id"], ["applications.organization_id", "applications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "assignee_id"], ["users.organization_id", "users.id"]),
        sa.CheckConstraint("status in ('open','closed','cancelled')", name="ck_application_review_tasks_status"),
        sa.CheckConstraint("ai_status in ('succeeded','failed')", name="ck_application_review_tasks_ai_status"),
    )
    op.create_index(
        "uq_application_review_tasks_open_application",
        "application_review_tasks",
        ["organization_id", "application_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
        sqlite_where=sa.text("status = 'open'"),
    )
    op.add_column("talent_pools", sa.Column("system_key", sa.String(64), nullable=True))
    op.create_index(
        "uq_talent_pools_system_key",
        "talent_pools",
        ["organization_id", "system_key"],
        unique=True,
        postgresql_where=sa.text("system_key IS NOT NULL"),
        sqlite_where=sa.text("system_key IS NOT NULL"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    deferred_exists = connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM applications WHERE stage = 'deferred')"))
    if deferred_exists:
        raise RuntimeError("refusing 0021 downgrade: deferred applications exist")
    new_recommendation_exists = connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM llm_screening_evaluations WHERE recommendation IN ('优先评审','建议评审'))"
        )
    )
    if new_recommendation_exists:
        raise RuntimeError("refusing 0021 downgrade: new LLM recommendations exist")
    op.drop_index("uq_talent_pools_system_key", table_name="talent_pools")
    op.drop_column("talent_pools", "system_key")
    op.drop_index("uq_application_review_tasks_open_application", table_name="application_review_tasks")
    op.drop_table("application_review_tasks")
    op.drop_column("llm_screening_evaluations", "dimensions")
    op.drop_constraint(
        "ck_llm_screening_evaluations_recommendation",
        "llm_screening_evaluations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_llm_screening_evaluations_recommendation",
        "llm_screening_evaluations",
        "recommendation in ('优先沟通','可沟通','暂缓','需人工复核')",
    )
    op.drop_constraint("ck_applications_stage", "applications", type_="check")
    op.create_check_constraint(
        "applications_stage_check",
        "applications",
        "stage in ('new','review','contact','interview_pending','interviewing','decision','passed','hired','rejected','withdrawn')",
    )
