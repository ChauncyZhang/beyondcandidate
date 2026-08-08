from cryptography.fernet import Fernet, InvalidToken
from email_validator import EmailNotValidError, validate_email


class EmailSecretCipher:
    def __init__(self, key: bytes) -> None:
        try:
            self._cipher = Fernet(key)
        except Exception:
            raise ValueError("invalid email encryption key") from None

    def encrypt(self, value: str) -> bytes:
        if not value or len(value) > 4096:
            raise ValueError("invalid protected email value")
        return self._cipher.encrypt(value.encode())

    def decrypt(self, value: bytes) -> str:
        try:
            return self._cipher.decrypt(value).decode()
        except (InvalidToken, UnicodeDecodeError):
            raise ValueError("protected email value cannot be decrypted") from None

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
