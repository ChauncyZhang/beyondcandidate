import re
import unicodedata
from urllib.parse import quote

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def derive_cursor_key(source: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"ux09/recruiting/cursor/v1",
    ).derive(source)


def content_disposition(stored_filename: str) -> str:
    if CONTROL.search(stored_filename):
        raise ValueError("filename contains control characters")
    normalized = unicodedata.normalize("NFC", stored_filename.replace("\\", "/")).rsplit("/", 1)[-1]
    if len(normalized) > 119:
        dot = normalized.rfind(".")
        suffix = normalized[dot:] if 0 < dot and len(normalized) - dot <= 10 else ""
        normalized = normalized[:119 - len(suffix)] + suffix
    normalized = normalized or "download"
    ascii_name = normalized.encode("ascii", "ignore").decode()
    if not ascii_name or ascii_name.startswith("."):
        suffix = ".pdf" if normalized.casefold().endswith(".pdf") else ""
        ascii_name = f"download{suffix}"
    ascii_name = ascii_name.replace('"', "_").replace(";", "_")
    encoded = quote(normalized, safe="!#$&+-.^_`|~")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"
