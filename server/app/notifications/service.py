import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from server.app.notifications.models import NotificationRead


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
