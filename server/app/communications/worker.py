import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from server.app.communications.models import EmailDelivery, EmailProviderConfig
from server.app.communications.provider import MailMessage, PermanentMailError, SmtpMailProvider, TemporaryMailError
from server.app.communications.security import EmailSecretCipher
from server.app.communications.service import EMAIL_JOB_PAYLOAD, communications_terminal_callbacks, mark_delivery_failed
from server.app.queue.payloads import UnsafePayload
from server.app.queue.service import PermanentJobError, RetryableJobError


logger = logging.getLogger(__name__)


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
                    if delivery.resource_type == "offer_access_token":
                        from server.app.offers.models import OfferAccessToken
                        token = db.scalar(select(OfferAccessToken).where(OfferAccessToken.organization_id == organization_id, OfferAccessToken.id == delivery.resource_id))
                        if token is None or self._offer_token_codec is None or not self._offer_public_base_url:
                            mark_delivery_failed(db, delivery, "offer_link_unavailable"); setup_error = "offer_link_unavailable"
                        else:
                            raw = self._offer_token_codec.raw_token(token.id)
                            body = body.replace("{{offer_public_link}}", f"{self._offer_public_base_url.rstrip('/')}/api/public/v1/offers/{raw}")
                    message = MailMessage(
                        recipient,
                        delivery.sender_email,
                        delivery.sender_name,
                        delivery.reply_to_email,
                        delivery.reply_to_name,
                        delivery.rendered_subject,
                        body,
                        f"<email-{delivery.id}@beyondcandidate.internal>",
                        delivery.attachment_filename,
                        delivery.attachment_content_type,
                        attachment_content,
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
