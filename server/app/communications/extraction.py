import base64
import re

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from server.app.recruiting.models import CandidateContact
from server.app.recruiting.security import ContactCipher
from server.app.screening.actions import CandidateTombstoned, lock_screening_candidate


_MAX_TEXT_CHARS = 200_000
_MAX_SCANNED_CANDIDATES = 10
_MAX_EMAILS = 10
_EMAIL_CANDIDATE = re.compile(
    r"[A-Za-z0-9.!#$%&'+/=?^_`{|}~-]{1,64}\s*@\s*"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}"
)


def extract_email_candidates(text: str) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for scanned, match in enumerate(_EMAIL_CANDIDATE.finditer(text[:_MAX_TEXT_CHARS]), start=1):
        if len(values) == _MAX_EMAILS or scanned > _MAX_SCANNED_CANDIDATES:
            break
        value = re.sub(r"\s*@\s*", "@", match.group(0))
        try:
            normalized = validate_email(value, check_deliverability=False).normalized
        except EmailNotValidError:
            continue
        if normalized.rsplit("@", 1)[1].casefold().endswith(".test"):
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        values.append(value)
    return tuple(values)


def persist_extracted_emails(db, *, organization_id, candidate_id, values, source: str, cipher: ContactCipher) -> int:
    if source not in {"native", "ocr", "manual"}:
        raise ValueError("invalid extracted email source")
    try:
        lock_screening_candidate(db, organization_id, candidate_id)
    except CandidateTombstoned:
        return 0

    created = 0
    seen_hashes: set[str] = set()
    for value in values:
        try:
            protected = cipher.protect("email", value)
        except ValueError:
            continue
        if protected.lookup_hash in seen_hashes:
            continue
        seen_hashes.add(protected.lookup_hash)
        existing = db.scalar(
            select(CandidateContact.id).where(
                CandidateContact.organization_id == organization_id,
                CandidateContact.kind == "email",
                CandidateContact.lookup_hash == protected.lookup_hash,
            )
        )
        if existing is not None:
            continue
        try:
            with db.begin_nested():
                db.add(
                    CandidateContact(
                        organization_id=organization_id,
                        candidate_id=candidate_id,
                        kind="email",
                        ciphertext=protected.ciphertext,
                        lookup_hash=protected.lookup_hash,
                        masked_value=protected.masked_value,
                        source=source,
                        confirmation_status="unconfirmed",
                    )
                )
                db.flush()
        except IntegrityError:
            continue
        created += 1
    return created


def contact_cipher_from_settings(settings) -> ContactCipher:
    encryption_key = settings.contact_encryption_key.get_secret_value()
    lookup_secret = settings.contact_lookup_secret.get_secret_value()
    if encryption_key == "change-me":
        return ContactCipher(
            b"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
            b"fedcba9876543210fedcba9876543210",
        )
    raw_lookup_secret = lookup_secret.encode()
    if len(raw_lookup_secret) != 32:
        try:
            raw_lookup_secret = base64.urlsafe_b64decode(raw_lookup_secret)
        except (ValueError, base64.binascii.Error):
            raise ValueError("invalid contact lookup secret") from None
    if len(raw_lookup_secret) != 32:
        raise ValueError("contact lookup secret must decode to 32 bytes")
    return ContactCipher(encryption_key.encode(), raw_lookup_secret)
