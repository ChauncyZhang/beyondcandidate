from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from server.app.communications.models import EmailDelivery, EmailProviderConfig
from server.app.communications.security import EmailSecretCipher
from server.app.communications.service import DeliveryCommand, SenderPolicy, enqueue_delivery
from server.app.identity.models import Job, User, UserStatus
from server.app.interviews.domain import CalendarContact, build_calendar_invitation
from server.app.interviews.models import Interview
from server.app.recruiting.models import Application, Candidate, CandidateContact
from server.app.recruiting.security import ContactCipher


INTERVIEW_MESSAGE_KINDS = frozenset(
    {"interview_invitation", "interview_rescheduled", "interview_cancelled"}
)


class CandidateEmailUnavailable(ValueError):
    safe_code = "candidate_email_unconfirmed"


class EmailConfigurationUnavailable(ValueError):
    safe_code = "email_not_configured"


@dataclass(frozen=True)
class InterviewMessage:
    kind: str
    subject: str
    body: str
    attachment_filename: str
    attachment_content_type: str
    attachment_content: bytes


def _single_line(value: str) -> str:
    if not value or "\r" in value or "\n" in value:
        raise ValueError("header_injection")
    return value


def _calendar_contacts(values: list[dict]) -> tuple[CalendarContact, ...]:
    contacts = []
    for value in values:
        try:
            contacts.append(CalendarContact(name=value["name"], email=value["email"]))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(contacts)


