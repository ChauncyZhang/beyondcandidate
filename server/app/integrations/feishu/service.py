from __future__ import annotations

import hashlib
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken

from server.app.integrations.feishu.provider import OAuthIdentity


class FeishuSecretCipher:
    def __init__(self, key: bytes) -> None:
        try:
            self._cipher = Fernet(key)
        except Exception:
            raise ValueError("invalid Feishu encryption key") from None

    def encrypt(self, value: str) -> bytes:
        if not value or len(value) > 4096:
            raise ValueError("invalid Feishu secret")
        return self._cipher.encrypt(value.encode())

    def decrypt(self, value: bytes) -> str:
        try:
            return self._cipher.decrypt(value).decode()
        except (InvalidToken, UnicodeDecodeError):
            raise ValueError("Feishu secret cannot be decrypted") from None


def hash_oauth_state(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def stable_identity_key(identity: OAuthIdentity) -> tuple[str, str]:
    if identity.union_id:
        return "union_id", identity.union_id
    if identity.open_id:
        return "open_id", identity.open_id
    raise ValueError("provider returned no stable identity")


def public_config(config) -> dict[str, object]:
    return {
        "app_id": config.app_id,
        "redirect_uri": config.redirect_uri,
        "calendar_id": config.calendar_id,
        "enabled": config.enabled,
        "app_secret_configured": config.encrypted_app_secret is not None,
        "verification_token_configured": config.encrypted_verification_token is not None,
        "encrypt_key_configured": config.encrypted_encrypt_key is not None,
        "version": config.version,
        "last_test_status": config.last_test_status,
        "last_tested_at": _iso(config.last_tested_at),
        "last_test_error_code": config.last_test_error_code,
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
