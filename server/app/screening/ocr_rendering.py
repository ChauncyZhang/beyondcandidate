import asyncio
import json
import math
import os
import sys
from dataclasses import asdict, dataclass
from typing import BinaryIO

from server.app.queue.service import normalize_safe_code


@dataclass(frozen=True)
class OcrRenderLimits:
    max_source_bytes: int = 10 * 1024 * 1024
    max_pages: int = 50
    max_page_pixels: int = 20_000_000
    max_total_pixels: int = 80_000_000
    max_total_bytes: int = 40 * 1024 * 1024
    dpi: int = 200

    def __post_init__(self) -> None:
        values = asdict(self)
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in values.values()):
            raise ValueError("OCR render limits must be positive integers")
        if self.dpi > 600:
            raise ValueError("OCR render dpi must not exceed 600")


@dataclass(frozen=True)
class RenderedOcrPage:
    page_number: int
    image_bytes: bytes
    width: int
    height: int
    media_type: str = "image/png"


class OcrRenderingError(Exception):
    def __init__(self, safe_code: str) -> None:
        self.safe_code = normalize_safe_code(safe_code)
        super().__init__(self.safe_code)


class IsolatedOcrRenderer:
    def __init__(
        self,
        *,
        timeout_seconds: float = 30,
        worker_module: str = "server.app.screening.ocr_renderer_worker",
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        self.timeout_seconds = timeout_seconds
        self.worker_module = worker_module
        self.last_pid: int | None = None

    async def render_pdf(
        self,
        stream: BinaryIO,
        *,
        limits: OcrRenderLimits = OcrRenderLimits(),
    ) -> tuple[RenderedOcrPage, ...]:
        stream.seek(0)
        source = stream.read(limits.max_source_bytes + 1)
        if len(source) > limits.max_source_bytes:
            raise OcrRenderingError("file_too_large")
        if not source.startswith(b"%PDF-"):
            raise OcrRenderingError("file_magic_mismatch")

        request = json.dumps({"limits": asdict(limits)}, separators=(",", ":")).encode("utf-8") + b"\n" + source
        environment = {
            key: os.environ[key]
            for key in ("PATH", "PYTHONPATH", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME")
            if key in os.environ
        }
        environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            self.worker_module,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=environment,
        )
        self.last_pid = process.pid
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(request), self.timeout_seconds)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise OcrRenderingError("pdf_render_timeout") from None
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise

        if process.returncode != 0 or len(stdout) > limits.max_total_bytes + 65_536:
            raise OcrRenderingError("pdf_render_failed")
        header_bytes, separator, payload = stdout.partition(b"\n")
        if not separator or len(header_bytes) > 65_536:
            raise OcrRenderingError("pdf_render_failed")
        try:
            header = json.loads(header_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OcrRenderingError("pdf_render_failed") from None
        if not isinstance(header, dict):
            raise OcrRenderingError("pdf_render_failed")
        if not header.get("ok"):
            raise OcrRenderingError(header.get("safe_code", "pdf_render_failed"))

        metadata = header.get("pages")
        if not isinstance(metadata, list) or len(metadata) > limits.max_pages:
            raise OcrRenderingError("pdf_render_failed")
        pages: list[RenderedOcrPage] = []
        offset = 0
        total_pixels = 0
        for expected_number, item in enumerate(metadata, start=1):
            if not isinstance(item, dict):
                raise OcrRenderingError("pdf_render_failed")
            try:
                number = item["page_number"]
                width = item["width"]
                height = item["height"]
                length = item["length"]
            except KeyError:
                raise OcrRenderingError("pdf_render_failed") from None
            if (
                not all(isinstance(value, int) and not isinstance(value, bool) for value in (number, width, height, length))
                or number != expected_number
                or width <= 0
                or height <= 0
                or length <= 0
                or width * height > limits.max_page_pixels
            ):
                raise OcrRenderingError("pdf_render_failed")
            total_pixels += width * height
            if total_pixels > limits.max_total_pixels or offset + length > len(payload):
                raise OcrRenderingError("pdf_render_failed")
            image = payload[offset : offset + length]
            if not image.startswith(b"\x89PNG\r\n\x1a\n"):
                raise OcrRenderingError("pdf_render_failed")
            pages.append(RenderedOcrPage(number, image, width, height))
            offset += length
        if offset != len(payload) or len(payload) > limits.max_total_bytes:
            raise OcrRenderingError("pdf_render_failed")
        return tuple(pages)
