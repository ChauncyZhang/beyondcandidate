from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select

from server.app.communications.interview_messages import AUTO_USABLE_CANDIDATE_EMAIL_SOURCES
from server.app.communications.models import EmailDelivery
from server.app.identity.models import Job, User, UserStatus
from server.app.integrations.feishu.models import (
    FeishuDepartmentMapping,
    FeishuIdentityBinding,
    FeishuInterviewSync,
    FeishuOnboardingConfig,
    FeishuOrganizationConfig,
)
from server.app.integrations.feishu.notifications import FEISHU_NOTIFICATION_EVENTS
from server.app.integrations.feishu.provider import ApprovalInstanceRequest, CalendarEventRequest, FeishuCredentials, FeishuProviderError
from server.app.interviews.models import Interview, InterviewParticipant
from server.app.notifications.models import UserNotification
from server.app.onboarding.models import OnboardingRecord
from server.app.onboarding.service import validate_definition
from server.app.queue.service import PermanentJobError, RetryableJobError
from server.app.recruiting.models import Application, ApplicationReviewTask, Candidate, CandidateContact
from server.app.recruiting.review_assignments import review_notification_user_ids


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class FeishuCalendarOutboxHandler:
    def __init__(self, sessions, provider, cipher, contact_cipher=None) -> None:
        self._sessions = sessions
        self._provider = provider
        self._cipher = cipher
        self._contact_cipher = contact_cipher

    def _candidate_email(self, db, organization_id, candidate_id) -> str | None:
        if self._contact_cipher is None:
            return None
        contacts = list(
            db.scalars(
                select(CandidateContact)
                .where(
                    CandidateContact.organization_id == organization_id,
                    CandidateContact.candidate_id == candidate_id,
                    CandidateContact.kind == "email",
                    or_(
                        CandidateContact.confirmation_status == "confirmed",
                        CandidateContact.source.in_(AUTO_USABLE_CANDIDATE_EMAIL_SOURCES),
                    ),
                )
                .order_by(
                    (CandidateContact.confirmation_status == "confirmed").desc(),
                    CandidateContact.confirmed_at.desc(),
                    CandidateContact.created_at.desc(),
                    CandidateContact.id.desc(),
                )
                .limit(2)
            ).all()
        )
        if not contacts:
            return None
        contact = contacts[0]
        if contact.confirmation_status != "confirmed" and len(contacts) > 1:
            return None
        try:
            return self._contact_cipher.decrypt(contact.ciphertext)
        except ValueError:
            return None

    async def __call__(self, event, idempotency_key) -> None:
        try:
            organization_id = UUID(event.payload["organization_id"])
            interview_id = UUID(event.payload["interview_id"])
            sync_id = UUID(event.payload["sync_id"])
            sync_generation = (
                UUID(event.payload["sync_generation"])
                if event.payload.get("sync_generation") is not None
                else None
            )
            if organization_id != event.organization_id or interview_id != event.aggregate_id:
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            raise PermanentJobError("feishu_payload_invalid") from None

        provider_error: FeishuProviderError | None = None
        with self._sessions.begin() as db:
            # Keep the interview and its sync row locked until the provider
            # result is persisted. A concurrent reschedule then observes the
            # external event id and queues an update instead of a second create.
            interview = db.scalar(
                select(Interview)
                .where(
                    Interview.organization_id == organization_id,
                    Interview.id == interview_id,
                )
                .with_for_update()
            )
            sync = db.scalar(select(FeishuInterviewSync).where(FeishuInterviewSync.organization_id == organization_id, FeishuInterviewSync.id == sync_id).with_for_update())
            config = db.scalar(select(FeishuOrganizationConfig).where(FeishuOrganizationConfig.organization_id == organization_id))
            if sync is None or interview is None:
                raise PermanentJobError("feishu_sync_missing")
            if sync_generation is not None and sync.idempotency_key != sync_generation:
                return
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
            calendar_id = config.calendar_id
            if action in {"update", "cancel"} and external_event_id and sync.external_calendar_id:
                calendar_id = sync.external_calendar_id
            credentials = FeishuCredentials(config.app_id, self._cipher.decrypt(config.encrypted_app_secret), config.redirect_uri, calendar_id)
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
            candidate_email = self._candidate_email(
                db,
                organization_id,
                candidate.id,
            )
            if candidate_email:
                emails.append(candidate_email)
            request = CalendarEventRequest(
                interview_id=interview.id,
                summary=f"{job.title} - {candidate.display_name} - {interview.round_name}",
                starts_at=_aware(interview.starts_at),
                ends_at=_aware(interview.ends_at),
                timezone=interview.timezone,
                description=f"ATS interview {interview.id}",
                location=interview.location or "",
                video_conference=interview.method == "video",
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
                sync.sync_status = "failed"
                sync.last_error_code = error.safe_code
                provider_error = error
            else:
                sync.last_error_code = None
                sync.next_retry_at = None
                if action == "cancel":
                    sync.sync_status = "cancelled"
                else:
                    sync.external_calendar_id = result.calendar_id or credentials.calendar_id
                    sync.external_event_id = result.event_id
                    sync.sync_status = "synced"
                    if request.video_conference and interview.method == "video":
                        if result.meeting_url is not None:
                            interview.meeting_url = result.meeting_url
                    elif interview.method != "video":
                        interview.meeting_url = None

        if provider_error is not None:
            exception = RetryableJobError if provider_error.retryable else PermanentJobError
            raise exception(provider_error.safe_code) from None


def _platform_origin(redirect_uri: str) -> str:
    parsed = urlsplit(redirect_uri)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PermanentJobError("feishu_config_invalid")
    return f"{parsed.scheme}://{parsed.netloc}"


def _candidate_link(origin: str, candidate_id: UUID, application_id: UUID, job_id: UUID) -> str:
    query = urlencode({"application": str(application_id), "job": str(job_id)})
    return f"{origin}/candidates/{candidate_id}?{query}"


def _interview_time_label(interview: Interview) -> str:
    starts_at = _aware(interview.starts_at)
    try:
        starts_at = starts_at.astimezone(ZoneInfo(interview.timezone))
    except (TypeError, ZoneInfoNotFoundError):
        starts_at = starts_at.astimezone(timezone.utc)
    return starts_at.strftime("%Y-%m-%d %H:%M")


def _escape_lark_md(value: object) -> str:
    replacements = {
        "&": "&#38;", "<": "&#60;", ">": "&#62;", "*": "&#42;",
        "_": "&#95;", "~": "&#126;", "[": "&#91;", "]": "&#93;",
        "(": "&#40;", ")": "&#41;", "#": "&#35;", "`": "&#96;",
        "\\": "&#92;", ":": "&#58;",
    }
    return "".join(replacements.get(character, character) for character in str(value))


def _notification_card(event_type, *, origin, application, candidate, job, interview, email_delivery=None) -> dict:
    if event_type == "email_delivery_failed":
        if email_delivery is None:
            raise PermanentJobError("feishu_notification_data_missing")
        title = "候选人邮件发送失败"
        description = "一封候选人邮件未能成功发送。"
        guidance = "请检查邮件配置或更正收件信息后重试。"
        action_label = "查看邮件记录"
        action_url = f"{origin}/settings/email?{urlencode({'delivery_id': str(email_delivery.id)})}"
        template, tag_text, tag_color = "red", "发送失败", "red"
        icon = "warning_outlined"
        fields = [("收件人", email_delivery.recipient_masked), ("错误代码", email_delivery.safe_error_code or "email_delivery_failed")]
    elif event_type == "interview_assignment_removed":
        title = "面试安排已变更"
        description = "你已被移出一场面试安排。"
        guidance = "如飞书日历仍保留旧日程，请忽略该日程。"
        action_label = "查看面试安排"
        action_url = f"{origin}/interviews"
        template, tag_text, tag_color = "grey", "已变更", "neutral"
        icon = "calendar_outlined"
        fields = [("变更类型", "面试参与人调整"), ("处理建议", "以招聘平台中的最新安排为准")]
    else:
        candidate_link = _candidate_link(origin, candidate.id, application.id, job.id)
        fields = [("候选人", candidate.display_name), ("应聘岗位", job.title)]
        template, tag_text, tag_color = "blue", "待处理", "yellow"
        icon = "bell_outlined"
        if event_type == "review_requested":
            title = "候选人评审待处理"
            description = "请查看候选人资料并提交评审结论。"
            guidance = "评审结果将自动推进后续招聘流程。"
            action_label, action_url = "去评审", candidate_link
        elif event_type == "interview_arrangement_requested":
            title = "面试待安排"
            description = "候选人已通过评审，请安排面试。"
            guidance = "选择面试官和时间后，系统会同步发送通知。"
            action_label = "安排面试"
            action_url = f"{origin}/interviews/new?candidate={candidate.id}"
            icon = "calendar_outlined"
        elif event_type == "next_interview_requested":
            title = "下一轮面试待安排"
            description = "上一轮评价已完成，请继续安排下一轮面试。"
            guidance = "系统会推荐下一轮，也允许追加面试轮次。"
            action_label = "安排下一轮"
            action_url = f"{origin}/interviews/new?candidate={candidate.id}"
            icon = "calendar_outlined"
        elif event_type == "hiring_decision_requested":
            title = "录用决策待处理"
            description = "所有面试评价已完成，请作出录用决策。"
            guidance = "请结合简历、面试记录和完整评价进行判断。"
            action_label, action_url = "去决策", candidate_link
            icon = "approval_outlined"
        elif event_type == "candidate_passed":
            title, description = "候选人已通过", "候选人已通过当前招聘流程。"
            guidance = "可在候选人详情中查看完整招聘记录。"
            action_label, action_url = "查看候选人", candidate_link
            template, tag_text, tag_color, icon = "green", "已通过", "green", "done_outlined"
        elif event_type == "candidate_rejected":
            title, description = "招聘流程已结束", "该候选人的招聘流程已结束。"
            guidance = "可在候选人详情中查看流程记录。"
            action_label, action_url = "查看候选人", candidate_link
            template, tag_text, tag_color, icon = "grey", "已结束", "neutral", "close_outlined"
        elif event_type == "offer_accepted":
            title, description = "Offer 已接受", "候选人已接受 Offer。"
            guidance = "可在候选人详情中查看录用记录。"
            action_label, action_url = "查看录用记录", candidate_link
            template, tag_text, tag_color, icon = "green", "已接受", "green", "done_outlined"
        elif event_type == "offer_declined":
            title, description = "Offer 已拒绝", "候选人已拒绝 Offer。"
            guidance = "可在候选人详情中查看完整记录。"
            action_label, action_url = "查看候选人", candidate_link
            template, tag_text, tag_color, icon = "grey", "已拒绝", "neutral", "close_outlined"
        else:
            if interview is None:
                raise PermanentJobError("feishu_notification_data_missing")
            fields.extend([("面试轮次", interview.round_name), ("面试时间", _interview_time_label(interview))])
            icon = "calendar_outlined"
            if event_type == "interview_scheduled":
                title, description = "面试已安排", "新的面试安排已确认。"
                guidance = "请提前查看候选人资料并按时参加。"
                action_label, action_url = "查看面试", f"{origin}/interviews"
                template, tag_text, tag_color = "green", "已安排", "green"
            elif event_type == "interview_rescheduled":
                title, description = "面试时间已调整", "面试时间或参与人已发生调整。"
                guidance = "请以招聘平台和飞书日历中的最新安排为准。"
                action_label = "查看新安排"
                action_url = f"{origin}/interviews/{interview.id}/reschedule"
                template, tag_text, tag_color = "orange", "已调整", "orange"
            elif event_type == "interview_cancelled":
                title, description = "面试已取消", "该场面试已取消。"
                guidance = "无需继续准备或提交本场面试评价。"
                action_label, action_url = "查看面试", f"{origin}/interviews"
                template, tag_text, tag_color = "grey", "已取消", "neutral"
            elif event_type == "feedback_requested":
                title, description = "面试评价待提交", "请提交本场面试评价。"
                guidance = "所有必填评价完成后，系统会自动推进招聘流程。"
                action_label = "提交评价"
                action_url = f"{origin}/interviews/{interview.id}/feedback"
            else:
                raise PermanentJobError("feishu_payload_invalid")

    field_items = [
        {
            "is_short": True,
            "text": {"tag": "lark_md", "content": f"**{label}**\n{_escape_lark_md(value)}"},
        }
        for label, value in fields
    ]
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "default",
            "summary": {"content": title},
        },
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": "BeyondCandidate 招聘协作"},
            "template": template,
            "icon": {"tag": "standard_icon", "token": icon},
            "text_tag_list": [{
                "tag": "text_tag",
                "text": {"tag": "plain_text", "content": tag_text},
                "color": tag_color,
            }],
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 20px 12px",
            "vertical_spacing": "12px",
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"**{description}**\n<font color='grey'>{guidance}</font>",
                },
                {"tag": "div", "fields": field_items},
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": action_label},
                    "type": "primary_filled",
                    "width": "fill",
                    "behaviors": [{"type": "open_url", "default_url": action_url}],
                },
            ],
        },
    }


