from __future__ import annotations

import hashlib
import html
import multiprocessing
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Mapping, Protocol

from pypdf import PdfReader


MAX_TEMPLATE_BYTES = 128 * 1024
MAX_VARIABLE_BYTES = 16 * 1024
MAX_VARIABLES_BYTES = 64 * 1024
MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 12
RENDER_TIMEOUT_SECONDS = 15
WEASYPRINT_VERSION = "69.0"

REQUIRED_PLACEHOLDERS = frozenset(
    {
        "organization_name",
        "candidate_name",
        "job_title",
        "work_location",
        "response_deadline",
        "hr_name",
        "hr_email",
    }
)
OPTIONAL_PLACEHOLDERS = frozenset(
    {
        "salary",
        "currency",
        "start_date",
        "probation_period",
        "compensation_details",
        "benefits",
        "offer_body",
    }
)
ALLOWED_PLACEHOLDERS = REQUIRED_PLACEHOLDERS | OPTIONAL_PLACEHOLDERS

_PLACEHOLDER = re.compile(r"{{\s*([a-z][a-z0-9_]*)\s*}}")
_PLACEHOLDER_MARKER = re.compile(r"{{|}}")
_CSS_HAZARD = re.compile(
    r"(?:@import|@font-face|@namespace|@page|url\s*\(|expression\s*\(|javascript\s*:|behavior\s*:|-moz-binding)",
    re.IGNORECASE,
)
_ALLOWED_TAGS = {
    "html", "head", "title", "style", "body", "main", "article", "section",
    "header", "footer", "div", "p", "span", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "ul", "ol", "li",
    "strong", "em", "b", "i", "br", "hr", "address", "blockquote", "dl", "dt", "dd",
}
_ALLOWED_ATTRIBUTES = {"class", "id", "lang", "dir", "colspan", "rowspan", "scope"}
_FIXED_CSS = """
@page { size: A4; margin: 18mm 16mm; }
html, body { font-family: "Noto Sans CJK SC", sans-serif !important; font-size: 10.5pt; line-height: 1.55; }
body { color: #111; overflow-wrap: anywhere; }
table { border-collapse: collapse; max-width: 100%; }
img, svg, object, embed, iframe, form, input, button, select, textarea { display: none !important; }
a { color: inherit !important; text-decoration: none !important; }
"""
_WINDOWS_RUNTIME_ROOT = Path(__file__).resolve().parents[3] / ".tmp" / "weasyprint-runtime" / f"v{WEASYPRINT_VERSION}"


class OfferPdfError(ValueError):
    pass


class OfferPdfRenderTimeout(OfferPdfError):
    pass


class OfferPdfStorageError(RuntimeError):
    pass


class OfferPdfStorageConflict(OfferPdfStorageError):
    pass


