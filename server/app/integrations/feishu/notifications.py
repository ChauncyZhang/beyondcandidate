from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select

from server.app.communications.models import EmailDelivery
from server.app.integrations.feishu.models import FeishuOrganizationConfig
from server.app.interviews.models import InterviewParticipant
from server.app.queue.payloads import FEISHU_NOTIFICATION_EVENTS
from server.app.queue.repository import QueueRepository


_EMAIL_DELIVERY_EVENTS = {"email_delivery_failed"}
_APPLICATION_EVENTS = FEISHU_NOTIFICATION_EVENTS - _EMAIL_DELIVERY_EVENTS - {
    "interview_scheduled",
    "interview_rescheduled",
    "interview_cancelled",
    "interview_assignment_removed",
    "feedback_requested",
}
_INTERVIEW_EVENTS = FEISHU_NOTIFICATION_EVENTS - _APPLICATION_EVENTS - _EMAIL_DELIVERY_EVENTS


def schedule_feishu_notification(
    db,
    *,
    organization_id: UUID,
    recipient_user_ids: Iterable[UUID],
    event_type: str,
    application_id: UUID | None = None,
    interview_id: UUID | None = None,
    email_delivery_id: UUID | None = None,
    actor_user_id: UUID | None = None,
    exclude_actor: bool = False,
):
    if event_type not in FEISHU_NOTIFICATION_EVENTS:
        raise ValueError("unsupported Feishu notification event")
    if event_type in _APPLICATION_EVENTS and application_id is None:
        raise ValueError("application_id is required for this Feishu notification event")
    if event_type in _INTERVIEW_EVENTS and interview_id is None:
        raise ValueError("interview_id is required for this Feishu notification event")
    if event_type in _EMAIL_DELIVERY_EVENTS and email_delivery_id is None:
        raise ValueError("email_delivery_id is required for this Feishu notification event")
    if not isinstance(organization_id, UUID):
        raise ValueError("organization_id must be a UUID")
    if actor_user_id is not None and not isinstance(actor_user_id, UUID):
        raise ValueError("actor_user_id must be a UUID")

    config = db.scalar(
        select(FeishuOrganizationConfig).where(
            FeishuOrganizationConfig.organization_id == organization_id
        )
    )
    if config is None or not config.enabled:
        return []

    recipients = tuple(
        dict.fromkeys(
            user_id
            for user_id in recipient_user_ids
            if not exclude_actor or user_id != actor_user_id
        )
    )
    if event_type in _EMAIL_DELIVERY_EVENTS:
        delivery = db.scalar(select(EmailDelivery).where(
            EmailDelivery.organization_id == organization_id,
            EmailDelivery.id == email_delivery_id,
        ))
        if delivery is None or delivery.status != "failed" or delivery.created_by is None:
            return []
        recipients = tuple(user_id for user_id in recipients if user_id == delivery.created_by)
    if any(not isinstance(user_id, UUID) for user_id in recipients):
        raise ValueError("recipient_user_ids must contain UUIDs")
    if event_type == "feedback_requested":
        required_feedback_user_ids = set(
            db.scalars(
                select(InterviewParticipant.user_id).where(
                    InterviewParticipant.organization_id == organization_id,
                    InterviewParticipant.interview_id == interview_id,
                    InterviewParticipant.required_feedback.is_(True),
                )
            )
        )
        recipients = tuple(
            user_id for user_id in recipients if user_id in required_feedback_user_ids
        )
    repository = QueueRepository(db)
    events = []
    for recipient_user_id in recipients:
        payload = {
            "organization_id": str(organization_id),
            "recipient_user_id": str(recipient_user_id),
            "event_type": event_type,
        }
        if application_id is not None:
            payload["application_id"] = str(application_id)
        if interview_id is not None:
            payload["interview_id"] = str(interview_id)
        if email_delivery_id is not None:
            payload["email_delivery_id"] = str(email_delivery_id)
        events.append(
            repository.append_outbox(
                organization_id,
                "feishu.notification.send",
                "user",
                recipient_user_id,
                payload,
            )
        )
    return events
