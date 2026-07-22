import io
import re
import socket
import subprocess
from types import SimpleNamespace

import pytest

from server.app.screening.parsers import ParserLimits, parse_document
from server.app.screening.pdfplumber_worker import _disable_network, extract_layout_text
from server.app.screening.structured_pdf import StructuredPdfError, extract_structured_pdf


def _pdf_bytes() -> bytes:
    from pypdf import PdfWriter

    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.write(stream)
    return stream.getvalue()


def test_worker_extracts_layout_text_without_images_or_ocr(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class Reader:
        is_encrypted = False
        pages = [object()]

    class Page:
        def extract_text(self, **kwargs):
            calls.append(kwargs)
            return "# Profile\nPython  "

    class Document:
        pages = [Page()]

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr("pypdf.PdfReader", lambda *_args, **_kwargs: Reader())
    monkeypatch.setitem(
        __import__("sys").modules,
        "pdfplumber",
        SimpleNamespace(open=lambda *_args, **_kwargs: Document()),
    )

    assert extract_layout_text(b"%PDF-test", max_pages=2, max_text_chars=100) == "# Profile\nPython"
    assert calls == [{"layout": True, "x_tolerance": 2, "y_tolerance": 3}]


def test_worker_disables_network_in_its_own_process() -> None:
    original_socket = socket.socket
    try:
        _disable_network()
        with pytest.raises(PermissionError, match="network disabled"):
            socket.socket()
    finally:
        socket.socket = original_socket


def test_real_library_subprocess_handles_layout_and_clean_json_protocol() -> None:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    content = DecodedStreamObject()
    content.set_data(b"\n".join([
        b"BT /F1 18 Tf 40 755 Td (CANDIDATE PROFILE) Tj ET",
        b"BT /F1 14 Tf 40 710 Td (EXPERIENCE) Tj ET",
        b"BT /F1 11 Tf 40 685 Td (Platform Engineer) Tj ET",
        b"BT /F1 14 Tf 330 710 Td (EDUCATION) Tj ET",
        b"BT /F1 11 Tf 330 685 Td (Example University) Tj ET",
        b"BT /F1 10 Tf 40 590 Td (YEAR) Tj ET",
        b"BT /F1 10 Tf 190 590 Td (COMPANY) Tj ET",
        b"BT /F1 10 Tf 360 590 Td (ROLE) Tj ET",
        b"BT /F1 10 Tf 40 558 Td (2021-2025) Tj ET",
        b"BT /F1 10 Tf 190 558 Td (Example Tech) Tj ET",
        b"BT /F1 10 Tf 360 558 Td (AI Engineer) Tj ET",
    ]))
    page[NameObject("/Contents")] = writer._add_object(content)
    stream = io.BytesIO()
    writer.write(stream)
    source = stream.getvalue()

    markdown = extract_structured_pdf(
        source,
        max_source_bytes=1024 * 1024,
        max_text_chars=20_000,
        max_pages=1,
        timeout_seconds=15,
    )

    normalized = re.sub(r"[ \t]+", " ", markdown)
    assert normalized.strip()
    assert normalized.index("CANDIDATE PROFILE") < normalized.index("EXPERIENCE")
    assert "Platform Engineer" in normalized
    assert "EDUCATION" in normalized and "Example University" in normalized
    assert "2021-2025" in normalized and "Example Tech" in normalized and "AI Engineer" in normalized


def test_pdf_prefers_layout_markdown_with_columns_and_table(monkeypatch) -> None:
    markdown = """# 候选人资料

## 工作经历
左栏：平台研发

## 教育经历
右栏：某大学本科

| 时间 | 公司 | 职位 |
| --- | --- | --- |
| 2021-2025 | 示例科技 | AI 工程师 |
"""
    monkeypatch.setattr("server.app.screening.parsers.extract_structured_pdf", lambda *_args, **_kwargs: markdown)

    parsed = parse_document(io.BytesIO(_pdf_bytes()), extension=".pdf", mime_type="application/pdf")

    assert parsed.parser_version == "pdf-pdfplumber-v1"
    assert parsed.text.index("工作经历") < parsed.text.index("教育经历")
    assert "| 2021-2025 | 示例科技 | AI 工程师 |" in parsed.text


@pytest.mark.parametrize("structured_result", ["", "  \n"])
def test_pdf_falls_back_when_layout_result_is_empty(monkeypatch, structured_result: str) -> None:
    monkeypatch.setattr(
        "server.app.screening.parsers.extract_structured_pdf",
        lambda *_args, **_kwargs: structured_result,
    )

    parsed = parse_document(io.BytesIO(_pdf_bytes()), extension=".pdf", mime_type="application/pdf")

    assert parsed.parser_version == "pdf-v4"
    assert parsed.quality == "empty"


@pytest.mark.parametrize("safe_code", ["worker_failed", "timeout"])
def test_pdf_falls_back_when_layout_parser_fails(monkeypatch, safe_code: str) -> None:
    def fail(*_args, **_kwargs):
        raise StructuredPdfError(safe_code)

    monkeypatch.setattr("server.app.screening.parsers.extract_structured_pdf", fail)
    parsed = parse_document(io.BytesIO(_pdf_bytes()), extension=".pdf", mime_type="application/pdf")
    assert parsed.parser_version == "pdf-v4"


def test_pypdf_generic_failure_does_not_block_structured_success(monkeypatch) -> None:
    class BrokenReader:
        def __init__(self, *_args, **_kwargs) -> None:
            raise ValueError("strict parser rejected an otherwise readable PDF")

    monkeypatch.setattr("pypdf.PdfReader", BrokenReader)
    monkeypatch.setattr(
        "server.app.screening.parsers.extract_structured_pdf",
        lambda *_args, **_kwargs: "# Candidate\n\nStructured profile",
    )

    parsed = parse_document(io.BytesIO(b"%PDF-readable"), extension=".pdf", mime_type="application/pdf")

    assert parsed.parser_version == "pdf-pdfplumber-v1"
    assert "Structured profile" in parsed.text


def test_structured_parser_timeout_is_typed_and_bounded() -> None:
    with pytest.raises(StructuredPdfError) as raised:
        extract_structured_pdf(
            b"%PDF-private",
            max_source_bytes=1024,
            max_text_chars=1024,
            max_pages=1,
            timeout_seconds=0.1,
            worker_module="server.tests.parser_hang_worker",
        )
    assert str(raised.value) == "timeout"


def test_structured_parser_rejects_oversized_worker_output(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b"x" * 9000),
    )
    with pytest.raises(StructuredPdfError) as raised:
        extract_structured_pdf(
            b"%PDF-test",
            max_source_bytes=1024,
            max_text_chars=1000,
            max_pages=1,
        )
    assert str(raised.value) == "worker_failed"
