from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


MAX_WEBHOOK_BYTES = 1024 * 1024


class FeishuWebhookError(ValueError):
    pass


@dataclass(frozen=True)
class FeishuWebhookConfig:
    organization_id: UUID
    app_id: str
    verification_token: str
    encrypt_key: str | None
    tenant_keys: frozenset[str]


@dataclass(frozen=True)
class VerifiedFeishuWebhook:
    organization_id: UUID
    payload: dict[str, Any]


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FeishuWebhookError("invalid JSON") from None
    if not isinstance(value, dict):
        raise FeishuWebhookError("webhook body must be an object")
    return value


def _decrypt(encrypted: object, encrypt_key: str) -> dict[str, Any]:
    if not isinstance(encrypted, str) or len(encrypted) > MAX_WEBHOOK_BYTES * 2:
        raise FeishuWebhookError("invalid encrypted body")
    try:
        ciphertext = base64.b64decode(encrypted, validate=True)
        if len(ciphertext) < 32 or len(ciphertext) % 16:
            raise ValueError
        decryptor = Cipher(
            algorithms.AES(hashlib.sha256(encrypt_key.encode()).digest()),
            modes.CBC(ciphertext[:16]),
        ).decryptor()
        padded = decryptor.update(ciphertext[16:]) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
    except (ValueError, TypeError):
        raise FeishuWebhookError("invalid encrypted body") from None
    return _json_object(plaintext)


def _token(payload: dict[str, Any]) -> str | None:
    header = payload.get("header")
    value = header.get("token") if isinstance(header, dict) else payload.get("token")
    return value if isinstance(value, str) else None


def _matches_identity(config: FeishuWebhookConfig, payload: dict[str, Any]) -> bool:
    header = payload.get("header")
    if not isinstance(header, dict):
        return payload.get("type") == "url_verification"
    app_id = header.get("app_id")
    tenant_key = header.get("tenant_key")
    if payload.get("type") == "url_verification":
        if app_id is not None and app_id != config.app_id:
            return False
        return tenant_key is None or tenant_key in config.tenant_keys
    if app_id != config.app_id:
        return False
    if tenant_key not in config.tenant_keys:
        return False
    return True


def verify_webhook(
    raw_body: bytes,
    headers: dict[str, str],
    configs: list[FeishuWebhookConfig],
) -> VerifiedFeishuWebhook:
    if not raw_body or len(raw_body) > MAX_WEBHOOK_BYTES:
        raise FeishuWebhookError("invalid webhook size")
    outer = _json_object(raw_body)
    matches: list[tuple[FeishuWebhookConfig, dict[str, Any]]] = []

    if "encrypt" in outer:
        signature_names = (
            "x-lark-request-timestamp",
            "x-lark-request-nonce",
            "x-lark-signature",
        )
        signature_presence = tuple(name in headers for name in signature_names)
        timestamp, nonce, signature = (headers.get(name) for name in signature_names)
        signature_values = (timestamp, nonce, signature)
        if any(signature_presence) and (
            not all(signature_presence) or not all(signature_values)
        ):
            raise FeishuWebhookError("missing signature")
        signed_request = all(signature_presence)
        for config in configs:
            if not config.encrypt_key:
                continue
            if signed_request:
                expected = hashlib.sha256(
                    (timestamp + nonce + config.encrypt_key).encode() + raw_body
                ).hexdigest()
                if not secrets.compare_digest(expected, signature):
                    continue
            try:
                payload = _decrypt(outer.get("encrypt"), config.encrypt_key)
            except FeishuWebhookError:
                continue
            # Feishu's initial encrypted URL verification does not carry the
            # event-signature headers. All ordinary encrypted events still do.
            if not signed_request and payload.get("type") != "url_verification":
                continue
            token = _token(payload)
            if (
                token is not None
                and secrets.compare_digest(config.verification_token, token)
                and _matches_identity(config, payload)
            ):
                matches.append((config, payload))
    else:
        token = _token(outer)
        if token is not None:
            matches.extend(
                (config, outer)
                for config in configs
                if config.encrypt_key is None
                and secrets.compare_digest(config.verification_token, token)
                and _matches_identity(config, outer)
            )

    if len(matches) != 1:
        raise FeishuWebhookError("webhook verification failed")
    config, payload = matches[0]
    return VerifiedFeishuWebhook(config.organization_id, payload)
