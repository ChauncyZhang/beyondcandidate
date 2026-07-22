import json
import os
import subprocess
import sys


class StructuredPdfError(Exception):
    def __init__(self, safe_code: str) -> None:
        self.safe_code = safe_code
        super().__init__(safe_code)


def extract_structured_pdf(
    data: bytes,
    *,
    max_source_bytes: int,
    max_text_chars: int,
    max_pages: int,
    timeout_seconds: float = 5.0,
    worker_module: str = "server.app.screening.pdfplumber_worker",
) -> str:
    """Run the layout parser in a disposable child so timeout still permits fallback."""
    if len(data) > max_source_bytes:
        raise StructuredPdfError("source_limit")
    header = json.dumps(
        {
            "max_source_bytes": max_source_bytes,
            "max_text_chars": max_text_chars,
            "max_pages": max_pages,
        },
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    environment = {
        key: os.environ[key]
        for key in ("PATH", "PYTHONPATH", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME")
        if key in os.environ
    }
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
    try:
        completed = subprocess.run(
            [sys.executable, "-m", worker_module],
            input=header + data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise StructuredPdfError("timeout") from error
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise StructuredPdfError("worker_failed") from error

    if completed.returncode != 0 or len(completed.stdout) > max_text_chars * 4 + 4096:
        raise StructuredPdfError("worker_failed")
    try:
        result = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StructuredPdfError("invalid_output") from error
    if not result.get("ok"):
        safe_code = result.get("safe_code")
        if safe_code not in {"pdf_encrypted", "pdf_page_limit"}:
            safe_code = "extraction_failed"
        raise StructuredPdfError(safe_code)
    text = result.get("text")
    if not isinstance(text, str) or len(text) > max_text_chars:
        raise StructuredPdfError("invalid_output")
    return text
