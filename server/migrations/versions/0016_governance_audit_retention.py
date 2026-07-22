"""Add governance retention policy and normalized partitioned audit storage."""

from __future__ import annotations

from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0016_governance_audit_retention"
down_revision = "0015_reports_exports"
branch_labels = None
depends_on = None


LEGACY_KEY_BY_RESOURCE_TYPE = {
    "candidate": "candidate_id",
    "application": "application_id",
    "job": "job_id",
    "resume": "resume_id",
    "screening_run": "run_id",
    "screening_item": "item_id",
    "interview": "interview_id",
    "talent_pool": "pool_id",
    "talent_pool_membership": "membership_id",
    "report_export": "export_id",
    "llm_config": "config_id",
}
RESOURCE_KEYS = tuple(LEGACY_KEY_BY_RESOURCE_TYPE.values())


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _next_month(value: date) -> date:
    return date(value.year + (value.month == 12), 1 if value.month == 12 else value.month + 1, 1)


def _create_audit_partition(start: date) -> None:
    end = _next_month(start)
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS audit_logs_{start:%Y_%m}
        PARTITION OF audit_logs
        FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')
        """
    )


def _create_append_only_trigger() -> None:
    op.execute(
        """
        CREATE TRIGGER audit_logs_append_only
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION reject_audit_log_mutation()
        """
    )


def _resource_type_sql() -> str:
    return """
        CASE
          WHEN event_type LIKE 'candidate.%' AND metadata_json ? 'candidate_id' THEN 'candidate'
          WHEN event_type LIKE 'application.%' AND metadata_json ? 'application_id' THEN 'application'
          WHEN event_type LIKE 'job.%' AND metadata_json ? 'job_id' THEN 'job'
          WHEN event_type LIKE 'resume.%' AND metadata_json ? 'resume_id' THEN 'resume'
          WHEN event_type LIKE 'screening.item_%' AND metadata_json ? 'item_id' THEN 'screening_item'
          WHEN event_type LIKE 'screening.%' AND metadata_json ? 'run_id' THEN 'screening_run'
          WHEN event_type LIKE 'interview.%' AND metadata_json ? 'interview_id' THEN 'interview'
          WHEN event_type LIKE 'talent_pool.member_%' AND metadata_json ? 'membership_id' THEN 'talent_pool_membership'
          WHEN event_type LIKE 'talent_pool.%' AND metadata_json ? 'pool_id' THEN 'talent_pool'
          WHEN event_type LIKE 'report_export.%' AND metadata_json ? 'export_id' THEN 'report_export'
          WHEN event_type LIKE 'llm.%' AND metadata_json ? 'config_id' THEN 'llm_config'
          ELSE NULL
        END
    """


def _resource_id_sql() -> str:
    return """
        CASE
          WHEN event_type LIKE 'candidate.%' AND metadata_json ? 'candidate_id' THEN metadata_json ->> 'candidate_id'
          WHEN event_type LIKE 'application.%' AND metadata_json ? 'application_id' THEN metadata_json ->> 'application_id'
          WHEN event_type LIKE 'job.%' AND metadata_json ? 'job_id' THEN metadata_json ->> 'job_id'
          WHEN event_type LIKE 'resume.%' AND metadata_json ? 'resume_id' THEN metadata_json ->> 'resume_id'
          WHEN event_type LIKE 'screening.item_%' AND metadata_json ? 'item_id' THEN metadata_json ->> 'item_id'
          WHEN event_type LIKE 'screening.%' AND metadata_json ? 'run_id' THEN metadata_json ->> 'run_id'
          WHEN event_type LIKE 'interview.%' AND metadata_json ? 'interview_id' THEN metadata_json ->> 'interview_id'
          WHEN event_type LIKE 'talent_pool.member_%' AND metadata_json ? 'membership_id' THEN metadata_json ->> 'membership_id'
          WHEN event_type LIKE 'talent_pool.%' AND metadata_json ? 'pool_id' THEN metadata_json ->> 'pool_id'
          WHEN event_type LIKE 'report_export.%' AND metadata_json ? 'export_id' THEN metadata_json ->> 'export_id'
          WHEN event_type LIKE 'llm.%' AND metadata_json ? 'config_id' THEN metadata_json ->> 'config_id'
          ELSE NULL
        END
    """


def _upgrade_retention() -> None:
    op.create_table(
        "retention_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "organizations.id",
                ondelete="CASCADE",
                deferrable=True,
                initially="DEFERRED",
            ),
            nullable=False,
        ),
        sa.Column("terminal_days", sa.Integer(), nullable=False, server_default="365"),
        sa.Column("talent_pool_days", sa.Integer(), nullable=False, server_default="730"),
        sa.Column("backup_window_days", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", name="uq_retention_policies_organization_id"),
        sa.UniqueConstraint("organization_id", "id", name="uq_retention_policies_tenant_id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "updated_by"],
            ["users.organization_id", "users.id"],
            name="fk_retention_policies_updated_by",
        ),
        sa.CheckConstraint(
            "terminal_days BETWEEN 30 AND 3650", name="ck_retention_policies_terminal_days"
        ),
        sa.CheckConstraint(
            "talent_pool_days BETWEEN 30 AND 3650", name="ck_retention_policies_talent_pool_days"
        ),
        sa.CheckConstraint(
            "backup_window_days BETWEEN 30 AND 3650", name="ck_retention_policies_backup_window_days"
        ),
        sa.CheckConstraint("version >= 1", name="ck_retention_policies_version"),
        sa.CheckConstraint(
            "version = 1 OR updated_by IS NOT NULL",
            name="ck_retention_policies_updated_by_version",
        ),
    )
    op.add_column(
        "organizations",
        sa.Column("retention_policy_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        """
        INSERT INTO retention_policies(id, organization_id, updated_by)
        SELECT md5(o.id::text || '-retention-policy')::uuid, o.id, NULL
        FROM organizations o
        """
    )
    op.execute(
        """
        UPDATE organizations o
        SET retention_policy_id = p.id
        FROM retention_policies p
        WHERE p.organization_id = o.id
        """
    )
    op.alter_column("organizations", "retention_policy_id", nullable=False)
    op.create_foreign_key(
        "fk_organizations_retention_policy",
        "organizations",
        "retention_policies",
        ["id", "retention_policy_id"],
        ["organization_id", "id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.execute(
        """
        CREATE FUNCTION seed_organization_retention_policy() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.retention_policy_id IS NULL THEN
            NEW.retention_policy_id := md5(NEW.id::text || '-retention-policy')::uuid;
          END IF;
          INSERT INTO retention_policies(id, organization_id, updated_by)
          VALUES (NEW.retention_policy_id, NEW.id, NULL);
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER organizations_seed_retention_policy
        BEFORE INSERT ON organizations
        FOR EACH ROW EXECUTE FUNCTION seed_organization_retention_policy()
        """
    )


def _upgrade_audit() -> None:
    bind = op.get_bind()
    oldest = bind.scalar(sa.text("SELECT min(created_at)::date FROM audit_logs"))
    today = bind.scalar(sa.text("SELECT current_date"))
    start = _month_start(oldest or today)
    through = _next_month(_month_start(today))

    op.execute("ALTER TABLE audit_logs RENAME TO audit_logs_0015")
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=True),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ip_hash", sa.String(64), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("created_at", "id", name="pk_audit_logs_created_id"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="SET NULL", name="fk_audit_logs_org"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="SET NULL", name="fk_audit_logs_actor"
        ),
        postgresql_partition_by="RANGE (created_at)",
    )
    cursor = start
    while cursor <= through:
        _create_audit_partition(cursor)
        cursor = _next_month(cursor)
    op.execute("CREATE TABLE audit_logs_default PARTITION OF audit_logs DEFAULT")

    resource_id = _resource_id_sql()
    resource_type = _resource_type_sql()
    removed_keys = ", ".join(f"'{key}'" for key in RESOURCE_KEYS)
    op.execute(
        f"""
        INSERT INTO audit_logs(
          id, organization_id, actor_user_id, category, event_type, outcome,
          resource_type, resource_id, ip_hash, trace_id, metadata_json, created_at
        )
        SELECT id, organization_id, actor_user_id,
               CASE
                 WHEN event_type LIKE 'retention_policy.%' OR event_type LIKE 'governance.%'
                   THEN 'governance'
                 WHEN event_type ~ '^(candidate|application|job|resume|screening|interview|talent_pool|report_export)\\.'
                   THEN 'recruiting'
                 ELSE 'system'
               END,
               event_type, outcome,
               {resource_type},
               CASE
                 WHEN ({resource_id}) ~* '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[1-5][0-9a-f]{{3}}-[89ab][0-9a-f]{{3}}-[0-9a-f]{{12}}$'
                 THEN ({resource_id})::uuid
                 ELSE NULL
               END,
               NULL, trace_id,
               metadata_json - ARRAY[{removed_keys}]::text[],
               created_at
        FROM audit_logs_0015
        """
    )
    source = bind.execute(
        sa.text(
            """
            SELECT count(*), md5(coalesce(string_agg(
              md5(created_at::text || '|' || id::text || '|' || event_type || '|' || outcome),
              '' ORDER BY created_at, id
            ), '')) FROM audit_logs_0015
            """
        )
    ).one()
    destination = bind.execute(
        sa.text(
            """
            SELECT count(*), md5(coalesce(string_agg(
              md5(created_at::text || '|' || id::text || '|' || event_type || '|' || outcome),
              '' ORDER BY created_at, id
            ), '')) FROM audit_logs
            """
        )
    ).one()
    if source != destination:
        raise RuntimeError("audit_logs copy verification failed")
    print(f"0016 audit copy rows={source[0]} ordered_digest={source[1]}")

    op.drop_table("audit_logs_0015")
    op.execute(
        "CREATE INDEX ix_audit_logs_org_created ON audit_logs "
        "(organization_id, created_at DESC, id DESC)"
    )
    op.execute(
        "CREATE INDEX ix_audit_logs_actor_created ON audit_logs "
        "(organization_id, actor_user_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_audit_logs_event_created ON audit_logs "
        "(organization_id, event_type, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_audit_logs_resource_created ON audit_logs "
        "(organization_id, resource_type, resource_id, created_at DESC)"
    )
    _create_append_only_trigger()
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ensure_audit_log_partitions(months_ahead integer DEFAULT 1)
        RETURNS void LANGUAGE plpgsql AS $$
        DECLARE
          month_start date;
          month_end date;
          partition_name text;
          offset_value integer;
        BEGIN
          IF months_ahead < 0 OR months_ahead > 24 THEN
            RAISE EXCEPTION 'months_ahead must be between 0 and 24';
          END IF;
          FOR offset_value IN 0..months_ahead LOOP
            month_start := (date_trunc('month', current_date) + (offset_value || ' months')::interval)::date;
            month_end := (month_start + interval '1 month')::date;
            partition_name := 'audit_logs_' || to_char(month_start, 'YYYY_MM');
            EXECUTE format(
              'CREATE TABLE IF NOT EXISTS %I PARTITION OF audit_logs FOR VALUES FROM (%L) TO (%L)',
              partition_name, month_start, month_end
            );
          END LOOP;
        END $$
        """
    )


