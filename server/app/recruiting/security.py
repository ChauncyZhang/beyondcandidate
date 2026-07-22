import base64
import hashlib
import hmac
from dataclasses import dataclass

from cryptography.fernet import Fernet
from email_validator import EmailNotValidError, validate_email


@dataclass(frozen=True, repr=False)
class ProtectedContact:
    ciphertext: bytes
    lookup_hash: str
    masked_value: str

    def __repr__(self) -> str:
        return "ProtectedContact(ciphertext=<redacted>, lookup_hash=<redacted>, masked_value=%r)" % self.masked_value


class ContactCipher:
    def __init__(self, encryption_key: bytes, lookup_secret: bytes) -> None:
        try:
            encryption_material = base64.urlsafe_b64decode(encryption_key)
        except (ValueError, base64.binascii.Error):
            raise ValueError("invalid encryption key") from None
        if len(lookup_secret) != 32 or hmac.compare_digest(encryption_material, lookup_secret):
            raise ValueError("lookup key must be an independent 32-byte key")
        self._cipher = Fernet(encryption_key)
        self._lookup_secret = lookup_secret

    @staticmethod
    def normalize(kind: str, value: str) -> str:
        kind = kind.strip().casefold()
        value = value.strip()
        if kind not in {"email", "phone"}:
            raise ValueError("unsupported contact kind")
        if kind == "email":
            try:
                return validate_email(value, check_deliverability=False).normalized.casefold()
            except EmailNotValidError:
                raise ValueError("invalid email") from None
        normalized = ("+" if value.startswith("+") else "") + "".join(character for character in value if character.isdigit())
        digits = normalized.lstrip("+")
        if not 7 <= len(digits) <= 15:
            raise ValueError("invalid phone length")
        return normalized

    @staticmethod
    def mask(kind: str, value: str) -> str:
        if kind.strip().casefold() == "email":
            local, _, domain = value.partition("@")
            return f"{local[:1].casefold()}***@{domain.casefold()}"
        digits = "".join(character for character in value if character.isdigit())
        return f"***{digits[-4:]}" if digits else "***"

    def protect(self, kind: str, value: str) -> ProtectedContact:
        plaintext = value.strip()
        normalized = self.normalize(kind, plaintext)
        digest = hmac.new(self._lookup_secret, normalized.encode(), hashlib.sha256).hexdigest()
        return ProtectedContact(self._cipher.encrypt(plaintext.encode()), digest, self.mask(kind, plaintext))

    def decrypt(self, ciphertext: bytes) -> str:
        return self._cipher.decrypt(ciphertext).decode()
