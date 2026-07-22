from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select

from server.app.identity.models import Job, User
from server.app.integrations.feishu.models import (
    FeishuIdentityBinding,
    FeishuInterviewSync,
    FeishuOrganizationConfig,
)
from server.app.integrations.feishu.provider import CalendarEventRequest, FeishuCredentials, FeishuProviderError
from server.app.interviews.models import Interview, InterviewParticipant
from server.app.queue.service import PermanentJobError, RetryableJobError
from server.app.recruiting.models import Application, Candidate


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class FeishuCalendarOutboxHandler:
    def __init__(self, sessions, provider, cipher) -> None:
        self._sessions = sessions
        self._provider = provider
        self._cipher = cipher

    async def __call__(self, event, idempotency_key) -> None:
        try:
            organization_id = UUID(event.payload["organization_id"])
            interview_id = UUID(event.payload["interview_id"])
            sync_id = UUID(event.payload["sync_id"])
            if organization_id != event.organization_id or interview_id != event.aggregate_id:
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            raise PermanentJobError("feishu_payload_invalid") from None

        with self._sessions.begin() as db:
            sync = db.scalar(select(FeishuInterviewSync).where(FeishuInterviewSync.organization_id == organization_id, FeishuInterviewSync.id == sync_id).with_for_update())
            interview = db.scalar(select(Interview).where(Interview.organization_id == organization_id, Interview.id == interview_id))
            config = db.scalar(select(FeishuOrganizationConfig).where(FeishuOrganizationConfig.organization_id == organization_id))
            if sync is None or interview is None:
                raise PermanentJobError("feishu_sync_missing")
            if config is None or not config.enabled or config.encrypted_app_secret is None:
                sync.sync_status = "disabled"
                return
            application = db.get(Application, interview.application_id)
            candidate = db.get(Candidate, application.candidate_id) if application else None
            job = db.get(Job, application.job_id) if application else None
            if application is None or candidate is None or job is None:
                raise PermanentJobError("feishu_interview_unavailable")
            action = sync.desired_action
            external_event_id = sync.external_event_id
            credentials = FeishuCredentials(config.app_id, self._cipher.decrypt(config.encrypted_app_secret), config.redirect_uri, config.calendar_id)
            participant_ids = list(
                db.scalars(
                    select(InterviewParticipant.user_id).where(
                        InterviewParticipant.organization_id == organization_id,
                        InterviewParticipant.interview_id == interview_id,
                    )
                )
            )
            attendee_user_ids = tuple(
                dict.fromkeys([interview.created_by, *participant_ids])
            )
            users = {
                user.id: user
                for user in db.scalars(
                    select(User).where(
                        User.organization_id == organization_id,
                        User.id.in_(attendee_user_ids),
                    )
                )
            }
            bindings = {
                binding.user_id: binding
                for binding in db.scalars(
                    select(FeishuIdentityBinding).where(
                        FeishuIdentityBinding.organization_id == organization_id,
                        FeishuIdentityBinding.user_id.in_(attendee_user_ids),
                    )
                )
            }
            open_ids: list[str] = []
            emails: list[str] = []
            for user_id in attendee_user_ids:
                binding = bindings.get(user_id)
                user = users.get(user_id)
                if binding is not None and binding.open_id:
                    open_ids.append(binding.open_id)
                elif user is not None and user.email:
                    emails.append(user.email)
            request = CalendarEventRequest(
                interview_id=interview.id,
                summary=f"{job.title} - {candidate.display_name} - {interview.round_name}",
                starts_at=_aware(interview.starts_at),
                ends_at=_aware(interview.ends_at),
                timezone=interview.timezone,
                description=f"ATS interview {interview.id}",
                location=interview.location or interview.meeting_url or "",
                attendee_open_ids=tuple(dict.fromkeys(open_ids)),
                attendee_emails=tuple(dict.fromkeys(emails)),
            )
            sync.sync_status = "syncing"
            sync.attempts += 1
            sync.last_attempted_at = datetime.now(timezone.utc)
        try:
            if action == "cancel":
                if external_event_id:
                    self._provider.cancel_event(credentials, external_event_id, idempotency_key=str(idempotency_key))
                result = None
            elif action == "update" and external_event_id:
                result = self._provider.update_event(credentials, external_event_id, request, idempotency_key=str(idempotency_key))
            else:
                result = self._provider.create_event(credentials, request, idempotency_key=str(idempotency_key))
        except FeishuProviderError as error:
            with self._sessions.begin() as db:
                locked = db.scalar(select(FeishuInterviewSync).where(FeishuInterviewSync.id == sync_id).with_for_update())
                if locked:
                    locked.sync_status = "failed"
                    locked.last_error_code = error.safe_code
            exception = RetryableJobError if error.retryable else PermanentJobError
            raise exception(error.safe_code) from None

        with self._sessions.begin() as db:
            locked = db.scalar(select(FeishuInterviewSync).where(FeishuInterviewSync.id == sync_id).with_for_update())
            if locked is None:
                raise PermanentJobError("feishu_sync_missing")
            locked.last_error_code = None
            locked.next_retry_at = None
            if action == "cancel":
                locked.sync_status = "cancelled"
            else:
                locked.external_calendar_id = credentials.calendar_id
                locked.external_event_id = result.event_id
                locked.sync_status = "synced"


def build_feishu_outbox_handlers(settings):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from server.app.integrations.feishu.provider import HttpFeishuProvider
    from server.app.integrations.feishu.service import FeishuSecretCipher

    key = settings.feishu_config_encryption_key.get_secret_value()
    if key == "change-me":
        key = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
    sessions = sessionmaker(
        create_engine(settings.database_url.replace("+asyncpg", "+psycopg").replace("+aiosqlite", ""), pool_pre_ping=True),
        expire_on_commit=False,
    )
    handler = FeishuCalendarOutboxHandler(sessions, HttpFeishuProvider(), FeishuSecretCipher(key.encode()))
    return {
        "feishu.calendar.create": handler,
        "feishu.calendar.update": handler,
        "feishu.calendar.cancel": handler,
    }
