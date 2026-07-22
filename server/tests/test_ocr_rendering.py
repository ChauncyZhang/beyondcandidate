import asyncio
import io
import time

import pytest
from pypdf import PdfWriter

from server.app.screening.ocr_rendering import (
    IsolatedOcrRenderer,
    OcrRenderingError,
    OcrRenderLimits,
)


def _pdf_bytes(*, pages: int = 1, encrypted: bool = False, width: int = 100, height: int = 100) -> bytes:
    stream = io.BytesIO()
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=width, height=height)
    if encrypted:
        writer.encrypt("secret")
    writer.write(stream)
    return stream.getvalue()


def _scanned_image_pdf() -> bytes:
    return _pdf_bytes(width=144, height=72)


def _render(data: bytes, *, limits: OcrRenderLimits = OcrRenderLimits(), timeout: float = 10):
    return asyncio.run(IsolatedOcrRenderer(timeout_seconds=timeout).render_pdf(io.BytesIO(data), limits=limits))


def test_renders_scanned_pdf_to_ordered_in_memory_png_pages() -> None:
    pages = _render(_scanned_image_pdf())
    assert len(pages) == 1
    assert pages[0].page_number == 1
    assert pages[0].media_type == "image/png"
    assert pages[0].image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert pages[0].width > pages[0].height > 0
    assert "path" not in pages[0].__dict__


@pytest.mark.parametrize(
    ("data", "limits", "code"),
    [
        (b"%PDF-broken", OcrRenderLimits(), "pdf_malformed"),
        (_pdf_bytes(encrypted=True), OcrRenderLimits(), "pdf_encrypted"),
        (_pdf_bytes(pages=2), OcrRenderLimits(max_pages=1), "pdf_page_limit"),
        (_pdf_bytes(width=1000, height=1000), OcrRenderLimits(max_page_pixels=100), "pdf_pixel_limit"),
        (_pdf_bytes(pages=2), OcrRenderLimits(max_total_pixels=100), "pdf_total_pixel_limit"),
        (_pdf_bytes(), OcrRenderLimits(max_total_bytes=8), "pdf_render_byte_limit"),
    ],
)
def test_rejects_malformed_encrypted_and_limits_without_partial_pages(data: bytes, limits: OcrRenderLimits, code: str) -> None:
    with pytest.raises(OcrRenderingError) as raised:
        _render(data, limits=limits)
    assert raised.value.safe_code == code
    assert str(raised.value) == code


def test_rejects_source_over_limit_before_starting_worker() -> None:
    renderer = IsolatedOcrRenderer(timeout_seconds=2)
    with pytest.raises(OcrRenderingError) as raised:
        asyncio.run(renderer.render_pdf(io.BytesIO(b"%PDF-" + b"x" * 20), limits=OcrRenderLimits(max_source_bytes=10)))
    assert raised.value.safe_code == "file_too_large"
    assert renderer.last_pid is None


def test_timeout_kills_isolated_renderer() -> None:
    renderer = IsolatedOcrRenderer(timeout_seconds=0.1, worker_module="server.tests.parser_hang_worker")
    started = time.monotonic()
    with pytest.raises(OcrRenderingError) as raised:
        asyncio.run(renderer.render_pdf(io.BytesIO(_pdf_bytes())))
    assert raised.value.safe_code == "pdf_render_timeout"
    assert time.monotonic() - started < 2


def test_render_limits_validate_configuration() -> None:
    with pytest.raises(ValueError):
        OcrRenderLimits(dpi=0)
    with pytest.raises(ValueError):
        OcrRenderLimits(dpi=601)
