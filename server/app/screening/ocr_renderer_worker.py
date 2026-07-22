import json
import io
import math
import socket
import sys
from dataclasses import dataclass


def _network_disabled(*_args, **_kwargs):
    raise PermissionError("network disabled")


socket.socket = _network_disabled


@dataclass(frozen=True)
class _Limits:
    max_source_bytes: int
    max_pages: int
    max_page_pixels: int
    max_total_pixels: int
    max_total_bytes: int
    dpi: int

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in self.__dict__.values()
        ) or self.dpi > 600:
            raise ValueError("invalid limits")


class _WorkerError(Exception):
    def __init__(self, safe_code: str) -> None:
        self.safe_code = safe_code


def _render(source: bytes, limits: _Limits) -> tuple[list[dict[str, int]], bytes]:
    if len(source) > limits.max_source_bytes:
        raise _WorkerError("file_too_large")
    if not source.startswith(b"%PDF-"):
        raise _WorkerError("file_magic_mismatch")

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(source), strict=True)
        if reader.is_encrypted:
            raise _WorkerError("pdf_encrypted")
        page_count = len(reader.pages)
    except _WorkerError:
        raise
    except Exception:
        raise _WorkerError("pdf_malformed") from None
    if page_count <= 0:
        raise _WorkerError("pdf_malformed")
    if page_count > limits.max_pages:
        raise _WorkerError("pdf_page_limit")

    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(source)
    except Exception:
        raise _WorkerError("pdf_malformed") from None
    try:
        if len(document) != page_count:
            raise _WorkerError("pdf_malformed")

        scale = limits.dpi / 72.0
        dimensions: list[tuple[int, int]] = []
        total_pixels = 0
        for page_number in range(page_count):
            page = document[page_number]
            try:
                page_width, page_height = page.get_size()
            finally:
                page.close()
            if not all(math.isfinite(value) and value > 0 for value in (page_width, page_height)):
                raise _WorkerError("pdf_malformed")
            width = math.ceil(page_width * scale)
            height = math.ceil(page_height * scale)
            pixels = width * height
            if pixels > limits.max_page_pixels:
                raise _WorkerError("pdf_pixel_limit")
            total_pixels += pixels
            if total_pixels > limits.max_total_pixels:
                raise _WorkerError("pdf_total_pixel_limit")
            dimensions.append((width, height))

        images: list[bytes] = []
        metadata: list[dict[str, int]] = []
        total_bytes = 0
        for index, (expected_width, expected_height) in enumerate(dimensions):
            page = document[index]
            try:
                bitmap = page.render(scale=scale)
                try:
                    rendered = bitmap.to_pil()
                    stream = io.BytesIO()
                    rendered.save(stream, format="PNG")
                    image = stream.getvalue()
                    width, height = rendered.size
                finally:
                    bitmap.close()
            except Exception:
                raise _WorkerError("pdf_render_failed") from None
            finally:
                page.close()
            if width <= 0 or height <= 0 or width > expected_width + 1 or height > expected_height + 1:
                raise _WorkerError("pdf_render_failed")
            total_bytes += len(image)
            if total_bytes > limits.max_total_bytes:
                raise _WorkerError("pdf_render_byte_limit")
            images.append(image)
            metadata.append({
                "page_number": index + 1,
                "width": width,
                "height": height,
                "length": len(image),
            })
        return metadata, b"".join(images)
    finally:
        document.close()


def main() -> None:
    payload = b""
    try:
        header = json.loads(sys.stdin.buffer.readline(8192))
        limits = _Limits(**header["limits"])
        source = sys.stdin.buffer.read(limits.max_source_bytes + 1)
        pages, payload = _render(source, limits)
        response = {"ok": True, "pages": pages}
    except _WorkerError as error:
        response = {"ok": False, "safe_code": error.safe_code}
    except Exception:
        response = {"ok": False, "safe_code": "pdf_render_failed"}
    sys.stdout.buffer.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n" + payload)


if __name__ == "__main__":
    main()