def _notification_recipient_is_current(
    db,
    *,
    organization_id: UUID,
    recipient_user_id: UUID,
    event_type: str,
    application: Application | None,
    job: Job | None,
    interview: Interview | None,
    email_delivery: EmailDelivery | None = None,
    user_notification: UserNotification | None = None,
) -> bool:
    participant = None
    if interview is not None:
        participant = db.scalar(
            select(InterviewParticipant).where(
                InterviewParticipant.organization_id == organization_id,
                InterviewParticipant.interview_id == interview.id,
                InterviewParticipant.user_id == recipient_user_id,
            )
        )
    if event_type == "interview_assignment_removed":
        return interview is not None and participant is None
    if event_type == "email_delivery_failed":
        return (
            email_delivery is not None and email_delivery.status == "failed"
            and user_notification is not None
            and user_notification.user_id == recipient_user_id
            and user_notification.event_type == "email_delivery_failed"
            and user_notification.resource_type == "email_delivery"
            and user_notification.resource_id == email_delivery.id
        )
    if application is None or job is None:
        return False
    if event_type == "review_requested":
        return application.stage == "review" and db.scalar(
            select(ApplicationReviewTask.id).where(
                ApplicationReviewTask.organization_id == organization_id,
                ApplicationReviewTask.application_id == application.id,
                ApplicationReviewTask.assignee_id == recipient_user_id,
                ApplicationReviewTask.status == "open",
            )
        ) is not None
    if event_type in {"interview_arrangement_requested", "next_interview_requested"}:
        return application.stage == "interview_pending" and application.owner_id == recipient_user_id
    if event_type == "hiring_decision_requested":
        return application.stage == "decision" and recipient_user_id in review_notification_user_ids(db, job)
    if event_type == "candidate_passed":
        return application.stage == "passed" and application.owner_id == recipient_user_id
    if event_type == "candidate_rejected":
        return application.stage == "rejected" and application.owner_id == recipient_user_id
    if event_type == "offer_accepted":
        return application.stage == "hired" and application.owner_id == recipient_user_id
    if event_type == "offer_declined":
        return application.stage == "withdrawn" and application.owner_id == recipient_user_id
    if participant is None or interview is None:
        return False
    if event_type == "interview_scheduled":
        return interview.status == "scheduled"
    if event_type == "interview_rescheduled":
        return interview.status == "rescheduled"
    if event_type == "interview_cancelled":
        return interview.status == "cancelled"
    if event_type == "feedback_requested":
        return (
            interview.status == "pending_feedback"
            and participant.required_feedback
            and participant.task_status == "ready"
        )
    return False


