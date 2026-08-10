import logging
import uuid
from datetime import datetime, timezone
from html import escape

from sqlalchemy import select

from server.app.communications.models import EmailDelivery, EmailProviderConfig
from server.app.communications.provider import MailMessage, PermanentMailError, SmtpMailProvider, TemporaryMailError
from server.app.communications.security import EmailSecretCipher
from server.app.communications.service import EMAIL_JOB_PAYLOAD, communications_terminal_callbacks, mark_delivery_failed
from server.app.identity.models import Organization
from server.app.queue.payloads import UnsafePayload
from server.app.queue.service import PermanentJobError, RetryableJobError


logger = logging.getLogger(__name__)


def _render_offer_link(body: str, token_id: uuid.UUID, codec, public_base_url: str) -> str:
    """Materialize a capability only in the transient provider message."""
    return body.replace("{{offer_public_link}}", f"{public_base_url}/offer/{codec.raw_token(token_id)}")


def _offer_html_body(*, brand_name: str, body: str, offer_link: str) -> str:
    safe_brand = escape(brand_name, quote=True)
    safe_initial = escape((brand_name.strip()[:1] or "O").upper(), quote=True)
    safe_link = escape(offer_link, quote=True)
    content = body.replace(offer_link, "").replace("请点击以下链接查看并确认 Offer：", "").strip()
    paragraphs = "".join(
        f'<p style="margin:0 0 16px;color:#475467;font-size:16px;line-height:1.75;">{escape(paragraph, quote=True).replace(chr(10), "<br>")}</p>'
        for paragraph in content.split("\n\n") if paragraph.strip()
    )
    return f'''<!doctype html>
<html lang="zh-CN"><body style="margin:0;padding:0;background:#f5f7fa;font-family:Arial,'Microsoft YaHei',sans-serif;color:#172033;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f5f7fa;padding:24px 12px;"><tr><td align="center">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;background:#ffffff;border:1px solid #e3e7ee;">
<tr><td style="padding:22px 32px;border-bottom:1px solid #e8ebf0;"><table role="presentation" cellspacing="0" cellpadding="0"><tr><td style="width:34px;height:34px;border-radius:7px;background:#0068d8;color:#ffffff;text-align:center;font-size:18px;">{safe_initial}</td><td style="padding-left:12px;font-size:18px;font-weight:600;color:#101828;">{safe_brand}</td></tr></table></td></tr>
<tr><td style="padding:38px 32px 34px;"><p style="margin:0 0 10px;color:#0068d8;font-size:13px;font-weight:600;">OFFER OF EMPLOYMENT</p><h1 style="margin:0 0 24px;color:#101828;font-size:28px;line-height:1.35;font-weight:600;">正式录用通知</h1>{paragraphs}
<table role="presentation" cellspacing="0" cellpadding="0" style="margin:28px 0 22px;"><tr><td style="border-radius:7px;background:#0068d8;"><a href="{safe_link}" style="display:inline-block;padding:13px 24px;color:#ffffff;text-decoration:none;font-size:16px;font-weight:600;">查看并确认 Offer</a></td></tr></table>
<p style="margin:0;color:#667085;font-size:13px;line-height:1.7;">该链接包含您的录用信息，请勿转发。如按钮无法打开，请联系招聘负责人。</p></td></tr>
<tr><td style="padding:18px 32px;border-top:1px solid #e8ebf0;background:#fafbfc;color:#7a8495;font-size:12px;text-align:center;">{safe_brand} · 安全录用确认邮件</td></tr>
</table></td></tr></table></body></html>'''


