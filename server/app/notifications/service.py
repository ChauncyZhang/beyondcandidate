import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from server.app.notifications.models import NotificationRead, UserNotification


def _serialized_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def workbench_notification_version(
    row: Any,
    *,
    stage: str | None = None,
    task_id: UUID | None = None,
    ai_status: str | None = None,
    config_warning: bool | None = None,
) -> str:
    payload = {
        "application_id": str(row.application_id),
        "stage": stage or row.stage,
        "application_version": row.application_version,
        "application_updated_at": _serialized_datetime(row.updated_at),
        "task_id": str(task_id) if task_id is not None else None,
        "ai_status": ai_status,
        "config_warning": config_warning,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def read_versions(db, organization_id: UUID, user_id: UUID, application_ids: list[UUID]) -> dict[UUID, str]:
    if not application_ids:
        return {}
    rows = db.execute(
        select(NotificationRead.application_id, NotificationRead.notification_version).where(
            NotificationRead.organization_id == organization_id,
            NotificationRead.user_id == user_id,
            NotificationRead.application_id.in_(application_ids),
        )
    ).all()
    return {application_id: version for application_id, version in rows}


def create_user_notification(
    db,
    *,
    organization_id: UUID,
    user_id: UUID,
    event_type: str,
    resource_type: str,
    resource_id: UUID,
    recipient_masked: str,
    safe_error_code: str,
) -> UserNotification:
    identity = {
        "organization_id": organization_id, "user_id": user_id, "event_type": event_type,
        "resource_type": resource_type, "resource_id": resource_id,
    }
    predicates = tuple(getattr(UserNotification, key) == value for key, value in identity.items())
    existing = db.scalar(select(UserNotification).where(*predicates))
    if existing is not None:
        return existing
    values = {
        "id": uuid.uuid4(), **identity, "recipient_masked": recipient_masked,
        "safe_error_code": safe_error_code, "created_at": datetime.now(timezone.utc),
    }
    dialect = db.get_bind().dialect.name
    statement = postgresql_insert(UserNotification) if dialect == "postgresql" else sqlite_insert(UserNotification) if dialect == "sqlite" else None
    if statement is None:
        notification = UserNotification(**values)
        db.add(notification); db.flush()
        return notification
    db.execute(statement.values(**values).on_conflict_do_nothing(
        index_elements=["organization_id", "user_id", "event_type", "resource_type", "resource_id"],
    ))
    return db.scalar(select(UserNotification).where(*predicates))
