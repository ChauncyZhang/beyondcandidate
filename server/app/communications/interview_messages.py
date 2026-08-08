from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from server.app.communications.models import EmailDelivery
from server.app.communications.security import EmailSecretCipher
from server.app.communications.service import (
    DeliveryCommand,
    EmailConfigurationUnavailable,
    SenderPolicy,
    enqueue_delivery,
    latest_email_provider_config,
)
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


class InterviewMessageValidationError(ValueError):
    safe_code = "interview_message_invalid"


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
    reply_to_name: str,
    dtstamp: datetime,
) -> InterviewMessage:
    if kind not in INTERVIEW_MESSAGE_KINDS:
        raise ValueError("interview_message_kind_invalid")
    try:
        local_timezone = ZoneInfo(interview.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise InterviewMessageValidationError("interview_timezone_invalid") from None

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
            f"如有问题，请直接回复此邮件联系 {reply_to_name}。",
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
        attachment_content_type=(
            "text/calendar; method=CANCEL; charset=UTF-8"
            if kind == "interview_cancelled"
            else "text/calendar; method=REQUEST; charset=UTF-8"
        ),
        attachment_content=calendar,
    )


def resolve_confirmed_candidate_email(
    db,
    *,
    organization_id,
    candidate_id,
    contact_cipher: ContactCipher,
) -> str:
    contact = db.scalar(
        select(CandidateContact)
        .where(
            CandidateContact.organization_id == organization_id,
            CandidateContact.candidate_id == candidate_id,
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
        return contact_cipher.decrypt(contact.ciphertext)
    except ValueError:
        raise CandidateEmailUnavailable from None


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
    candidate_email = resolve_confirmed_candidate_email(
        db,
        organization_id=interview.organization_id,
        candidate_id=candidate.id,
        contact_cipher=contact_cipher,
    )

    provider_config = latest_email_provider_config(db, interview.organization_id)
    if provider_config is None or not provider_config.enabled:
        raise EmailConfigurationUnavailable
    try:
        organizer = CalendarContact(
            name=interview.calendar_organizer["name"],
            email=interview.calendar_organizer["email"],
        )
    except (KeyError, TypeError, ValueError):
        raise InterviewMessageValidationError("interview_organizer_invalid") from None
    initial_delivery = db.scalar(
        select(EmailDelivery)
        .where(
            EmailDelivery.organization_id == interview.organization_id,
            EmailDelivery.resource_type == "interview_invitation",
            EmailDelivery.resource_id == interview.id,
        )
        .order_by(EmailDelivery.created_at, EmailDelivery.id)
        .limit(1)
    )
    if initial_delivery is not None:
        reply_to_email = initial_delivery.reply_to_email
        reply_to_name = initial_delivery.reply_to_name
    else:
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
            reply_to_email = provider_config.default_reply_to_email
            reply_to_name = provider_config.default_reply_to_name

    message = render_interview_message(
        kind=kind,
        interview=interview,
        candidate_name=candidate.display_name,
        candidate_email=candidate_email,
        job_title=job.title,
        organizer=organizer,
        reply_to_name=reply_to_name,
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
