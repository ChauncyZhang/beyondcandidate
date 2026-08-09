import html
import hmac
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from server.app.communications.models import EmailDelivery, EmailProviderConfig, EmailTemplate
from server.app.communications.security import EmailSecretCipher
from server.app.identity.models import AuditLog, User, UserRole, UserStatus
from server.app.notifications.service import create_user_notification
from server.app.queue.payloads import PayloadSchema, OpaqueIdField, UnsafePayload
from server.app.queue.repository import QueueRepository
from server.app.queue.service import normalize_safe_code


VARIABLE = re.compile(r"{{\s*([A-Za-z][A-Za-z0-9_]*)\s*}}")
EMAIL_JOB_PAYLOAD = PayloadSchema({"organization_id": OpaqueIdField(), "delivery_id": OpaqueIdField()})
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryCommand:
    organization_id: uuid.UUID
    recipient: str | None
    subject: str
    body: str
    resource_type: str
    resource_id: uuid.UUID
    idempotency_key: str
    operation: str
    reply_to_email: str | None = None
    reply_to_name: str | None = None
    created_by: uuid.UUID | None = None
    template_id: uuid.UUID | None = None
    template_version: int | None = None
    parent_delivery_id: uuid.UUID | None = None
    trace_id: str | None = None
    attachment_filename: str | None = None
    attachment_content_type: str | None = None
    attachment_content: bytes | None = None
    attachment_ciphertext: bytes | None = None
    recipient_ciphertext: bytes | None = None
    recipient_masked: str | None = None


@dataclass(frozen=True)
class SenderPolicy:
    email: str
    name: str


class DeliveryIdempotencyConflict(ValueError):
    pass


class EmailConfigurationUnavailable(ValueError):
    safe_code = "email_not_configured"


def _safe_header(value: str) -> str:
    if not value or "\r" in value or "\n" in value:
        raise ValueError("header_injection")
    return value


def latest_email_provider_config(db, organization_id: uuid.UUID) -> EmailProviderConfig | None:
    return db.scalar(
        select(EmailProviderConfig)
        .where(EmailProviderConfig.organization_id == organization_id)
        .order_by(EmailProviderConfig.version.desc())
        .limit(1)
    )


def validate_template(subject: str, body: str, allowed_variables: list[str]) -> None:
    _safe_header(subject)
    referenced = set(VARIABLE.findall(subject)) | set(VARIABLE.findall(body))
    if not referenced <= set(allowed_variables):
        raise ValueError("template_variables_invalid")
    residue = VARIABLE.sub("", subject + body)
    if "{{" in residue or "}}" in residue:
        raise ValueError("template_variables_invalid")


def render_template(template: EmailTemplate, variables: dict[str, str]) -> tuple[str, str]:
    allowed = set(template.variable_allowlist)
    if set(variables) != allowed:
        raise ValueError("template_variables_invalid")
    validate_template(template.subject_template, template.body_template, template.variable_allowlist)
    normalized = {key: str(value) for key, value in variables.items()}
    subject = VARIABLE.sub(lambda match: normalized[match.group(1)], template.subject_template)
    body = VARIABLE.sub(lambda match: html.escape(normalized[match.group(1)]), template.body_template)
    return _safe_header(subject), body


