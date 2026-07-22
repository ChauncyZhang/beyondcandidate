import base64
import hashlib
import hmac
import json


class InvalidCursor(Exception):
    pass


class CursorCodec:
    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    def encode(self, organization_id: str, sort: str, value: str, resource_id: str) -> str:
        payload = json.dumps({"organization_id": organization_id, "sort": sort, "value": value, "id": resource_id}, separators=(",", ":")).encode()
        signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(signature + payload).decode().rstrip("=")

    def decode(self, token: str, organization_id: str, sort: str) -> dict[str, str]:
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            signature, payload = raw[:32], raw[32:]
            if not hmac.compare_digest(signature, hmac.new(self._secret, payload, hashlib.sha256).digest()):
                raise InvalidCursor
            decoded = json.loads(payload)
            if decoded["organization_id"] != organization_id or decoded["sort"] != sort:
                raise InvalidCursor
            return decoded
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise InvalidCursor from None
