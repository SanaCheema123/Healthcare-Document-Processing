"""Zero-cost "OCR" for documents that already carry text.

Digital (born-electronic) PDFs and plain text files don't need OCR. Reading the
text layer directly is faster, cheaper and more accurate than Textract, so the
factory tries this first and only falls back to real OCR for scanned pages.
"""
from __future__ import annotations

import io

from app.core.schemas import OCRLine, OCRResult
from app.ocr.base import OCRError, OCRProvider

# A page with fewer characters than this is assumed to be a scan.
MIN_CHARS_PER_PAGE = 40


class TextLayerProvider(OCRProvider):
    name = "text_layer"

    def extract(self, content: bytes, mime_type: str) -> OCRResult:
        if mime_type == "text/plain":
            text = content.decode("utf-8", errors="replace")
            lines = [OCRLine(text=l, confidence=1.0) for l in text.splitlines() if l.strip()]
            return OCRResult(provider=self.name, page_count=1, lines=lines)

        if mime_type == "application/pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(content))
            lines: list[OCRLine] = []
            for page_no, page in enumerate(reader.pages, start=1):
                page_text = page.extract_text() or ""
                if len(page_text.strip()) < MIN_CHARS_PER_PAGE:
                    raise OCRError(f"page {page_no} has no usable text layer")
                lines.extend(
                    OCRLine(text=l, confidence=1.0, page=page_no)
                    for l in page_text.splitlines()
                    if l.strip()
                )
            return OCRResult(provider=self.name, page_count=len(reader.pages), lines=lines)

        raise OCRError(f"text layer not available for {mime_type}")
