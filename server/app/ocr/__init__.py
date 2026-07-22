"""Organization-scoped OCR provider configuration and gateway."""

from server.app.ocr.gateway import GatewayError, OcrGateway, OcrGatewayError

__all__ = ["GatewayError", "OcrGateway", "OcrGatewayError"]
