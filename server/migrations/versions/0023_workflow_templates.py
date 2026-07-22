"""Add organization workflow templates and job bindings."""

import json
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0023_workflow_templates"
down_revision = "0022_department_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "workflow_templates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("rounds", json_document, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "name"),
        sa.CheckConstraint("status in ('active','inactive')", name="ck_workflow_templates_status"),
        sa.CheckConstraint("version >= 1", name="ck_workflow_templates_version"),
    )
    op.add_column("jobs", sa.Column("workflow_template_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_jobs_workflow_template",
        "jobs",
        "workflow_templates",
        ["organization_id", "workflow_template_id"],
        ["organization_id", "id"],
    )

    organizations = op.get_bind().execute(sa.text("select id from organizations")).scalars()
    template_table = sa.table(
        "workflow_templates",
        sa.column("id", sa.Uuid()),
        sa.column("organization_id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("rounds", json_document),
        sa.column("status", sa.String()),
        sa.column("version", sa.Integer()),
    )
    rows = []
    standard_template_ids = {}
    technical_template_ids = {}
    for organization_id in organizations:
        standard_id = uuid.uuid4()
        technical_id = uuid.uuid4()
        standard_template_ids[organization_id] = standard_id
        technical_template_ids[organization_id] = technical_id
        rows.extend(
            [
                {
                    "id": standard_id,
                    "organization_id": organization_id,
                    "name": "标准社招流程",
                    "rounds": ["一面"],
                    "status": "active",
                    "version": 1,
                },
                {
                    "id": technical_id,
                    "organization_id": organization_id,
                    "name": "技术岗位流程",
                    "rounds": ["一面", "二面"],
                    "status": "active",
                    "version": 1,
                },
            ]
        )
    if rows:
        op.bulk_insert(template_table, rows)

    job_table = sa.table(
        "jobs",
        sa.column("id", sa.Uuid()),
        sa.column("organization_id", sa.Uuid()),
        sa.column("workflow_template_id", sa.Uuid()),
    )
    jd_table = sa.table(
        "job_jd_versions",
        sa.column("job_id", sa.Uuid()),
        sa.column("content", json_document),
        sa.column("version_number", sa.Integer()),
    )
    bind = op.get_bind()
    latest_process_by_job = {}
    jd_rows = bind.execute(
        sa.select(jd_table.c.job_id, jd_table.c.content, jd_table.c.version_number)
        .order_by(jd_table.c.job_id, jd_table.c.version_number.desc())
    ).mappings()
    for jd_row in jd_rows:
        if jd_row["job_id"] in latest_process_by_job:
            continue
        content = jd_row["content"]
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                content = {}
        latest_process_by_job[jd_row["job_id"]] = (
            content.get("process_template", "") if isinstance(content, dict) else ""
        )
    for job_row in bind.execute(
        sa.select(job_table.c.id, job_table.c.organization_id)
    ).mappings():
        process_name = latest_process_by_job.get(job_row["id"], "")
        template_id = (
            technical_template_ids[job_row["organization_id"]]
            if "技术" in process_name
            else standard_template_ids[job_row["organization_id"]]
        )
        bind.execute(
            job_table.update()
            .where(
                job_table.c.id == job_row["id"],
                job_table.c.organization_id == job_row["organization_id"],
            )
            .values(workflow_template_id=template_id)
        )


def downgrade() -> None:
    op.drop_constraint("fk_jobs_workflow_template", "jobs", type_="foreignkey")
    op.drop_column("jobs", "workflow_template_id")
    op.drop_table("workflow_templates")