class _TemplateValidator(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.style_depth = 0

    def handle_decl(self, decl: str) -> None:
        if decl.strip().casefold() != "doctype html":
            raise OfferPdfError("template declaration is not allowed")

    def handle_pi(self, data: str) -> None:
        raise OfferPdfError("template processing instructions are not allowed")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.casefold()
        if normalized not in _ALLOWED_TAGS:
            raise OfferPdfError(f"template tag is not allowed: {normalized}")
        for name, value in attrs:
            attribute = name.casefold()
            if attribute not in _ALLOWED_ATTRIBUTES or attribute.startswith("on"):
                raise OfferPdfError(f"template attribute is not allowed: {attribute}")
            if value and _PLACEHOLDER_MARKER.search(value):
                raise OfferPdfError("placeholders are only allowed in text nodes")
        if normalized == "style":
            self.style_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "style" and self.style_depth:
            self.style_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.style_depth and (_PLACEHOLDER_MARKER.search(data) or _CSS_HAZARD.search(data)):
            raise OfferPdfError("template stylesheet contains a forbidden construct")


def _validated_html(template_html: str, variables: Mapping[str, str]) -> tuple[str, bytes]:
    if not isinstance(template_html, str):
        raise OfferPdfError("template_html must be a string")
    template_bytes = template_html.encode("utf-8")
    if not template_bytes or len(template_bytes) > MAX_TEMPLATE_BYTES:
        raise OfferPdfError("template size is outside the allowed range")
    if not isinstance(variables, Mapping):
        raise OfferPdfError("variables must be a mapping")

    validator = _TemplateValidator()
    try:
        validator.feed(template_html)
        validator.close()
    except OfferPdfError:
        raise
    except Exception as error:
        raise OfferPdfError("template HTML is malformed") from error

    placeholders = set(_PLACEHOLDER.findall(template_html))
    remaining_markers = _PLACEHOLDER.sub("", template_html)
    if _PLACEHOLDER_MARKER.search(remaining_markers):
        raise OfferPdfError("template contains a malformed placeholder")
    unknown_placeholders = placeholders - ALLOWED_PLACEHOLDERS
    if unknown_placeholders:
        raise OfferPdfError("template contains an unknown placeholder")
    if not REQUIRED_PLACEHOLDERS.issubset(placeholders):
        raise OfferPdfError("template omits required placeholders")

    keys = set(variables)
    if any(not isinstance(key, str) for key in keys) or keys != placeholders:
        raise OfferPdfError("variables must exactly match template placeholders")
    total_bytes = 0
    escaped: dict[str, str] = {}
    for key in sorted(keys):
        value = variables[key]
        if not isinstance(value, str) or not value.strip():
            raise OfferPdfError("variable values must be non-blank strings")
        value_bytes = value.encode("utf-8")
        if len(value_bytes) > MAX_VARIABLE_BYTES:
            raise OfferPdfError("variable value is too large")
        total_bytes += len(value_bytes)
        escaped[key] = html.escape(value, quote=True)
    if total_bytes > MAX_VARIABLES_BYTES:
        raise OfferPdfError("variables are too large")

    rendered = _PLACEHOLDER.sub(lambda match: escaped[match.group(1)], template_html)
    input_digest = hashlib.sha256(template_bytes + b"\0" + b"\0".join(
        key.encode("ascii") + b"=" + variables[key].encode("utf-8") for key in sorted(keys)
    )).digest()
    return rendered, input_digest


def _deny_url_fetch(url: str, *args, **kwargs):
    raise OfferPdfError("template resources are not allowed")


def _assert_safe_pdf(pdf_bytes: bytes) -> None:
    if not pdf_bytes or len(pdf_bytes) > MAX_PDF_BYTES:
        raise OfferPdfError("rendered PDF is outside the allowed size")
    try:
        reader = PdfReader(BytesIO(pdf_bytes), strict=True)
        if not 1 <= len(reader.pages) <= MAX_PDF_PAGES:
            raise OfferPdfError("rendered PDF page count is outside the allowed range")
        root = reader.trailer["/Root"]
        for forbidden in ("/AcroForm", "/OpenAction", "/AA"):
            if forbidden in root:
                raise OfferPdfError("rendered PDF contains active content")
        names = root.get("/Names")
        if names and ("/EmbeddedFiles" in names or "/JavaScript" in names):
            raise OfferPdfError("rendered PDF contains active content or attachments")
        for page in reader.pages:
            if "/AA" in page or "/Annots" in page:
                raise OfferPdfError("rendered PDF contains interactive content")
    except OfferPdfError:
        raise
    except Exception as error:
        raise OfferPdfError("rendered PDF is invalid") from error


def _render_worker(connection, rendered_html: str, identifier: bytes) -> None:
    try:
        if sys.platform == "win32":
            pdf_bytes = _render_windows_pdf(rendered_html, identifier)
            _assert_safe_pdf(pdf_bytes)
            connection.send((True, pdf_bytes))
            return
        from weasyprint import CSS, HTML
        from weasyprint.text.fonts import FontConfiguration

        font_config = FontConfiguration()
        document = HTML(string=rendered_html, base_url=None, url_fetcher=_deny_url_fetch).render(
            stylesheets=[CSS(string=_FIXED_CSS, font_config=font_config)],
            font_config=font_config,
            presentational_hints=False,
        )
        if not 1 <= len(document.pages) <= MAX_PDF_PAGES:
            raise OfferPdfError("rendered PDF page count is outside the allowed range")
        document.metadata.attachments = []
        document.metadata.created = "2000-01-01T00:00:00Z"
        document.metadata.modified = "2000-01-01T00:00:00Z"
        document.metadata.generator = "BeyondCandidate Offer PDF Renderer"
        document.metadata.custom = {}
        document.metadata.xmp_metadata = []
        pdf_bytes = document.write_pdf(
            pdf_identifier=identifier,
            pdf_variant="pdf/a-2u",
            pdf_forms=False,
            pdf_tags=False,
            attachments=None,
            custom_metadata=False,
            presentational_hints=False,
        )
        _assert_safe_pdf(pdf_bytes)
        connection.send((True, pdf_bytes))
    except BaseException as error:
        connection.send((False, str(error)[:300] or error.__class__.__name__))
    finally:
        connection.close()


def _render_windows_pdf(rendered_html: str, identifier: bytes) -> bytes:
    executable = Path(os.environ.get("BEYONDCANDIDATE_WEASYPRINT_EXECUTABLE", _WINDOWS_RUNTIME_ROOT / "weasyprint.exe"))
    font = Path(os.environ.get("BEYONDCANDIDATE_OFFER_FONT", _WINDOWS_RUNTIME_ROOT / "NotoSansCJKsc-Regular.otf"))
    if not executable.is_file() or not font.is_file():
        raise OfferPdfError("Windows Offer PDF runtime is missing; run server/scripts/install_weasyprint_windows.ps1")
    approved_css = (
        f'@font-face {{ font-family: "BeyondCandidate Noto CJK"; src: url("{font.resolve().as_uri()}"); }}\n'
        + _FIXED_CSS.replace('"Noto Sans CJK SC"', '"BeyondCandidate Noto CJK"')
    )
    fixed_head = (
        '<meta name="dcterms.created" content="2000-01-01T00:00:00Z">'
        '<meta name="dcterms.modified" content="2000-01-01T00:00:00Z">'
        '<meta name="generator" content="BeyondCandidate Offer PDF Renderer">'
        f"<style>{approved_css}</style>"
    )
    command_html, replaced = re.subn(r"</head\s*>", fixed_head + "</head>", rendered_html, count=1, flags=re.IGNORECASE)
    if replaced != 1:
        raise OfferPdfError("template must contain a head element")
    command = [
        str(executable),
        "--encoding", "utf-8",
        "--pdf-identifier", identifier.hex(),
        "--pdf-variant", "pdf/a-2u",
        "--allowed-protocols", "file",
        "--no-http-redirects",
        "--fail-on-http-errors",
        "-", "-",
    ]
    try:
        completed = subprocess.run(
            command,
            input=command_html.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=RENDER_TIMEOUT_SECONDS - 2,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise OfferPdfRenderTimeout("offer PDF rendering timed out") from error
    if completed.returncode != 0:
        raise OfferPdfError("Windows Offer PDF runtime failed")
    return completed.stdout


def render_offer_pdf(template_html: str, variables: Mapping[str, str]) -> bytes:
    rendered_html, identifier = _validated_html(template_html, variables)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_render_worker, args=(child, rendered_html, identifier), daemon=True)
    process.start()
    child.close()
    try:
        if not parent.poll(RENDER_TIMEOUT_SECONDS):
            process.terminate()
            process.join(2)
            if process.is_alive():
                process.kill()
                process.join(2)
            raise OfferPdfRenderTimeout("offer PDF rendering timed out")
        succeeded, result = parent.recv()
    except EOFError as error:
        raise OfferPdfError("offer PDF renderer exited unexpectedly") from error
    finally:
        parent.close()
        process.join(2)
        if process.is_alive():
            process.kill()
            process.join(2)
    if not succeeded:
        raise OfferPdfError(f"offer PDF rendering failed: {result}")
    _assert_safe_pdf(result)
    return result


class PrivateOfferPdfStorage(Protocol):
    def write_immutable(self, storage_key: str, content: bytes, sha256: str) -> None: ...


@dataclass(frozen=True)
class OfferPdfObject:
    storage_key: str
    sha256: str
    size_bytes: int


def offer_pdf_storage_key(organization_id, offer_id, offer_version_id) -> str:
    return f"offers/{organization_id}/offers/{offer_id}/versions/{offer_version_id}.pdf"


class MinioOfferPdfStorage:
    def __init__(self, client, private_bucket: str) -> None:
        self.client = client
        self.private_bucket = private_bucket

    @staticmethod
    def _metadata_digest(stat) -> str | None:
        metadata = getattr(stat, "metadata", {}) or {}
        normalized = {str(key).casefold().removeprefix("x-amz-meta-"): str(value) for key, value in metadata.items()}
        return normalized.get("sha256")

    @staticmethod
    def _is_precondition_failure(error: Exception) -> bool:
        response = getattr(error, "response", None)
        return getattr(error, "code", None) == "PreconditionFailed" or getattr(response, "status", None) == 412

    def write_immutable(self, storage_key: str, content: bytes, sha256: str) -> None:
        headers = {
            "Content-Type": "application/pdf",
            "If-None-Match": "*",
            "X-Amz-Meta-Sha256": sha256,
            "X-Amz-Meta-Immutable": "true",
        }
        try:
            self.client._put_object(self.private_bucket, storage_key, content, headers)
        except Exception as error:
            if not self._is_precondition_failure(error):
                raise OfferPdfStorageError("private offer PDF storage is unavailable") from None
            try:
                existing = self.client.stat_object(self.private_bucket, storage_key)
            except Exception:
                raise OfferPdfStorageError("private offer PDF storage is unavailable") from None
            if getattr(existing, "size", None) == len(content) and self._metadata_digest(existing) == sha256:
                return
            raise OfferPdfStorageConflict("immutable offer PDF object already exists with different content") from None
