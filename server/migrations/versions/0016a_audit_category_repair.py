"""Repair post-0016 audit categories before deletion governance."""

from __future__ import annotations

from alembic import op


revision = "0016a_audit_category_repair"
down_revision = "0016_governance_audit_retention"
branch_labels = None
depends_on = None


_CATEGORY_SQL = """
CASE
  WHEN event_type LIKE 'retention_policy.%' OR event_type LIKE 'governance.%'
    THEN 'governance'
  WHEN event_type ~ '^(candidate|application|job|resume|screening|interview|talent_pool|report_export)\\.'
    THEN 'recruiting'
  WHEN event_type LIKE 'llm.%'
    THEN 'system'
  ELSE 'system'
END
"""


def upgrade() -> None:
    op.execute("DROP TRIGGER audit_logs_append_only ON audit_logs")
    op.execute(
        f"""
        UPDATE audit_logs
        SET category = {_CATEGORY_SQL}
        WHERE category IS DISTINCT FROM ({_CATEGORY_SQL})
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_logs_append_only
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION reject_audit_log_mutation()
        """
    )


def downgrade() -> None:
    # The repaired classification is authoritative data and remains valid at 0016.
    pass
