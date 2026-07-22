import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any


SENSITIVE_KEY_FRAGMENTS = {
    "api_key",
    "authorization",
    "body",
    "cookie",
    "database_url",
    "headers",
    "ledger_key",
    "object_key",
    "password",
    "payload",
    "secret",
    "session",
    "token",
}


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]"
            if any(fragment in str(key).lower() for fragment in SENSITIVE_KEY_FRAGMENTS)
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        }
        context = getattr(record, "context", None)
        if context is not None:
            payload["context"] = redact(context)
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
