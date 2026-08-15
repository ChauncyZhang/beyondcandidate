import logging
import uuid
from datetime import datetime, timezone
from html import escape
from urllib.parse import urlsplit

from sqlalchemy import select

from server.app.communications.interview_messages import PENDING_FEISHU_MEETING_TEXT
from server.app.communications.models import EmailDelivery, EmailProviderConfig
from server.app.communications.provider import MailMessage, PermanentMailError, SmtpMailProvider, TemporaryMailError
from server.app.communications.security import EmailSecretCipher
from server.app.communications.service import EMAIL_JOB_PAYLOAD, communications_terminal_callbacks, mark_delivery_failed
from server.app.identity.models import Organization
from server.app.integrations.feishu.models import FeishuInterviewSync
from server.app.interviews.domain import replace_calendar_location
from server.app.interviews.models import Interview
from server.app.queue.payloads import UnsafePayload
from server.app.queue.service import PermanentJobError, RetryableJobError


logger = logging.getLogger(__name__)


INTERVIEW_EMAIL_STYLES = {
    "interview_invitation": ("面试邀请", "面试已安排", "#0068d8", "#eaf3ff"),
    "interview_rescheduled": ("面试安排更新", "面试时间已变更", "#b54708", "#fff4e5"),
    "interview_cancelled": ("面试取消通知", "本次面试已取消", "#b42318", "#fff0ee"),
}
INTERVIEW_MEETING_LINK_EMAIL_TYPES = {
    "interview_invitation",
    "interview_rescheduled",
}
MAX_FEISHU_LINK_WAIT_ATTEMPTS = 2


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