def enqueue_delivery(db, command: DeliveryCommand, *, cipher: EmailSecretCipher, sender_policy: SenderPolicy) -> EmailDelivery:
    actor_scope = str(command.created_by) if command.created_by is not None else "system"
    business_dedupe_key = cipher.fingerprint(
        "delivery-business-key",
        {"organization_id": str(command.organization_id), "actor": actor_scope, "operation": command.operation, "key": command.idempotency_key},
    )
    if db.bind.dialect.name == "postgresql":
        db.execute(text("select pg_advisory_xact_lock(hashtextextended(:scope, 0))"), {"scope": f"email-delivery:{command.organization_id}:{business_dedupe_key}"})
    if (command.reply_to_email is None) != (command.reply_to_name is None):
        raise ValueError("reply_to_pair_required")
    if command.attachment_content is not None and command.attachment_ciphertext is not None:
        raise ValueError("attachment_content_ambiguous")
    if command.recipient is None:
        if command.recipient_ciphertext is None or command.recipient_masked is None:
            raise ValueError("recipient_snapshot_required")
    elif command.recipient_ciphertext is not None or command.recipient_masked is not None:
        raise ValueError("recipient_snapshot_ambiguous")
    attachment_snapshot = command.attachment_ciphertext or command.attachment_content
    attachment_values = (
        command.attachment_filename,
        command.attachment_content_type,
        attachment_snapshot,
    )
    if any(value is not None for value in attachment_values) and not all(
        value is not None for value in attachment_values
    ):
        raise ValueError("attachment_triplet_required")

    def fingerprint(reply_to_email: str, reply_to_name: str, effective_sender: SenderPolicy) -> str:
        return cipher.fingerprint("delivery-request", {
            "recipient": command.recipient,
            "recipient_ciphertext": command.recipient_ciphertext.hex() if command.recipient_ciphertext else None,
            "recipient_masked": command.recipient_masked,
            "reply_to_email": reply_to_email,
            "reply_to_name": reply_to_name, "subject": command.subject, "body": command.body,
            "resource_type": command.resource_type, "resource_id": str(command.resource_id),
            "template_id": str(command.template_id) if command.template_id else None,
            "template_version": command.template_version, "parent_delivery_id": str(command.parent_delivery_id) if command.parent_delivery_id else None,
            "sender_email": effective_sender.email, "sender_name": effective_sender.name,
            "attachment_filename": command.attachment_filename,
            "attachment_content_type": command.attachment_content_type,
            "attachment_snapshot": attachment_snapshot.hex() if attachment_snapshot is not None else None,
        })

    existing = db.scalar(select(EmailDelivery).where(EmailDelivery.organization_id == command.organization_id, EmailDelivery.business_dedupe_key == business_dedupe_key))
    if existing is not None:
        request_fingerprint = fingerprint(
            command.reply_to_email or existing.reply_to_email,
            command.reply_to_name or existing.reply_to_name,
            SenderPolicy(existing.sender_email, existing.sender_name),
        )
        if not hmac.compare_digest(existing.request_fingerprint, request_fingerprint):
            raise DeliveryIdempotencyConflict("idempotency_conflict")
        return existing
    config = latest_email_provider_config(db, command.organization_id)
    if config is None or not config.enabled:
        raise EmailConfigurationUnavailable("email_not_configured")
    effective_sender = SenderPolicy(
        config.sender_address or sender_policy.email,
        config.sender_name or sender_policy.name,
    )
    reply_to_email = command.reply_to_email or config.default_reply_to_email
    reply_to_name = command.reply_to_name or config.default_reply_to_name
    request_fingerprint = fingerprint(reply_to_email, reply_to_name, effective_sender)
    recipient = cipher.normalize_email(command.recipient) if command.recipient is not None else None
    sender = cipher.normalize_email(_safe_header(effective_sender.email))
    reply_to = cipher.normalize_email(_safe_header(reply_to_email))
    delivery = EmailDelivery(
        organization_id=command.organization_id, provider_config_id=config.id, provider_config_version=config.version,
        template_id=command.template_id, template_version=command.template_version,
        recipient_ciphertext=(
            cipher.encrypt_recipient(recipient)
            if recipient is not None
            else command.recipient_ciphertext
        ),
        recipient_masked=(
            cipher.mask_email(recipient)
            if recipient is not None
            else command.recipient_masked
        ),
        sender_email=sender, sender_name=_safe_header(effective_sender.name), reply_to_email=reply_to,
        reply_to_name=_safe_header(reply_to_name), rendered_subject=_safe_header(command.subject),
        rendered_body=command.body, resource_type=command.resource_type, resource_id=command.resource_id,
        attachment_filename=_safe_header(command.attachment_filename) if command.attachment_filename else None,
        attachment_content_type=_safe_header(command.attachment_content_type) if command.attachment_content_type else None,
        attachment_ciphertext=(
            command.attachment_ciphertext
            if command.attachment_ciphertext is not None
            else cipher.encrypt_attachment(command.attachment_content)
            if command.attachment_content is not None
            else None
        ),
        business_dedupe_key=business_dedupe_key, request_fingerprint=request_fingerprint,
        parent_delivery_id=command.parent_delivery_id, created_by=command.created_by, status="queued", version=1,
    )
    try:
        with db.begin_nested():
            db.add(delivery); db.flush()
    except IntegrityError:
        existing = db.scalar(select(EmailDelivery).where(EmailDelivery.organization_id == command.organization_id, EmailDelivery.business_dedupe_key == business_dedupe_key))
        if existing is None or not hmac.compare_digest(existing.request_fingerprint, request_fingerprint):
            raise DeliveryIdempotencyConflict("idempotency_conflict") from None
        return existing
    QueueRepository(db).enqueue(
        command.organization_id, "communications.send_email",
        {"organization_id": str(command.organization_id), "delivery_id": str(delivery.id)},
        max_attempts=3, dedupe_key=f"email-delivery:{delivery.id}", trace_id=command.trace_id,
    )
    return delivery


