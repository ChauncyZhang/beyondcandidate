import re


_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{5,}\d)(?!\w)")
_LABELED_FIELD = re.compile(
    r"(?im)^(?P<label>\s*(?:姓名|名字|候选人姓名|name|candidate\s+name|地址|住址|家庭住址|address)\s*[:：]\s*)(?P<value>[^\r\n]*)"
)


def redact_screening_text(text: str, *, candidate_name: str | None = None) -> str:
    """Return a deterministic copy with direct candidate identifiers removed."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    redacted = _LABELED_FIELD.sub(lambda match: match.group("label") + "[REDACTED]", text)
    if candidate_name and candidate_name.strip():
        redacted = re.sub(re.escape(candidate_name.strip()), "[REDACTED_NAME]", redacted, flags=re.IGNORECASE)
    redacted = _EMAIL.sub("[REDACTED_EMAIL]", redacted)
    return _PHONE.sub("[REDACTED_PHONE]", redacted)
