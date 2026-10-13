import hashlib
import io
import re
import unicodedata
from urllib.parse import urlsplit

from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject
from sqlalchemy import select
from starlette.requests import Request
from starlette.formparsers import MultiPartException

from server.app.offers.models import OfferAttachment
from server.app.screening.scanner import ClamAvScanner, ScanResult


MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_PDF_OBJECTS = 50000
MAX_PDF_DEPTH = 128
FORBIDDEN_PDF_KEYS = frozenset({
    "/AA", "/OpenAction", "/JS", "/JavaScript", "/EmbeddedFiles", "/EF",
    "/XFA", "/RichMediaContent", "/RichMediaSettings",
})
PDF_ACTION_TYPES = frozenset({
    "/JavaScript", "/Launch", "/SubmitForm", "/ImportData", "/GoToR", "/GoToE",
    "/Rendition", "/Movie", "/Sound", "/Hide", "/SetOCGState", "/Trans", "/GoTo3DView",
    "/Thread", "/Named", "/ResetForm",
})


def _validate_passive_action(value) -> None:
    action_type = value["/S"] if "/S" in value else None
    if action_type == "/GoTo" and "/D" in value:
        return
    if action_type == "/URI" and "/URI" in value:
        uri = value["/URI"]
        if isinstance(uri, str) and not any(ord(c) < 32 or ord(c) == 127 for c in uri):
            parsed = urlsplit(uri)
            if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
                return
            if parsed.scheme.lower() == "mailto" and parsed.path:
                return
    raise ValueError("PDF unsafe or unknown action")


def _reject_active_pdf_objects(root) -> None:
    # Walk raw dictionary/array values, not decoded content streams. References
    # and container identities break parent cycles; budgets bound hostile graphs.
    pending = [(root, 0, False)]
    references, containers = set(), set()
    visited = 0
    while pending:
        value, depth, action_context = pending.pop()
        visited += 1
        if visited > MAX_PDF_OBJECTS or depth > MAX_PDF_DEPTH:
            raise ValueError("PDF object traversal limit")
        if isinstance(value, IndirectObject):
            identity = (value.idnum, value.generation, action_context)
            if identity in references:
                continue
            references.add(identity)
            value = value.get_object()
            if isinstance(value, IndirectObject):
                pending.append((value, depth + 1, action_context))
                continue
        if isinstance(value, NameObject) and value in PDF_ACTION_TYPES:
            raise ValueError("PDF unsafe action")
        if isinstance(value, (DictionaryObject, ArrayObject)):
            identity = (id(value), action_context)
            if identity in containers:
                continue
            containers.add(identity)
            if isinstance(value, DictionaryObject):
                if FORBIDDEN_PDF_KEYS.intersection(value.keys()):
                    raise ValueError("PDF active content")
                action_type = value["/S"] if "/S" in value else None
                object_type = value["/Type"] if "/Type" in value else None
                is_action = action_context or object_type == "/Action" or action_type in PDF_ACTION_TYPES | {"/GoTo", "/URI"}
                if is_action:
                    _validate_passive_action(value)
                subtype = value["/Subtype"] if "/Subtype" in value else None
                if subtype in {"/FileAttachment", "/RichMedia", "/Screen", "/Movie", "/Sound", "/3D"}:
                    raise ValueError("PDF embedded content")
                # Tagged PDF structure elements use /A for layout attributes, not actions.
                children = ((child, depth + 1, (key == "/A" and object_type != "/StructElem") or (is_action and key == "/Next")) for key, child in value.items())
            else:
                children = ((child, depth + 1, action_context) for child in value)
            if visited + len(pending) + len(value) > MAX_PDF_OBJECTS:
                raise ValueError("PDF object traversal limit")
            pending.extend(children)
        elif action_context:
            raise ValueError("PDF invalid action object")


def bounded_upload_request(request: Request) -> Request:
    total = 0

    async def receive():
        nonlocal total
        message = await request.receive()
        if message["type"] == "http.request":
            total += len(message.get("body", b""))
            # Permit multipart framing, but bound spool/disk use even without Content-Length.
            if total > MAX_ATTACHMENT_BYTES + 64 * 1024:
                raise MultiPartException("offer_attachment_too_large")
        return message

    return Request(request.scope, receive=receive)


class AttachmentError(ValueError):
    def __init__(self, code: str, status: int = 422):
        self.code, self.status = code, status
        super().__init__(code)


def sanitize_filename(filename: str | None) -> str:
    filename = unicodedata.normalize("NFC", filename or "offer.pdf")
    filename = re.split(r"[/\\]", filename)[-1]
    filename = "".join(c for c in filename if not unicodedata.category(c).startswith("C"))
    filename = re.sub(r'[<>:"|?*]', "_", filename).strip(" .")
    if not filename.lower().endswith(".pdf"):
        raise AttachmentError("offer_attachment_invalid_type")
    return (filename[:-4].strip(" .")[:240] or "offer") + ".pdf"


async def scan_attachment(content: bytes, settings) -> None:
    scanner = ClamAvScanner(
        settings.clamav_host, settings.clamav_port,
        connect_timeout=settings.clamav_connect_timeout_seconds,
        read_timeout=settings.clamav_read_timeout_seconds,
        total_timeout=settings.clamav_total_timeout_seconds,
    )
    try:
        result = await scanner.scan(io.BytesIO(content), MAX_ATTACHMENT_BYTES)
    except Exception:
        raise AttachmentError("offer_attachment_scanner_unavailable", 503) from None
    if result == ScanResult.INFECTED:
        raise AttachmentError("offer_attachment_malware_detected")
    if result != ScanResult.CLEAN:
        raise AttachmentError("offer_attachment_scanner_unavailable", 503)


def validate_pdf(content: bytes) -> None:
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise AttachmentError("offer_attachment_too_large", 413)
    try:
        if not content.startswith(b"%PDF-"):
            raise ValueError
        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise ValueError
        _reject_active_pdf_objects(reader.trailer)
        if not 1 <= len(reader.pages) <= 200:
            raise ValueError
    except Exception:
        raise AttachmentError("offer_attachment_invalid_pdf") from None


def current_attachment(db, offer):
    return db.scalar(select(OfferAttachment).where(
        OfferAttachment.organization_id == offer.organization_id,
        OfferAttachment.offer_id == offer.id,
        OfferAttachment.offer_version_id == offer.current_version_id,
    ))


def attachment_view(attachment):
    if attachment is None:
        return None
    return {"filename": attachment.filename, "size_bytes": attachment.size_bytes,
            "content_type": attachment.content_type, "sha256": attachment.sha256}


def attachment_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
