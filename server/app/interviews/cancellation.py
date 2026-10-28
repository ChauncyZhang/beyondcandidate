from datetime import datetime, timezone

from sqlalchemy import select

from server.app.identity.models import AuditLog
from server.app.integrations.feishu.notifications import schedule_feishu_notification
from server.app.integrations.feishu.sync import schedule_interview_sync
from server.app.interviews.models import InterviewEvent, InterviewParticipant
from server.app.recruiting.service import InvalidStateTransition


CANCELLABLE_STATUSES = frozenset({"draft", "scheduled", "rescheduled", "confirmed", "completed", "pending_feedback"})


def cancel_interview_record(
    db, interview, *, actor_user_id, trace_id, reason_text,
    notify_candidate=False, request=None,
):
    """Caller holds candidate, application and interview locks, in that order."""
    if interview.status not in CANCELLABLE_STATUSES or not reason_text.strip():
        raise InvalidStateTransition
    source = interview.status
    participants = db.scalars(
        select(InterviewParticipant).where(
            InterviewParticipant.organization_id == interview.organization_id,
            InterviewParticipant.interview_id == interview.id,
        ).order_by(InterviewParticipant.id).with_for_update()
    ).all()
    interview.status = "cancelled"
    interview.calendar_sequence += 1
    interview.version += 1
    interview.updated_at = datetime.now(timezone.utc)
    for participant in participants:
        if participant.task_status != "completed":
            participant.task_status = "cancelled"
    db.add(InterviewEvent(
        organization_id=interview.organization_id,
        interview_id=interview.id,
        actor_user_id=actor_user_id,
        event_type="interview.cancelled",
        payload={"from_status": source, "to_status": "cancelled", "reason": reason_text.strip(), "application_advanced": False},
    ))
    db.add(AuditLog(
        organization_id=interview.organization_id, actor_user_id=actor_user_id,
        event_type="interview.cancelled", outcome="success", trace_id=trace_id,
        resource_type="interview", resource_id=interview.id,
        metadata_json={"from_status": source, "to_status": "cancelled"},
    ))
    db.flush()
    if notify_candidate and request is not None:
        from server.app.interviews.api import _enqueue_candidate_interview_email

        _enqueue_candidate_interview_email(
            request, db, interview, "interview_cancelled",
            actor_user_id=actor_user_id, manual_on_error=True,
        )
    schedule_interview_sync(db, interview, "cancel")
    schedule_feishu_notification(
        db, organization_id=interview.organization_id,
        recipient_user_ids=[p.user_id for p in participants],
        event_type="interview_cancelled", application_id=interview.application_id,
        interview_id=interview.id, actor_user_id=actor_user_id, exclude_actor=True,
    )
    db.flush()
    return interview