class FeishuNotificationOutboxHandler:
    def __init__(self, sessions, provider, cipher) -> None:
        self._sessions = sessions
        self._provider = provider
        self._cipher = cipher

    async def __call__(self, event, idempotency_key) -> None:
        try:
            organization_id = UUID(event.payload["organization_id"])
            recipient_user_id = UUID(event.payload["recipient_user_id"])
            event_type = event.payload["event_type"]
            application_id = UUID(event.payload["application_id"]) if "application_id" in event.payload else None
            interview_id = UUID(event.payload["interview_id"]) if "interview_id" in event.payload else None
            email_delivery_id = UUID(event.payload["email_delivery_id"]) if "email_delivery_id" in event.payload else None
            if (
                organization_id != event.organization_id
                or recipient_user_id != event.aggregate_id
                or event.aggregate_type != "user"
                or event_type not in FEISHU_NOTIFICATION_EVENTS
                or (event_type == "email_delivery_failed" and (email_delivery_id is None or application_id is not None or interview_id is not None))
                or (event_type != "email_delivery_failed" and email_delivery_id is not None)
            ):
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            raise PermanentJobError("feishu_payload_invalid") from None

        with self._sessions.begin() as db:
            config = db.scalar(
                select(FeishuOrganizationConfig).where(
                    FeishuOrganizationConfig.organization_id == organization_id
                )
            )
            if config is None or not config.enabled or config.encrypted_app_secret is None:
                return
            user = db.scalar(
                select(User).where(
                    User.organization_id == organization_id,
                    User.id == recipient_user_id,
                )
            )
            binding = db.scalar(
                select(FeishuIdentityBinding).where(
                    FeishuIdentityBinding.organization_id == organization_id,
                    FeishuIdentityBinding.user_id == recipient_user_id,
                )
            )
            if user is None or user.status != UserStatus.ACTIVE:
                raise PermanentJobError("feishu_recipient_inactive")
            if binding is None or not binding.open_id:
                raise PermanentJobError("feishu_recipient_unbound")

            interview = None
            if interview_id is not None:
                interview = db.scalar(
                    select(Interview).where(
                        Interview.organization_id == organization_id,
                        Interview.id == interview_id,
                    )
                )
                if interview is None:
                    raise PermanentJobError("feishu_notification_data_missing")
                if application_id is not None and application_id != interview.application_id:
                    raise PermanentJobError("feishu_payload_invalid")
                application_id = interview.application_id
            origin = _platform_origin(config.redirect_uri)
            credentials = FeishuCredentials(
                config.app_id,
                self._cipher.decrypt(config.encrypted_app_secret),
                config.redirect_uri,
                config.calendar_id,
            )
            if event_type == "email_delivery_failed":
                email_delivery = db.scalar(select(EmailDelivery).where(
                    EmailDelivery.organization_id == organization_id,
                    EmailDelivery.id == email_delivery_id,
                ))
                if email_delivery is None:
                    raise PermanentJobError("feishu_notification_data_missing")
                user_notification = db.scalar(select(UserNotification).where(
                    UserNotification.organization_id == organization_id,
                    UserNotification.user_id == recipient_user_id,
                    UserNotification.event_type == "email_delivery_failed",
                    UserNotification.resource_type == "email_delivery",
                    UserNotification.resource_id == email_delivery_id,
                ))
                if not _notification_recipient_is_current(
                    db, organization_id=organization_id, recipient_user_id=recipient_user_id,
                    event_type=event_type, application=None, job=None, interview=None,
                    email_delivery=email_delivery, user_notification=user_notification,
                ):
                    return
                card = _notification_card(
                    event_type, origin=origin, application=None, candidate=None, job=None,
                    interview=None, email_delivery=email_delivery,
                )
                open_id = binding.open_id
            elif event_type == "interview_assignment_removed":
                if not _notification_recipient_is_current(
                    db,
                    organization_id=organization_id,
                    recipient_user_id=recipient_user_id,
                    event_type=event_type,
                    application=None,
                    job=None,
                    interview=interview,
                ):
                    return
                card = _notification_card(
                    event_type,
                    origin=origin,
                    application=None,
                    candidate=None,
                    job=None,
                    interview=interview,
                )
                open_id = binding.open_id
            else:
                if application_id is None:
                    raise PermanentJobError("feishu_payload_invalid")
                application = db.scalar(
                    select(Application).where(
                        Application.organization_id == organization_id,
                        Application.id == application_id,
                    )
                )
                if application is None:
                    raise PermanentJobError("feishu_notification_data_missing")
                candidate = db.scalar(
                    select(Candidate).where(
                        Candidate.organization_id == organization_id,
                        Candidate.id == application.candidate_id,
                        Candidate.deleted_at.is_(None),
                    )
                )
                job = db.scalar(
                    select(Job).where(
                        Job.organization_id == organization_id,
                        Job.id == application.job_id,
                    )
                )
                if candidate is None or job is None:
                    raise PermanentJobError("feishu_notification_data_missing")
                if not _notification_recipient_is_current(
                    db,
                    organization_id=organization_id,
                    recipient_user_id=recipient_user_id,
                    event_type=event_type,
                    application=application,
                    job=job,
                    interview=interview,
                ):
                    return
                card = _notification_card(
                    event_type,
                    origin=origin,
                    application=application,
                    candidate=candidate,
                    job=job,
                    interview=interview,
                )
                open_id = binding.open_id

        try:
            self._provider.send_card(
                credentials,
                open_id,
                card,
                idempotency_key=str(idempotency_key),
            )
        except FeishuProviderError as error:
            exception = RetryableJobError if error.retryable else PermanentJobError
            raise exception(error.safe_code) from None


