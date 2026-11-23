"""OCR routing: text layer first, then the configured OCR engine, with fallback."""
from __future__ import annotations

import logging

from app.core.config import Settings
from app.core.schemas import OCRResult
from app.ocr.base import OCRError, OCRProvider
from app.ocr.text_layer import TextLayerProvider

log = logging.getLogger(__name__)


class OCRRouter:
    def __init__(self, engines: list[OCRProvider]):
        self.text_layer = TextLayerProvider()
        self.engines = engines

    def extract(self, content: bytes, mime_type: str) -> OCRResult:
        try:
            return self.text_layer.extract(content, mime_type)
        except OCRError as exc:
            log.info("text layer unavailable, using OCR engine: %s", exc)

        errors = []
        for engine in self.engines:
            try:
                return engine.extract(content, mime_type)
            except OCRError as exc:
                log.warning("OCR engine %s failed: %s", engine.name, exc)
                errors.append(f"{engine.name}: {exc}")
        raise OCRError("; ".join(errors) or "no OCR engine configured")


def build_ocr_router(settings: Settings) -> OCRRouter:
    engines: list[OCRProvider] = []
    if settings.ocr_provider in ("textract", "auto"):
        try:
            from app.ocr.textract import TextractProvider

            if settings.ocr_provider == "textract" or settings.environment != "local":
                engines.append(TextractProvider(region=settings.aws_region))
        except Exception as exc:  # pragma: no cover
            log.warning("Textract unavailable: %s", exc)
    if settings.ocr_provider in ("tesseract", "auto"):
        from app.ocr.tesseract import TesseractProvider

        engines.append(TesseractProvider())
    return OCRRouter(engines)
