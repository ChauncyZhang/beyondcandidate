import uuid
from dataclasses import dataclass
from pathlib import PurePath

from sqlalchemy import select

from server.app.ocr.gateway import OcrGatewayError
from server.app.ocr.models import OcrProviderConfig
from server.app.screening.document_quality import TextQualityAssessment, assess_text_quality
from server.app.screening.ocr_rendering import (
    IsolatedOcrRenderer,
    OcrRenderingError,
    OcrRenderLimits,
)


@dataclass(frozen=True)
class EnrichedResumeText:
    text: str
    assessment: TextQualityAssessment
    used_ocr: bool
    safe_error_code: str | None = None


class ResumeTextEnhancer:
    """Use OCR only when native PDF extraction is not reliable enough."""

    def __init__(self, sessions, storage, gateway, cipher, settings, renderer=None):
        self.sessions = sessions
        self.storage = storage
        self.gateway = gateway
        self.cipher = cipher
        self.settings = settings
        self.renderer = renderer or IsolatedOcrRenderer(
            timeout_seconds=max(10, settings.parser_hard_timeout_seconds)
        )

    @staticmethod
    def _prefer_ocr(native: TextQualityAssessment, ocr: TextQualityAssessment) -> bool:
        rank = {"empty": 0, "poor": 1, "good": 2}
        if rank[ocr.quality] != rank[native.quality]:
            return rank[ocr.quality] > rank[native.quality]
        return (
            ocr.quality == "poor"
            and int(ocr.metrics["visible_char_count"]) > int(native.metrics["visible_char_count"]) * 1.2
        )

    async def enhance(
        self,
        organization_id: uuid.UUID,
        *,
        storage_key: str,
        filename: str,
        mime_type: str,
        native_text: str,
    ) -> EnrichedResumeText:
        native = assess_text_quality(native_text)
        is_pdf = mime_type == "application/pdf" or PurePath(filename).suffix.casefold() == ".pdf"
        if native.quality == "good" or not is_pdf:
            return EnrichedResumeText(native_text, native, False)

        with self.sessions() as database:
            config = database.scalar(
                select(OcrProviderConfig).where(OcrProviderConfig.organization_id == organization_id)
            )
            if config is None or not config.enabled or config.encrypted_api_key is None:
                return EnrichedResumeText(native_text, native, False, "ocr_config_disabled")
            provider_id, base_url, model = config.provider_id, config.base_url, config.model
            try:
                api_key = self.cipher.decrypt(config.encrypted_api_key)
            except ValueError:
                return EnrichedResumeText(native_text, native, False, "ocr_key_unavailable")

        stream = None
        try:
            source_limit = min(self.settings.parser_max_source_bytes, 10 * 1024 * 1024)
            stream = await self.storage.open(storage_key, source_limit)
            pages = await self.renderer.render_pdf(
                stream,
                limits=OcrRenderLimits(
                    max_source_bytes=source_limit,
                    max_pages=min(self.settings.parser_pdf_max_pages, 20),
                ),
            )
            page_text = await self.gateway.extract_images(
                provider_id,
                base_url,
                model,
                api_key,
                [page.image_bytes for page in pages],
            )
            ocr_text = "\n\n".join(value.strip() for value in page_text if value.strip())
            ocr = assess_text_quality(ocr_text)
            if self._prefer_ocr(native, ocr):
                return EnrichedResumeText(ocr_text, ocr, True)
            return EnrichedResumeText(native_text, native, False, "ocr_quality_not_improved")
        except OcrRenderingError as error:
            return EnrichedResumeText(native_text, native, False, error.safe_code)
        except OcrGatewayError as error:
            return EnrichedResumeText(native_text, native, False, error.safe_code)
        except Exception:
            return EnrichedResumeText(native_text, native, False, "ocr_unavailable")
        finally:
            if stream is not None:
                stream.close()