def _approval_form_value(semantic: str, mapping: dict, value, *, feishu_department_id: str):
    control_type = mapping["control_type"]
    if semantic == "department":
        return [{"open_id": feishu_department_id}]
    if semantic == "gender":
        return (mapping.get("options") or {}).get(value, value)
    if semantic == "phone" and control_type == "telephone":
        digits = str(value).lstrip("+")
        if digits.startswith("86") and len(digits) > 11:
            return {"countryCode": "+86", "nationalNumber": digits[2:]}
        return {"countryCode": "+86", "nationalNumber": digits}
    if semantic == "expected_start_date" and control_type == "date":
        return f"{value}T00:00:00+08:00"
    return value


class FeishuOnboardingOutboxHandler:
    def __init__(self, sessions, provider, feishu_cipher, onboarding_cipher) -> None:
        self._sessions = sessions
        self._provider = provider
        self._feishu_cipher = feishu_cipher
        self._onboarding_cipher = onboarding_cipher

    def _fail(self, organization_id: UUID, onboarding_id: UUID, generation: UUID, safe_code: str) -> None:
        with self._sessions.begin() as db:
            record = db.scalar(select(OnboardingRecord).where(
                OnboardingRecord.organization_id == organization_id,
                OnboardingRecord.id == onboarding_id,
                OnboardingRecord.generation == generation,
            ).with_for_update())
            if record is None or record.status == "submitted":
                return
            record.status = "failed"
            record.failed_at = datetime.now(timezone.utc)
            record.safe_error_code = safe_code
            record.version += 1

    async def __call__(self, event, idempotency_key) -> None:
        try:
            organization_id = UUID(event.payload["organization_id"])
            onboarding_id = UUID(event.payload["onboarding_id"])
            generation = UUID(event.payload["generation"])
        except (KeyError, TypeError, ValueError):
            raise PermanentJobError("feishu_payload_invalid") from None

        with self._sessions.begin() as db:
            record = db.scalar(select(OnboardingRecord).where(
                OnboardingRecord.organization_id == organization_id,
                OnboardingRecord.id == onboarding_id,
            ).with_for_update())
            if record is None:
                raise PermanentJobError("feishu_onboarding_missing")
            if record.generation != generation or record.status == "submitted":
                return
            if record.status not in {"submitting", "failed"}:
                raise PermanentJobError("feishu_onboarding_state_invalid")
            config = db.scalar(select(FeishuOnboardingConfig).where(
                FeishuOnboardingConfig.organization_id == organization_id,
                FeishuOnboardingConfig.enabled.is_(True),
                FeishuOnboardingConfig.validation_status == "valid",
            ))
            base = db.scalar(select(FeishuOrganizationConfig).where(
                FeishuOrganizationConfig.organization_id == organization_id,
                FeishuOrganizationConfig.enabled.is_(True),
            ))
            binding = db.scalar(select(FeishuIdentityBinding).where(
                FeishuIdentityBinding.organization_id == organization_id,
                FeishuIdentityBinding.user_id == record.started_by,
            ))
            department = db.scalar(select(FeishuDepartmentMapping).where(
                FeishuDepartmentMapping.organization_id == organization_id,
                FeishuDepartmentMapping.department_id == record.department_id,
            ))
            if config is None or base is None or base.encrypted_app_secret is None:
                safe_code = "feishu_onboarding_not_configured"
            elif binding is None or not binding.open_id:
                safe_code = "feishu_onboarding_initiator_unbound"
            elif department is None:
                safe_code = "feishu_department_unmapped"
            else:
                safe_code = None
            preflight_error = safe_code
            if preflight_error:
                record.status = "failed"
                record.failed_at = datetime.now(timezone.utc)
                record.safe_error_code = preflight_error
                record.version += 1
            elif record.status == "failed":
                record.status = "submitting"
                record.failed_at = None
                record.safe_error_code = None
                record.version += 1
            if not preflight_error:
                credentials = FeishuCredentials(
                    base.app_id,
                    self._feishu_cipher.decrypt(base.encrypted_app_secret),
                    base.redirect_uri,
                    base.calendar_id,
                )
                pii = self._onboarding_cipher.decrypt(record.pii_ciphertext)
                field_mapping = dict(config.field_mapping)
                expected_fingerprint = config.definition_fingerprint
                approval_code = config.approval_code
                initiator_open_id = binding.open_id
                feishu_department_id = department.feishu_department_id
                source = {
                    "candidate_name": pii["name"],
                    "gender": pii["gender"],
                    "department": record.department_name,
                    "job_title": record.job_title,
                    "phone": pii["phone"],
                    "email": pii["email"],
                    "home_address": pii["home_address"],
                    "expected_start_date": record.expected_start_date.isoformat(),
                }

        if preflight_error:
            raise PermanentJobError(preflight_error)

        try:
            definition = self._provider.get_approval_definition(credentials, approval_code)
            validation_error = validate_definition(field_mapping, definition)
            if validation_error or definition.fingerprint != expected_fingerprint:
                raise FeishuProviderError(
                    validation_error or "feishu_approval_definition_changed",
                    retryable=False,
                )
            form = tuple(
                {
                    "id": mapping["control_id"],
                    "type": mapping["control_type"],
                    "value": _approval_form_value(
                        semantic,
                        mapping,
                        source[semantic],
                        feishu_department_id=feishu_department_id,
                    ),
                }
                for semantic, mapping in field_mapping.items()
            )
            result = self._provider.create_approval_instance(
                credentials,
                ApprovalInstanceRequest(
                    approval_code=approval_code,
                    initiator_open_id=initiator_open_id,
                    department_id=None,
                    form=form,
                ),
                idempotency_key=str(generation),
            )
        except FeishuProviderError as error:
            if error.provider_code == 60012:
                try:
                    result = self._provider.find_approval_instance_by_uuid(
                        credentials,
                        approval_code,
                        str(generation),
                        started_at=record.started_at,
                    )
                except FeishuProviderError:
                    if event.attempts >= event.max_attempts:
                        self._fail(organization_id, onboarding_id, generation, "feishu_approval_reconciliation_required")
                        raise PermanentJobError("feishu_approval_reconciliation_required") from None
                    raise RetryableJobError("feishu_approval_reconciliation_pending") from None
                if result is None:
                    if event.attempts >= event.max_attempts:
                        self._fail(organization_id, onboarding_id, generation, "feishu_approval_reconciliation_required")
                        raise PermanentJobError("feishu_approval_reconciliation_required") from None
                    raise RetryableJobError("feishu_approval_reconciliation_pending") from None
            else:
                safe_code = error.safe_code
                if error.retryable and event.attempts < event.max_attempts:
                    raise RetryableJobError(safe_code) from None
                if error.retryable:
                    safe_code = "feishu_approval_reconciliation_required"
                self._fail(organization_id, onboarding_id, generation, safe_code)
                raise PermanentJobError(safe_code) from None

        with self._sessions.begin() as db:
            record = db.scalar(select(OnboardingRecord).where(
                OnboardingRecord.organization_id == organization_id,
                OnboardingRecord.id == onboarding_id,
                OnboardingRecord.generation == generation,
            ).with_for_update())
            if record is None or record.status == "submitted":
                return
            record.status = "submitted"
            record.feishu_instance_code = result.instance_code
            record.submitted_at = datetime.now(timezone.utc)
            record.failed_at = None
            record.safe_error_code = None
            record.version += 1


