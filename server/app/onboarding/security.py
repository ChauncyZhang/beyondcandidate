from __future__ import annotations

import base64
import hashlib
import hmac
import json

from cryptography.fernet import Fernet, InvalidToken


class OnboardingPiiCipher:
    """Purpose-separated encryption derived from CONTACT_ENCRYPTION_KEY."""

    def __init__(self, contact_encryption_key: bytes) -> None:
        try:
            source = base64.urlsafe_b64decode(contact_encryption_key)
        except (ValueError, base64.binascii.Error):
            raise ValueError("invalid contact encryption key") from None
        if len(source) != 32:
            raise ValueError("contact encryption key must decode to 32 bytes")
        derived = hmac.new(source, b"beyondcandidate:onboarding-pii:v1", hashlib.sha256).digest()
        self._cipher = Fernet(base64.urlsafe_b64encode(derived))

    def encrypt(self, value: dict[str, str]) -> bytes:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return self._cipher.encrypt(payload.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> dict[str, str]:
        try:
            value = json.loads(self._cipher.decrypt(ciphertext).decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, ValueError, TypeError):
            raise ValueError("invalid onboarding ciphertext") from None
        if not isinstance(value, dict) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
            raise ValueError("invalid onboarding payload")
        return value
