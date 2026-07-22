from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Connection, event, insert, select

from server.app.governance.audit import category_for_event
from server.app.governance.models import RetentionPolicy
from server.app.identity.models import AuditLog, Organization, utcnow


_registered = False


def _ensure_policy_pointer(_: Any, __: Connection, organization: Organization) -> None:
    if organization.retention_policy_id is None:
        organization.retention_policy_id = uuid.uuid4()


def _seed_default_policy(_: Any, connection: Connection, organization: Organization) -> None:
    existing_id = connection.scalar(
        select(RetentionPolicy.id).where(
            RetentionPolicy.organization_id == organization.id
        )
    )
    if existing_id is not None:
        if existing_id != organization.retention_policy_id:
            raise RuntimeError("organization retention policy pointer is inconsistent")
        return

    timestamp = utcnow()
    connection.execute(
        insert(RetentionPolicy).values(
            id=organization.retention_policy_id,
            organization_id=organization.id,
            terminal_days=365,
            talent_pool_days=730,
            backup_window_days=90,
            version=1,
            updated_by=None,
            created_at=timestamp,
            updated_at=timestamp,
        )
    )


def _categorize_audit(_: Any, __: Connection, audit: AuditLog) -> None:
    audit.category = category_for_event(audit.event_type)


def register_governance_orm() -> None:
    global _registered
    if _registered:
        return
    event.listen(Organization, "before_insert", _ensure_policy_pointer)
    event.listen(Organization, "after_insert", _seed_default_policy)
    event.listen(AuditLog, "before_insert", _categorize_audit)
    _registered = True