def render_interview_message(
    *,
    kind: str,
    interview: Interview,
    candidate_name: str,
    candidate_email: str,
    job_title: str,
    organizer: CalendarContact,
    dtstamp: datetime,
) -> InterviewMessage:
    if kind not in INTERVIEW_MESSAGE_KINDS:
        raise ValueError("interview_message_kind_invalid")
    try:
        local_timezone = ZoneInfo(interview.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("interview_timezone_invalid") from None

    labels = {
        "interview_invitation": ("面试邀请", "已为您安排面试"),
        "interview_rescheduled": ("面试时间变更", "您的面试安排已更新"),
        "interview_cancelled": ("面试取消", "您的面试已取消"),
    }
    subject_label, opening = labels[kind]
    subject = _single_line(f"{subject_label}：{job_title} - {interview.round_name}")
    starts_at = interview.starts_at
    ends_at = interview.ends_at
    if starts_at.tzinfo is None or starts_at.utcoffset() is None:
        starts_at = starts_at.replace(tzinfo=timezone.utc)
    if ends_at.tzinfo is None or ends_at.utcoffset() is None:
        ends_at = ends_at.replace(tzinfo=timezone.utc)
    local_start = starts_at.astimezone(local_timezone)
    local_end = ends_at.astimezone(local_timezone)
    location = interview.meeting_url or interview.location or "待确认"
    body = "\n".join(
        [
            f"{candidate_name}，您好：",
            "",
            opening + "。",
            f"职位：{job_title}",
            f"轮次：{interview.round_name}",
            f"时间：{local_start:%Y-%m-%d %H:%M} - {local_end:%H:%M} ({interview.timezone})",
            f"方式：{interview.method}",
            f"地点/链接：{location}",
            "",
            f"如有问题，请直接回复此邮件联系 {organizer.name}。",
        ]
    )
    attendees = (
        CalendarContact(name=candidate_name, email=candidate_email),
        *_calendar_contacts(interview.calendar_attendees or []),
    )
    calendar = build_calendar_invitation(
        interview_id=interview.id,
        starts_at=starts_at,
        duration_minutes=int((ends_at - starts_at).total_seconds() // 60),
        summary=f"{job_title} - {interview.round_name}",
        location=location,
        description=opening,
        sequence=interview.calendar_sequence,
        dtstamp=dtstamp,
        status="cancelled" if kind == "interview_cancelled" else interview.status,
        organizer=organizer,
        attendees=attendees,
        timezone_name=interview.timezone,
    )
    return InterviewMessage(
        kind=kind,
        subject=subject,
        body=body,
        attachment_filename="interview.ics",
        attachment_content_type="text/calendar",
        attachment_content=calendar,
    )


def enqueue_interview_message(
    db,
    *,
    interview: Interview,
    kind: str,
    trace_id: str | None,
    contact_cipher: ContactCipher,
    email_cipher: EmailSecretCipher,
    sender_policy: SenderPolicy,
) -> EmailDelivery:
    application = db.scalar(
        select(Application).where(
            Application.organization_id == interview.organization_id,
            Application.id == interview.application_id,
        )
    )
    if application is None:
        raise ValueError("interview_application_unavailable")
    candidate = db.scalar(
        select(Candidate).where(
            Candidate.organization_id == interview.organization_id,
            Candidate.id == application.candidate_id,
        )
    )
    job = db.scalar(
        select(Job).where(
            Job.organization_id == interview.organization_id,
            Job.id == application.job_id,
        )
    )
    if candidate is None or job is None:
        raise ValueError("interview_context_unavailable")
    contact = db.scalar(
        select(CandidateContact)
        .where(
            CandidateContact.organization_id == interview.organization_id,
            CandidateContact.candidate_id == candidate.id,
            CandidateContact.kind == "email",
            CandidateContact.confirmation_status == "confirmed",
        )
        .order_by(
            CandidateContact.confirmed_at.desc(),
            CandidateContact.created_at.desc(),
            CandidateContact.id.desc(),
        )
        .limit(1)
        .with_for_update()
    )
    if contact is None:
        raise CandidateEmailUnavailable
    try:
        candidate_email = contact_cipher.decrypt(contact.ciphertext)
    except ValueError:
        raise CandidateEmailUnavailable from None

    provider_config = db.scalar(
        select(EmailProviderConfig)
        .where(
            EmailProviderConfig.organization_id == interview.organization_id,
            EmailProviderConfig.enabled.is_(True),
        )
        .order_by(EmailProviderConfig.version.desc())
        .limit(1)
    )
    if provider_config is None:
        raise EmailConfigurationUnavailable
    responsible_hr = db.scalar(
        select(User).where(
            User.organization_id == interview.organization_id,
            User.id == interview.owner_id,
            User.status == UserStatus.ACTIVE,
        )
    )
    if responsible_hr is not None:
        try:
            responsible_email = email_cipher.normalize_email(responsible_hr.email)
        except ValueError:
            responsible_hr = None
            responsible_email = None
    else:
        responsible_email = None
    reply_to_email = responsible_email
    reply_to_name = responsible_hr.display_name if responsible_hr is not None else None
    if responsible_hr is None:
        organizer = CalendarContact(
            name=provider_config.default_reply_to_name,
            email=provider_config.default_reply_to_email,
        )
    else:
        organizer = CalendarContact(name=reply_to_name, email=reply_to_email)

    message = render_interview_message(
        kind=kind,
        interview=interview,
        candidate_name=candidate.display_name,
        candidate_email=candidate_email,
        job_title=job.title,
        organizer=organizer,
        dtstamp=datetime.now(timezone.utc),
    )
    return enqueue_delivery(
        db,
        DeliveryCommand(
            organization_id=interview.organization_id,
            recipient=candidate_email,
            reply_to_email=reply_to_email,
            reply_to_name=reply_to_name,
            subject=message.subject,
            body=message.body,
            resource_type=kind,
            resource_id=interview.id,
            idempotency_key=f"{interview.id}:{kind}:{interview.calendar_sequence}",
            operation="interview.transactional_email",
            # Interview ownership is the stable responsible-HR snapshot. The
            # initiating actor remains on the interview event/audit records.
            created_by=interview.owner_id,
            trace_id=trace_id,
            attachment_filename=message.attachment_filename,
            attachment_content_type=message.attachment_content_type,
            attachment_content=message.attachment_content,
        ),
        cipher=email_cipher,
        sender_policy=sender_policy,
    )
