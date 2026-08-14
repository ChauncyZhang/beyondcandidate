from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select

from server.app.communications.models import EmailDelivery, EmailProviderConfig
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
AUTO_USABLE_CANDIDATE_EMAIL_SOURCES = frozenset({"native", "ocr"})


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


def resolve_interview_reply_identity(
    db,
    *,
    organization_id,
    job_owner_id,
    email_cipher: EmailSecretCipher,
    provider_config: EmailProviderConfig | None,
) -> tuple[str | None, str]:
    visible_name = (
        (provider_config.default_reply_to_name or "").strip()
        if provider_config is not None
        else ""
    ) or "HR"
    responsible_hr = db.scalar(
        select(User).where(
            User.organization_id == organization_id,
            User.id == job_owner_id,
            User.status == UserStatus.ACTIVE,
        )
    )
    if responsible_hr is not None:
        try:
            return email_cipher.normalize_email(responsible_hr.email), visible_name
        except ValueError:
            pass
    return (
        provider_config.default_reply_to_email if provider_config is not None else None,
        visible_name,
    )


def replace_interview_reply_contact(body: str, reply_to_name: str) -> str:
    replacement = f"如有问题，请直接回复此邮件联系 {reply_to_name}。"
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("如有问题，"):
            lines[index] = replacement
            return "\n".join(lines)
    return f"{body.rstrip()}\n\n{replacement}"


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


def _interview_location_text(interview: Interview) -> str:
    if interview.method == "video":
        return interview.meeting_url or "飞书视频会议将通过日历邀请发送"
    if interview.method == "onsite":
        if not interview.location:
            raise InterviewMessageValidationError("interview_location_invalid")
        return interview.location
    if interview.method == "phone":
        return "招聘负责人将通过电话联系"
    raise InterviewMessageValidationError("interview_method_invalid")


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
    location = _interview_location_text(interview)
    method_label = {
        "video": "视频面试",
        "onsite": "现场面试",
        "phone": "电话面试",
    }.get(interview.method, interview.method)
    body = "\n".join(
        [
            f"{candidate_name}，您好：",
            "",
            opening + "。",
            f"职位：{job_title}",
            f"轮次：{interview.round_name}",
            f"时间：{local_start:%Y-%m-%d %H:%M} - {local_end:%H:%M} ({interview.timezone})",
            f"方式：{method_label}",
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
    candidate = db.scalar(
        select(Candidate)
        .where(
            Candidate.organization_id == organization_id,
            Candidate.id == candidate_id,
        )
        .with_for_update()
    )
    if candidate is None or candidate.deleted_at is not None:
        raise CandidateEmailUnavailable

    contacts = list(db.scalars(
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
        .with_for_update()
    ).all())
    if not contacts:
        raise CandidateEmailUnavailable
    contact = contacts[0]
    if contact.confirmation_status != "confirmed" and len(contacts) > 1:
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
    provider_config = latest_email_provider_config(db, interview.organization_id)
    if provider_config is None or not provider_config.enabled:
        raise EmailConfigurationUnavailable
    candidate_email = resolve_confirmed_candidate_email(
        db,
        organization_id=interview.organization_id,
        candidate_id=candidate.id,
        contact_cipher=contact_cipher,
    )

    try:
        organizer = CalendarContact(
            name=interview.calendar_organizer["name"],
            email=interview.calendar_organizer["email"],
        )
    except (KeyError, TypeError, ValueError):
        raise InterviewMessageValidationError("interview_organizer_invalid") from None
    reply_to_email, reply_to_name = resolve_interview_reply_identity(
        db,
        organization_id=interview.organization_id,
        job_owner_id=job.owner_id,
        email_cipher=email_cipher,
        provider_config=provider_config,
    )

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
            # Delivery attribution follows the interview ownership snapshot. The
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
