import asyncio
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from typing import Protocol

import aiosmtplib

# Private/internal SMTP relays are intentionally supported for self-hosted enterprises.
# Shared-tenant deployments must enforce relay reachability through worker egress policy.
# SMTP delivery is at-least-once at the ambiguous post-DATA boundary; the stable
# Message-ID lets operators reconcile retries but does not provide exactly-once delivery.


def _header(value: str, field: str) -> str:
    if not value or "\r" in value or "\n" in value:
        raise ValueError(f"invalid {field}")
    return value


@dataclass(frozen=True)
class MailMessage:
    recipient: str
    sender_email: str
    sender_name: str
    reply_to_email: str
    reply_to_name: str
    subject: str
    body: str
    message_id: str


@dataclass(frozen=True)
class ProviderReceipt:
    receipt_id: str


class MailProvider(Protocol):
    async def send(self, message: MailMessage) -> ProviderReceipt: ...


class MailError(RuntimeError):
    def __init__(self, safe_code: str, _private_detail: str | None = None) -> None:
        self.safe_code = safe_code
        super().__init__(safe_code)


class TemporaryMailError(MailError): pass
class PermanentMailError(MailError): pass


class SmtpMailProvider:
    def __init__(self, *, host: str, port: int, tls_mode: str, username: str, password: str, timeout_seconds: float = 10) -> None:
        if tls_mode not in {"starttls", "tls"}:
            raise ValueError("invalid SMTP TLS mode")
        self._host, self._port, self._tls_mode = host, port, tls_mode
        self._username, self._password, self._timeout = username, password, timeout_seconds

    async def send(self, message: MailMessage) -> ProviderReceipt:
        mail = EmailMessage()
        mail["From"] = formataddr((_header(message.sender_name, "sender name"), _header(message.sender_email, "sender email")))
        mail["To"] = _header(message.recipient, "recipient")
        mail["Reply-To"] = formataddr((_header(message.reply_to_name, "reply-to name"), _header(message.reply_to_email, "reply-to email")))
        mail["Subject"] = _header(message.subject, "subject")
        mail["Message-ID"] = _header(message.message_id, "message id")
        mail.set_content(message.body)
        try:
            await aiosmtplib.send(
                mail,
                hostname=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                timeout=self._timeout,
                use_tls=self._tls_mode == "tls",
                start_tls=self._tls_mode == "starttls",
                validate_certs=True,
            )
        except aiosmtplib.SMTPAuthenticationError:
            raise PermanentMailError("smtp_authentication_failed") from None
        except aiosmtplib.SMTPRecipientsRefused:
            raise PermanentMailError("smtp_recipient_rejected") from None
        except aiosmtplib.SMTPResponseException as error:
            kind = PermanentMailError if error.code >= 500 else TemporaryMailError
            raise kind("smtp_rejected" if error.code >= 500 else "smtp_unavailable") from None
        except (aiosmtplib.SMTPException, OSError, asyncio.TimeoutError, TimeoutError):
            raise TemporaryMailError("smtp_unavailable") from None
        return ProviderReceipt(str(uuid.uuid4()))
