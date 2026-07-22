import io
import json
import socket
import sys


def _network_disabled(*_args, **_kwargs):
    raise PermissionError("network disabled")


def _disable_network() -> None:
    socket.socket = _network_disabled


class PdfWorkerError(Exception):
    def __init__(self, safe_code: str) -> None:
        self.safe_code = safe_code
        super().__init__(safe_code)


def extract_layout_text(source: bytes, *, max_pages: int, max_text_chars: int) -> str:
    import pdfplumber
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(source), strict=True)
    if reader.is_encrypted:
        raise PdfWorkerError("pdf_encrypted")
    if len(reader.pages) > max_pages:
        raise PdfWorkerError("pdf_page_limit")

    page_text: list[str] = []
    with pdfplumber.open(io.BytesIO(source), unicode_norm="NFKC") as document:
        if len(document.pages) > max_pages:
            raise PdfWorkerError("pdf_page_limit")
        for page in document.pages:
            text = page.extract_text(layout=True, x_tolerance=2, y_tolerance=3) or ""
            page_text.append(text.rstrip())

    result = "\n\n".join(value for value in page_text if value.strip())
    if len(result) > max_text_chars:
        raise ValueError("invalid structured PDF output")
    return result


def main() -> None:
    _disable_network()
    try:
        header = json.loads(sys.stdin.buffer.readline(8192))
        max_source_bytes = int(header["max_source_bytes"])
        max_text_chars = int(header["max_text_chars"])
        max_pages = int(header["max_pages"])
        source = sys.stdin.buffer.read(max_source_bytes + 1)
        if len(source) > max_source_bytes:
            raise ValueError("source limit")
        text = extract_layout_text(source, max_pages=max_pages, max_text_chars=max_text_chars)
        output = {"ok": True, "text": text}
    except PdfWorkerError as error:
        output = {"ok": False, "safe_code": error.safe_code}
    except Exception:
        output = {"ok": False}
    sys.stdout.write(json.dumps(output, separators=(",", ":"), ensure_ascii=False))


if __name__ == "__main__":
    main()
