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


def _render_pdf(source: bytes, limits: _Limits) -> tuple[list[dict[str, int]], bytes]:
    if len(source) > limits.max_source_bytes:
        raise _WorkerError("file_too_large")
    if not source.startswith(b"%PDF-"):
        raise _WorkerError("file_magic_mismatch")

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(source), strict=True)
        if reader.is_encrypted:
            raise _WorkerError("pdf_encrypted")
    except _WorkerError:
        raise
    except Exception:
        # PDFium is the rendering authority. It can safely render some exported
        # image PDFs whose cross-reference metadata pypdf cannot parse.
        pass

    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(source)
    except Exception:
        raise _WorkerError("pdf_malformed") from None
    try:
        page_count = len(document)
        if page_count <= 0:
            raise _WorkerError("pdf_malformed")
        if page_count > limits.max_pages:
            raise _WorkerError("pdf_page_limit")

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


def _render_image(source: bytes, limits: _Limits) -> tuple[list[dict[str, int | str]], bytes]:
    if len(source) > limits.max_source_bytes:
        raise _WorkerError("file_too_large")
    try:
        from PIL import Image

        with Image.open(io.BytesIO(source)) as opened:
            if opened.format not in {"JPEG", "PNG"}:
                raise _WorkerError("file_type_not_allowed")
            width, height = opened.size
            if width <= 0 or height <= 0:
                raise _WorkerError("image_malformed")
            if width * height > limits.max_page_pixels:
                raise _WorkerError("image_pixel_limit")
            opened.load()
            media_type = "image/jpeg" if opened.format == "JPEG" else "image/png"
    except _WorkerError:
        raise
    except Exception:
        raise _WorkerError("image_malformed") from None
    if len(source) > limits.max_total_bytes:
        raise _WorkerError("image_render_byte_limit")
    return [{"page_number": 1, "width": width, "height": height, "length": len(source), "media_type": media_type}], source


def main() -> None:
    payload = b""
    try:
        header = json.loads(sys.stdin.buffer.readline(8192))
        limits = _Limits(**header["limits"])
        source_type = header.get("source_type", "pdf")
        source = sys.stdin.buffer.read(limits.max_source_bytes + 1)
        if source_type == "pdf":
            pages, payload = _render_pdf(source, limits)
        elif source_type == "image":
            pages, payload = _render_image(source, limits)
        else:
            raise _WorkerError("ocr_source_type_invalid")
        response = {"ok": True, "pages": pages}
    except _WorkerError as error:
        response = {"ok": False, "safe_code": error.safe_code}
    except Exception:
        response = {"ok": False, "safe_code": "pdf_render_failed"}
    sys.stdout.buffer.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n" + payload)


if __name__ == "__main__":
    main()