class EmailDeliveryJobHandler:
    def __init__(self, sessions, provider, cipher: EmailSecretCipher, *, offer_token_codec=None, offer_public_base_url: str | None = None, timeout_seconds: float = 10) -> None:
        self._sessions, self._provider, self._cipher = sessions, provider, cipher
        self._offer_token_codec, self._offer_public_base_url = offer_token_codec, offer_public_base_url
        self._timeout = timeout_seconds

    async def __call__(self, job) -> None:
        try:
            payload = EMAIL_JOB_PAYLOAD.validate(job.payload)
            organization_id = uuid.UUID(payload["organization_id"]); delivery_id = uuid.UUID(payload["delivery_id"])
            if uuid.UUID(str(job.organization_id)) != organization_id:
                raise UnsafePayload("job tenant does not match payload")
        except (AttributeError, TypeError, ValueError, UnsafePayload):
            raise PermanentJobError("email_delivery_payload_invalid") from None

        setup_error = None
        with self._sessions.begin() as db:
            delivery = db.scalar(select(EmailDelivery).where(EmailDelivery.organization_id == organization_id, EmailDelivery.id == delivery_id).with_for_update())
            if delivery is None:
                raise PermanentJobError("email_delivery_unavailable")
            if delivery.status == "sent":
                return
            if delivery.status == "failed":
                raise PermanentJobError(delivery.safe_error_code or "email_delivery_failed")
            latest_config = db.scalar(select(EmailProviderConfig).where(EmailProviderConfig.organization_id == organization_id).order_by(EmailProviderConfig.version.desc()).limit(1))
            config = db.scalar(select(EmailProviderConfig).where(
                EmailProviderConfig.organization_id == organization_id,
                EmailProviderConfig.id == delivery.provider_config_id,
                EmailProviderConfig.version == delivery.provider_config_version,
            ))
            if latest_config is None or not latest_config.enabled or config is None or not config.enabled:
                mark_delivery_failed(db, delivery, "email_configuration_unavailable")
                setup_error = "email_configuration_unavailable"
            else:
                try:
                    recipient = self._cipher.decrypt_recipient(delivery.recipient_ciphertext)
                    password = self._cipher.decrypt_smtp_password(config.encrypted_password)
                    attachment_content = (
                        self._cipher.decrypt_attachment(delivery.attachment_ciphertext)
                        if delivery.attachment_ciphertext is not None
                        else None
                    )
                except ValueError:
                    mark_delivery_failed(db, delivery, "email_secret_unavailable")
                    setup_error = "email_secret_unavailable"
                else:
                    delivery.attempts += 1
                    delivery.version += 1
                    body = delivery.rendered_body
                    html_body = None
                    if delivery.resource_type == "offer_access_token":
                        from server.app.offers.models import OfferAccessToken
                        token = db.scalar(select(OfferAccessToken).where(OfferAccessToken.organization_id == organization_id, OfferAccessToken.id == delivery.resource_id))
                        if token is None or self._offer_token_codec is None or not self._offer_public_base_url:
                            mark_delivery_failed(db, delivery, "offer_link_unavailable"); setup_error = "offer_link_unavailable"
                        else:
                            organization = db.scalar(select(Organization).where(Organization.id == organization_id))
                            offer_link = f"{self._offer_public_base_url}/offer/{self._offer_token_codec.raw_token(token.id)}"
                            body = _render_offer_link(body, token.id, self._offer_token_codec, self._offer_public_base_url)
                            html_body = _offer_html_body(brand_name=organization.name if organization else delivery.sender_name, body=body, offer_link=offer_link)
                    message = MailMessage(
                        recipient=recipient,
                        sender_email=delivery.sender_email,
                        sender_name=delivery.sender_name,
                        reply_to_email=delivery.reply_to_email,
                        reply_to_name=delivery.reply_to_name,
                        subject=delivery.rendered_subject,
                        body=body,
                        message_id=f"<email-{delivery.id}@beyondcandidate.internal>",
                        attachment_filename=delivery.attachment_filename,
                        attachment_content_type=delivery.attachment_content_type,
                        attachment_content=attachment_content,
                        html_body=html_body,
                    )
                    provider = self._provider or SmtpMailProvider(host=config.host, port=config.port, tls_mode=config.tls_mode, username=config.username, password=password, timeout_seconds=self._timeout)

        if setup_error is not None:
            raise PermanentJobError(setup_error)

        # SMTP is at-least-once: a disconnect after DATA can leave acceptance
        # ambiguous. Retries reuse the deterministic Message-ID for reconciliation.
        try:
            receipt = await provider.send(message)
        except TemporaryMailError as error:
            with self._sessions.begin() as db:
                delivery = db.scalar(select(EmailDelivery).where(EmailDelivery.organization_id == organization_id, EmailDelivery.id == delivery_id).with_for_update())
                if delivery is not None and delivery.status == "queued":
                    delivery.safe_error_code = error.safe_code
                    delivery.version += 1
            logger.error("email_delivery_attempt_failed", extra={"context": {"delivery_id": str(delivery_id), "safe_error_code": error.safe_code}})
            raise RetryableJobError(error.safe_code) from None
        except PermanentMailError as error:
            with self._sessions.begin() as db:
                delivery = db.scalar(select(EmailDelivery).where(EmailDelivery.organization_id == organization_id, EmailDelivery.id == delivery_id).with_for_update())
                if delivery is not None:
                    mark_delivery_failed(db, delivery, error.safe_code)
            logger.error("email_delivery_terminal_failure", extra={"context": {"delivery_id": str(delivery_id), "safe_error_code": error.safe_code}})
            raise PermanentJobError(error.safe_code) from None

        with self._sessions.begin() as db:
            delivery = db.scalar(select(EmailDelivery).where(EmailDelivery.organization_id == organization_id, EmailDelivery.id == delivery_id).with_for_update())
            if delivery is None or delivery.status != "queued":
                raise PermanentJobError("email_delivery_state_conflict")
            if delivery.resource_type == "offer_access_token":
                from server.app.offers.service import mark_offer_delivery_sent
                mark_offer_delivery_sent(db, delivery, now=datetime.now(timezone.utc))
            delivery.status = "sent"; delivery.safe_error_code = None
            delivery.provider_receipt_id = receipt.receipt_id[:255]; delivery.sent_at = datetime.now(timezone.utc); delivery.version += 1


__all__ = ["EmailDeliveryJobHandler", "communications_terminal_callbacks"]
