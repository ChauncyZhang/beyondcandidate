from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select

from server.app.integrations.feishu.models import FeishuInterviewSync, FeishuOrganizationConfig
from server.app.queue.repository import QueueRepository


def schedule_interview_sync(database, interview, action: str) -> FeishuInterviewSync:
    if action not in {"create", "update", "cancel"}:
        raise ValueError("unsupported Feishu sync action")
    config = database.scalar(select(FeishuOrganizationConfig).where(FeishuOrganizationConfig.organization_id == interview.organization_id))
    sync = database.scalar(select(FeishuInterviewSync).where(FeishuInterviewSync.organization_id == interview.organization_id, FeishuInterviewSync.interview_id == interview.id).with_for_update())
    if sync is None:
        sync = FeishuInterviewSync(
            organization_id=interview.organization_id,
            interview_id=interview.id,
            desired_action=action,
            sync_status="disabled",
            idempotency_key=uuid4(),
        )
        database.add(sync)
        database.flush()
    effective_action = "create" if action == "update" and sync.external_event_id is None else action
    sync.desired_action = effective_action
    sync.idempotency_key = uuid4()
    sync.last_error_code = None
    sync.next_retry_at = None
    if config is None or not config.enabled:
        sync.sync_status = "disabled"
        return sync
    sync.sync_status = "pending"
    QueueRepository(database).append_outbox(
        interview.organization_id,
        f"feishu.calendar.{effective_action}",
        "interview",
        interview.id,
        {
            "organization_id": str(interview.organization_id),
            "interview_id": str(interview.id),
            "sync_id": str(sync.id),
        },
    )
    return sync


def mark_provider_change(database, organization_id, external_event_id: str, *, provider_revision: str | None = None) -> bool:
    sync = database.scalar(select(FeishuInterviewSync).where(FeishuInterviewSync.organization_id == organization_id, FeishuInterviewSync.external_event_id == external_event_id).with_for_update())
    if sync is None:
        return False
    # Deliberately do not mutate Interview: the ATS schedule remains authoritative.
    sync.sync_status = "pending_confirmation"
    sync.provider_revision = provider_revision
    return True
