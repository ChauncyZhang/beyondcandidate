import html
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from server.app.communications.models import EmailDelivery, EmailProviderConfig, EmailTemplate
from server.app.communications.security import EmailSecretCipher
from server.app.identity.models import AuditLog
from server.app.queue.payloads import PayloadSchema, OpaqueIdField, UnsafePayload
from server.app.queue.repository import QueueRepository
from server.app.queue.service import normalize_safe_code


VARIABLE = re.compile(r"{{\s*([A-Za-z][A-Za-z0-9_]*)\s*}}")
EMAIL_JOB_PAYLOAD = PayloadSchema({"organization_id": OpaqueIdField(), "delivery_id": OpaqueIdField()})


@dataclass(frozen=True)
class DeliveryCommand:
    organization_id: uuid.UUID
    recipient: str
    sender_email: str
    sender_name: str
    reply_to_email: str
    reply_to_name: str
    subject: str
    body: str
    resource_type: str
    resource_id: uuid.UUID
    idempotency_key: str
    created_by: uuid.UUID | None = None
    template_id: uuid.UUID | None = None
    template_version: int | None = None
    parent_delivery_id: uuid.UUID | None = None
    trace_id: str | None = None


def _safe_header(value: str) -> str:
    if not value or "\r" in value or "\n" in value:
        raise ValueError("header_injection")
    return value


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


def enqueue_delivery(db, command: DeliveryCommand, *, cipher: EmailSecretCipher) -> EmailDelivery:
    existing = db.scalar(select(EmailDelivery).where(EmailDelivery.organization_id == command.organization_id, EmailDelivery.business_dedupe_key == command.idempotency_key))
    if existing is not None:
        return existing
    config = db.scalar(select(EmailProviderConfig).where(EmailProviderConfig.organization_id == command.organization_id))
    if config is None or not config.enabled:
        raise ValueError("email_not_configured")
    recipient = cipher.normalize_email(command.recipient)
    sender = cipher.normalize_email(_safe_header(command.sender_email))
    reply_to = cipher.normalize_email(_safe_header(command.reply_to_email))
    delivery = EmailDelivery(
        organization_id=command.organization_id, provider_config_id=config.id, provider_config_version=config.version,
        template_id=command.template_id, template_version=command.template_version,
        recipient_ciphertext=cipher.encrypt(recipient), recipient_masked=cipher.mask_email(recipient),
        sender_email=sender, sender_name=_safe_header(command.sender_name), reply_to_email=reply_to,
        reply_to_name=_safe_header(command.reply_to_name), rendered_subject=_safe_header(command.subject),
        rendered_body=command.body, resource_type=command.resource_type, resource_id=command.resource_id,
        business_dedupe_key=command.idempotency_key, parent_delivery_id=command.parent_delivery_id,
        created_by=command.created_by, status="queued",
    )
    db.add(delivery); db.flush()
    QueueRepository(db).enqueue(
        command.organization_id, "communications.send_email",
        {"organization_id": str(command.organization_id), "delivery_id": str(delivery.id)},
        max_attempts=3, dedupe_key=f"email-delivery:{delivery.id}", trace_id=command.trace_id,
    )
    return delivery


def mark_delivery_failed(db, delivery: EmailDelivery, safe_code: str, now: datetime | None = None) -> None:
    if delivery.status in {"sent", "failed"}:
        return
    delivery.status = "failed"
    delivery.safe_error_code = normalize_safe_code(safe_code)
    delivery.failed_at = now or datetime.now(timezone.utc)
    db.add(AuditLog(
        organization_id=delivery.organization_id, actor_user_id=delivery.created_by,
        category="recruiting", event_type="email.delivery_failed", outcome="failure",
        resource_type="email_delivery", resource_id=delivery.id,
        trace_id=f"email-{str(delivery.id)[:8]}",
        metadata_json={"delivery_id": str(delivery.id), "recipient": delivery.recipient_masked, "safe_error_code": delivery.safe_error_code},
    ))


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
        mark_delivery_failed(db, delivery, safe_code, now)


def communications_terminal_callbacks():
    return {"communications.send_email": email_delivery_terminal_callback}
