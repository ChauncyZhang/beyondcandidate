import base64
import hashlib
import hmac
import json

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from email_validator import EmailNotValidError, validate_email


class EmailSecretCipher:
    def __init__(self, key: bytes) -> None:
        try:
            master = base64.urlsafe_b64decode(key)
            if len(master) != 32:
                raise ValueError
        except Exception:
            raise ValueError("invalid email encryption key") from None

        def derive(purpose: bytes) -> bytes:
            return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"BeyondCandidate/email/v1", info=purpose).derive(master)

        self._smtp_cipher = Fernet(base64.urlsafe_b64encode(derive(b"smtp-password")))
        self._recipient_cipher = Fernet(base64.urlsafe_b64encode(derive(b"recipient")))
        self._attachment_cipher = Fernet(base64.urlsafe_b64encode(derive(b"attachment-snapshot")))
        self._idempotency_key = derive(b"idempotency-hmac")

    @staticmethod
    def _encrypt(cipher: Fernet, value: str) -> bytes:
        if not value or len(value) > 4096:
            raise ValueError("invalid protected email value")
        return cipher.encrypt(value.encode())

    @staticmethod
    def _decrypt(cipher: Fernet, value: bytes) -> str:
        try:
            return cipher.decrypt(value).decode()
        except (InvalidToken, UnicodeDecodeError):
            raise ValueError("protected email value cannot be decrypted") from None

    def encrypt_smtp_password(self, value: str) -> bytes:
        return self._encrypt(self._smtp_cipher, value)

    def decrypt_smtp_password(self, value: bytes) -> str:
        return self._decrypt(self._smtp_cipher, value)

    def encrypt_recipient(self, value: str) -> bytes:
        return self._encrypt(self._recipient_cipher, value)

    def decrypt_recipient(self, value: bytes) -> str:
        return self._decrypt(self._recipient_cipher, value)

    def encrypt_attachment(self, value: bytes) -> bytes:
        if not value or len(value) > 1024 * 1024:
            raise ValueError("invalid attachment snapshot")
        return self._attachment_cipher.encrypt(value)

    def decrypt_attachment(self, value: bytes) -> bytes:
        try:
            return self._attachment_cipher.decrypt(value)
        except InvalidToken:
            raise ValueError("attachment snapshot cannot be decrypted") from None

    def fingerprint(self, purpose: str, payload: object) -> str:
        if not purpose or len(purpose) > 100:
            raise ValueError("invalid fingerprint purpose")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hmac.new(self._idempotency_key, purpose.encode() + b"\0" + encoded, hashlib.sha256).hexdigest()

    @staticmethod
    def normalize_email(value: str) -> str:
        try:
            return validate_email(value, check_deliverability=False).normalized
        except EmailNotValidError:
            raise ValueError("invalid email address") from None

    @classmethod
    def mask_email(cls, value: str) -> str:
        normalized = cls.normalize_email(value)
        local, domain = normalized.rsplit("@", 1)
        if len(local) == 1:
            masked = "*"
        elif len(local) == 2:
            masked = local[0] + "*"
        else:
            masked = local[0] + "*" * (len(local) - 2) + local[-1]
        return f"{masked}@{domain}"