def _responsible_notification_user_id(db, delivery: EmailDelivery) -> uuid.UUID | None:
    if delivery.created_by is not None:
        creator = db.scalar(select(User).where(
            User.organization_id == delivery.organization_id,
            User.id == delivery.created_by,
            User.status == UserStatus.ACTIVE,
        ))
        if creator is not None:
            return creator.id
    fallback = db.scalar(
        select(User.id)
        .join(UserRole, UserRole.user_id == User.id)
        .where(
            User.organization_id == delivery.organization_id,
            User.status == UserStatus.ACTIVE,
            UserRole.role == "recruiting_admin",
        )
        .order_by(User.created_at.asc(), User.id.asc())
        .limit(1)
    )
    return fallback


def mark_delivery_failed(db, delivery: EmailDelivery, safe_code: str, now: datetime | None = None) -> str:
    if delivery.status in {"sent", "failed"}:
        return delivery.safe_error_code or normalize_safe_code(safe_code)
    responsible_user_id = _responsible_notification_user_id(db, delivery)
    effective_safe_code = normalize_safe_code(safe_code) if responsible_user_id is not None else "email_notification_recipient_unavailable"
    delivery.status = "failed"
    delivery.safe_error_code = effective_safe_code
    delivery.failed_at = now or datetime.now(timezone.utc)
    if delivery.resource_type == "offer_access_token":
        from server.app.offers.service import revoke_offer_delivery_token
        revoke_offer_delivery_token(db, delivery, now=delivery.failed_at)
    delivery.version += 1
    if responsible_user_id is not None:
        create_user_notification(
            db, organization_id=delivery.organization_id, user_id=responsible_user_id,
            event_type="email_delivery_failed", resource_type="email_delivery", resource_id=delivery.id,
            recipient_masked=delivery.recipient_masked, safe_error_code=delivery.safe_error_code,
        )
    db.add(AuditLog(
        organization_id=delivery.organization_id, actor_user_id=delivery.created_by,
        category="recruiting", event_type="email.delivery_failed", outcome="failure",
        resource_type="email_delivery", resource_id=delivery.id,
        trace_id=f"email-{str(delivery.id)[:8]}",
        metadata_json={"delivery_id": str(delivery.id), "recipient": delivery.recipient_masked, "safe_error_code": delivery.safe_error_code},
    ))
    if responsible_user_id is None:
        db.add(AuditLog(
            organization_id=delivery.organization_id, actor_user_id=None, category="system",
            event_type="email.notification_recipient_unavailable", outcome="failure",
            resource_type="email_delivery", resource_id=delivery.id,
            trace_id=f"email-{str(delivery.id)[:8]}",
            metadata_json={"delivery_id": str(delivery.id), "safe_error_code": effective_safe_code},
        ))
    db.flush()
    if responsible_user_id is not None:
        try:
            with db.begin_nested():
                from server.app.integrations.feishu.notifications import schedule_feishu_notification
                schedule_feishu_notification(
                    db, organization_id=delivery.organization_id, event_type="email_delivery_failed",
                    recipient_user_ids=[responsible_user_id], email_delivery_id=delivery.id,
                )
        except Exception:
            logger.error(
                "email_failure_adapter_scheduling_failed",
                extra={"context": {"delivery_id": str(delivery.id), "adapter": "feishu"}},
            )
    return effective_safe_code


def email_delivery_terminal_callback(db, job, safe_code, now) -> None:
    try:
        payload = EMAIL_JOB_PAYLOAD.validate(job.payload)
        organization_id = uuid.UUID(payload["organization_id"]); delivery_id = uuid.UUID(payload["delivery_id"])
        if uuid.UUID(str(job.organization_id)) != organization_id:
            return
    except (AttributeError, TypeError, ValueError, UnsafePayload):
        return
    delivery = db.scalar(select(EmailDelivery).where(EmailDelivery.organization_id == organization_id, EmailDelivery.id == delivery_id).with_for_update())
    if delivery is not None:
        terminal_code = mark_delivery_failed(db, delivery, safe_code, now)
        if terminal_code == "email_notification_recipient_unavailable":
            job.last_error_code = terminal_code


def communications_terminal_callbacks():
    return {"communications.send_email": email_delivery_terminal_callback}
