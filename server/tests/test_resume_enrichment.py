import io
import asyncio
import uuid
from types import SimpleNamespace

from server.app.ocr.gateway import OcrGatewayError
from server.app.screening.ocr_rendering import RenderedOcrPage
from server.app.screening.resume_enrichment import ResumeTextEnhancer


class Database:
    def __init__(self, config):
        self.config = config

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def scalar(self, _statement):
        return self.config


class Storage:
    def __init__(self):
        self.opens = 0

    async def open(self, _key, _limit):
        self.opens += 1
        return io.BytesIO(b"%PDF-1.7\nsource")


class Renderer:
    async def render_pdf(self, _stream, *, limits):
        assert limits.max_pages == 20
        return (RenderedOcrPage(1, b"\x89PNG\r\n\x1a\npixels", 1, 1),)


class Cipher:
    def decrypt(self, _value):
        return "secret"


def settings():
    return SimpleNamespace(
        parser_hard_timeout_seconds=30,
        parser_max_source_bytes=10 * 1024 * 1024,
        parser_pdf_max_pages=50,
    )


def config():
    return SimpleNamespace(
        enabled=True,
        encrypted_api_key=b"encrypted",
        provider_id="ocr",
        base_url="https://ocr.example.test/v1",
        model="vision",
    )


def test_good_native_text_never_calls_ocr():
    storage = Storage()
    enhancer = ResumeTextEnhancer(lambda: Database(config()), storage, object(), Cipher(), settings(), Renderer())

    result = asyncio.run(enhancer.enhance(
        uuid.uuid4(),
        storage_key="clean/resume",
        filename="resume.pdf",
        mime_type="application/pdf",
        native_text="Python 工程师，负责企业级平台研发。",
    ))

    assert result.assessment.quality == "good"
    assert result.used_ocr is False
    assert storage.opens == 0


def test_poor_pdf_uses_better_ocr_text():
    class Gateway:
        async def extract_images(self, provider_id, base_url, model, api_key, images):
            assert (provider_id, base_url, model, api_key) == (
                "ocr", "https://ocr.example.test/v1", "vision", "secret"
            )
            assert len(images) == 1
            return ["个人简介\n十年企业财务管理经验\n工作经历\n负责预算与税务筹划"]

    storage = Storage()
    enhancer = ResumeTextEnhancer(lambda: Database(config()), storage, Gateway(), Cipher(), settings(), Renderer())
    result = asyncio.run(enhancer.enhance(
        uuid.uuid4(),
        storage_key="clean/resume",
        filename="resume.pdf",
        mime_type="application/pdf",
        native_text="2024年09月2022年09月2020年09月",
    ))

    assert result.used_ocr is True
    assert result.assessment.quality == "good"
    assert "财务管理" in result.text
    assert storage.opens == 1


def test_ocr_failure_preserves_native_text_and_safe_code():
    class Gateway:
        async def extract_images(self, *_args):
            raise OcrGatewayError("provider_unavailable")

    native = "2024年09月2022年09月2020年09月"
    enhancer = ResumeTextEnhancer(lambda: Database(config()), Storage(), Gateway(), Cipher(), settings(), Renderer())
    result = asyncio.run(enhancer.enhance(
        uuid.uuid4(),
        storage_key="clean/resume",
        filename="resume.pdf",
        mime_type="application/pdf",
        native_text=native,
    ))

    assert result.text == native
    assert result.used_ocr is False
    assert result.safe_error_code == "provider_unavailable"