def upgrade() -> None:
    _upgrade_retention()
    op.add_column("candidates", sa.Column("retention_due_at", sa.DateTime(timezone=True)))
    op.add_column(
        "idempotency_records",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE idempotency_records SET expires_at = created_at + interval '24 hours' "
        "WHERE expires_at IS NULL"
    )
    op.alter_column(
        "idempotency_records",
        "expires_at",
        nullable=False,
        server_default=sa.text("now() + interval '24 hours'"),
    )
    op.create_index(
        "ix_idempotency_records_expires_at",
        "idempotency_records",
        ["organization_id", "expires_at"],
    )
    _upgrade_audit()


def _downgrade_audit() -> None:
    op.execute("DROP FUNCTION IF EXISTS ensure_audit_log_partitions(integer)")
    op.execute("DROP TRIGGER audit_logs_append_only ON audit_logs")
    op.execute("ALTER TABLE audit_logs RENAME TO audit_logs_0016")
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    legacy_key = "CASE resource_type " + " ".join(
        f"WHEN '{resource_type}' THEN '{key}'"
        for resource_type, key in LEGACY_KEY_BY_RESOURCE_TYPE.items()
    ) + " ELSE NULL END"
    op.execute(
        f"""
        INSERT INTO audit_logs(
          id, organization_id, actor_user_id, event_type, outcome,
          trace_id, metadata_json, created_at
        )
        SELECT id, organization_id, actor_user_id, event_type, outcome, trace_id,
               metadata_json || CASE
                 WHEN ({legacy_key}) IS NOT NULL AND resource_id IS NOT NULL
                   THEN jsonb_build_object(({legacy_key}), resource_id::text)
                 ELSE '{{}}'::jsonb
               END,
               created_at
        FROM audit_logs_0016
        """
    )
    source_count = op.get_bind().scalar(sa.text("SELECT count(*) FROM audit_logs_0016"))
    destination_count = op.get_bind().scalar(sa.text("SELECT count(*) FROM audit_logs"))
    if source_count != destination_count:
        raise RuntimeError("audit_logs downgrade copy verification failed")
    op.execute("DROP TABLE audit_logs_0016 CASCADE")
    _create_append_only_trigger()


def downgrade() -> None:
    _downgrade_audit()
    op.drop_index("ix_idempotency_records_expires_at", table_name="idempotency_records")
    op.drop_column("idempotency_records", "expires_at")
    op.drop_column("candidates", "retention_due_at")
    op.execute("DROP TRIGGER organizations_seed_retention_policy ON organizations")
    op.execute("DROP FUNCTION seed_organization_retention_policy()")
    op.drop_constraint("fk_organizations_retention_policy", "organizations", type_="foreignkey")
    op.drop_column("organizations", "retention_policy_id")
    op.drop_table("retention_policies")