def _safe_web_link(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _materialize_interview_meeting_link(
    body: str,
    attachment_content: bytes | None,
    meeting_url: str,
) -> tuple[str, bytes | None]:
    body = body.replace(PENDING_FEISHU_MEETING_TEXT, meeting_url)
    if attachment_content is not None:
        attachment_content = replace_calendar_location(
            attachment_content,
            meeting_url,
        )
    return body, attachment_content


def _calendar_sequence(attachment_content: bytes | None) -> int | None:
    if attachment_content is None:
        return None
    try:
        calendar = attachment_content.decode("utf-8").replace("\r\n ", "")
    except UnicodeDecodeError:
        return None
    for line in calendar.split("\r\n"):
        if line.startswith("SEQUENCE:"):
            value = line.removeprefix("SEQUENCE:")
            return int(value) if value.isdigit() else None
    return None


def _prepare_interview_delivery(
    db,
    delivery: EmailDelivery,
    job,
    body: str,
    attachment_content: bytes | None,
) -> tuple[str, bytes | None, bool]:
    if delivery.resource_type not in INTERVIEW_MEETING_LINK_EMAIL_TYPES:
        return body, attachment_content, False
    interview = db.scalar(
        select(Interview).where(
            Interview.organization_id == delivery.organization_id,
            Interview.id == delivery.resource_id,
        )
    )
    if interview is None:
        return body, attachment_content, False
    delivery_sequence = _calendar_sequence(attachment_content)
    if (
        delivery_sequence is not None
        and delivery_sequence != interview.calendar_sequence
    ):
        return body, attachment_content, True
    if interview.method != "video":
        return body, attachment_content, False
    meeting_url = _safe_web_link(interview.meeting_url or "")
    if meeting_url is not None:
        body, attachment_content = _materialize_interview_meeting_link(
            body,
            attachment_content,
            meeting_url,
        )
        return body, attachment_content, False
    sync = db.scalar(
        select(FeishuInterviewSync).where(
            FeishuInterviewSync.organization_id == delivery.organization_id,
            FeishuInterviewSync.interview_id == interview.id,
        )
    )
    if (
        sync is not None
        and sync.sync_status in {"pending", "syncing"}
        and int(getattr(job, "attempts", 0)) <= MAX_FEISHU_LINK_WAIT_ATTEMPTS
    ):
        raise RetryableJobError("feishu_meeting_link_pending")
    return body, attachment_content, False


def _interview_html_body(*, brand_name: str, subject: str, body: str, kind: str) -> str:
    title, status_text, accent, accent_background = INTERVIEW_EMAIL_STYLES[kind]
    safe_brand = escape(brand_name, quote=True)
    safe_initial = escape((brand_name.strip()[:1] or "B").upper(), quote=True)
    safe_subject = escape(subject, quote=True)
    lines = [line.strip() for line in body.splitlines()]
    nonempty = [line for line in lines if line]
    greeting = nonempty[0] if nonempty else "您好："
    detail_labels = ("职位", "轮次", "时间", "方式", "地点/链接")
    details: list[tuple[str, str]] = []
    detail_lines: set[str] = set()
    for line in nonempty:
        for label in detail_labels:
            prefix = f"{label}："
            if line.startswith(prefix):
                details.append((label, line.removeprefix(prefix).strip()))
                detail_lines.add(line)
                break
    narrative = [
        line for line in nonempty[1:]
        if line not in detail_lines and not line.startswith("如有问题，")
    ]
    opening = narrative[0] if narrative else status_text
    closing = next(
        (line for line in reversed(nonempty) if line.startswith("如有问题，")),
        "如有问题，请直接回复此邮件联系招聘负责人。",
    )
    detail_rows = []
    meeting_link = None
    for label, value in details:
        safe_value = escape(value, quote=True)
        if label == "地点/链接":
            meeting_link = None if kind == "interview_cancelled" else _safe_web_link(value)
            if meeting_link is not None:
                safe_link = escape(meeting_link, quote=True)
                safe_value = f'<a href="{safe_link}" style="color:{accent};text-decoration:none;word-break:break-all;">{safe_value}</a>'
        detail_rows.append(
            f'<tr><td style="width:92px;padding:10px 0;color:#667085;font-size:14px;vertical-align:top;">{escape(label)}</td>'
            f'<td style="padding:10px 0;color:#101828;font-size:15px;font-weight:600;line-height:1.6;vertical-align:top;">{safe_value}</td></tr>'
        )
    action = ""
    if meeting_link is not None and kind != "interview_cancelled":
        safe_link = escape(meeting_link, quote=True)
        action = (
            f'<table role="presentation" cellspacing="0" cellpadding="0" style="margin:26px 0 8px;"><tr>'
            f'<td style="border-radius:7px;background:{accent};"><a href="{safe_link}" style="display:inline-block;padding:13px 24px;color:#ffffff;text-decoration:none;font-size:15px;font-weight:600;">进入面试</a></td>'
            f'</tr></table>'
        )
    return f'''<!doctype html>
<html lang="zh-CN"><body style="margin:0;padding:0;background:#f5f7fa;font-family:Arial,'Microsoft YaHei',sans-serif;color:#172033;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f5f7fa;padding:24px 12px;"><tr><td align="center">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;background:#ffffff;border:1px solid #e3e7ee;border-radius:8px;overflow:hidden;">
<tr><td style="padding:20px 30px;border-bottom:1px solid #e8ebf0;"><table role="presentation" cellspacing="0" cellpadding="0"><tr><td style="width:36px;height:36px;border-radius:7px;background:#0068d8;color:#ffffff;text-align:center;font-size:18px;font-weight:700;">{safe_initial}</td><td style="padding-left:12px;font-size:17px;font-weight:600;color:#101828;">{safe_brand}<div style="margin-top:2px;color:#7a8495;font-size:12px;font-weight:400;">招聘团队</div></td></tr></table></td></tr>
<tr><td style="padding:34px 30px 30px;"><span style="display:inline-block;margin:0 0 16px;padding:6px 10px;border-radius:5px;background:{accent_background};color:{accent};font-size:12px;font-weight:700;">{escape(title)}</span>
<h1 style="margin:0 0 10px;color:#101828;font-size:26px;line-height:1.4;font-weight:650;">{safe_subject}</h1>
<p style="margin:0 0 8px;color:#344054;font-size:16px;line-height:1.75;">{escape(greeting, quote=True)}</p>
<p style="margin:0 0 24px;color:#475467;font-size:15px;line-height:1.75;">{escape(opening, quote=True)}</p>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="padding:8px 20px;border:1px solid #e5e9f0;border-radius:7px;background:#fafbfc;">{''.join(detail_rows)}</table>
{action}
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:24px;"><tr><td style="padding:14px 16px;border-left:3px solid {accent};background:{accent_background};color:#475467;font-size:13px;line-height:1.7;">邮件已附带日历文件，您可以将面试安排添加到常用日历。</td></tr></table>
<p style="margin:24px 0 0;color:#667085;font-size:13px;line-height:1.75;">{escape(closing, quote=True)}</p></td></tr>
<tr><td style="padding:17px 30px;border-top:1px solid #e8ebf0;background:#fafbfc;color:#7a8495;font-size:12px;text-align:center;">{safe_brand} · 候选人面试通知</td></tr>
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
            if delivery.status == "cancelled":
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
                    body = delivery.rendered_body
                    if delivery.resource_type in INTERVIEW_EMAIL_STYLES:
                        body, attachment_content, superseded = _prepare_interview_delivery(
                            db,
                            delivery,
                            job,
                            body,
                            attachment_content,
                        )
                        if superseded:
                            delivery.status = "cancelled"
                            delivery.safe_error_code = "interview_message_superseded"
                            delivery.version += 1
                            return
                    delivery.attempts += 1
                    delivery.version += 1
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
                    elif delivery.resource_type in INTERVIEW_EMAIL_STYLES:
                        html_body = _interview_html_body(
                            brand_name=delivery.sender_name,
                            subject=delivery.rendered_subject,
                            body=body,
                            kind=delivery.resource_type,
                        )
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
            terminal = False
            with self._sessions.begin() as db:
                delivery = db.scalar(select(EmailDelivery).where(EmailDelivery.organization_id == organization_id, EmailDelivery.id == delivery_id).with_for_update())
                if delivery is not None and delivery.status == "queued":
                    if delivery.attempts >= 3:
                        mark_delivery_failed(db, delivery, error.safe_code)
                        terminal = True
                    else:
                        delivery.safe_error_code = error.safe_code
                        delivery.version += 1
            logger.error("email_delivery_attempt_failed", extra={"context": {"delivery_id": str(delivery_id), "safe_error_code": error.safe_code}})
            exception = PermanentJobError if terminal else RetryableJobError
            raise exception(error.safe_code) from None
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