def build_feishu_outbox_handlers(settings):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from server.app.integrations.feishu.provider import HttpFeishuProvider
    from server.app.integrations.feishu.service import FeishuSecretCipher
    from server.app.communications.extraction import contact_cipher_from_settings
    from server.app.onboarding.security import OnboardingPiiCipher

    key = settings.feishu_config_encryption_key.get_secret_value()
    if key == "change-me":
        key = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
    sessions = sessionmaker(
        create_engine(settings.database_url.replace("+asyncpg", "+psycopg").replace("+aiosqlite", ""), pool_pre_ping=True),
        expire_on_commit=False,
    )
    provider = HttpFeishuProvider()
    cipher = FeishuSecretCipher(key.encode())
    handler = FeishuCalendarOutboxHandler(
        sessions,
        provider,
        cipher,
        contact_cipher_from_settings(settings),
    )
    notification_handler = FeishuNotificationOutboxHandler(sessions, provider, cipher)
    contact_key = settings.contact_encryption_key.get_secret_value()
    if contact_key == "change-me":
        contact_key = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
    onboarding_handler = FeishuOnboardingOutboxHandler(
        sessions,
        provider,
        cipher,
        OnboardingPiiCipher(contact_key.encode()),
    )
    return {
        "feishu.calendar.create": handler,
        "feishu.calendar.update": handler,
        "feishu.calendar.cancel": handler,
        "feishu.notification.send": notification_handler,
        "feishu.approval.onboarding.create": onboarding_handler,
    }
