"""Local OCR with Tesseract — used offline, in dev, or when Textract is unavailable."""
from __future__ import annotations

import io
from collections import defaultdict

from app.core.schemas import BoundingBox, OCRLine, OCRResult
from app.ocr.base import OCRError, OCRProvider


def _pdf_to_images(content: bytes, dpi: int = 300):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(content)
    scale = dpi / 72
    return [page.render(scale=scale).to_pil() for page in pdf]


def _preprocess(img):
    """Light-weight clean-up that measurably helps Tesseract on faxed/scanned forms."""
    from PIL import ImageOps

    img = ImageOps.exif_transpose(img).convert("L")
    img = ImageOps.autocontrast(img)
    return img


class TesseractProvider(OCRProvider):
    name = "tesseract"

    def __init__(self, lang: str = "eng", psm: int = 6):
        self.lang = lang
        self.config = f"--oem 1 --psm {psm}"

    def extract(self, content: bytes, mime_type: str) -> OCRResult:
        try:
            import pytesseract
            from PIL import Image
        except ImportError as exc:  # pragma: no cover
            raise OCRError("pytesseract / Pillow not installed") from exc

        if mime_type == "application/pdf":
            images = _pdf_to_images(content)
        elif mime_type.startswith("image/"):
            img = Image.open(io.BytesIO(content))
            images = []
            for i in range(getattr(img, "n_frames", 1)):  # multi-page TIFF
                img.seek(i)
                images.append(img.copy())
        else:
            raise OCRError(f"Tesseract cannot process {mime_type}")

        lines: list[OCRLine] = []
        for page_no, img in enumerate(images, start=1):
            img = _preprocess(img)
            w, h = img.size
            data = pytesseract.image_to_data(
                img, lang=self.lang, config=self.config, output_type=pytesseract.Output.DICT
            )
            grouped: dict[tuple, list[int]] = defaultdict(list)
            for i, word in enumerate(data["text"]):
                if word.strip() and float(data["conf"][i]) >= 0:
                    key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
                    grouped[key].append(i)
            for idxs in grouped.values():
                words = [data["text"][i] for i in idxs]
                confs = [float(data["conf"][i]) / 100 for i in idxs]
                left = min(data["left"][i] for i in idxs)
                top = min(data["top"][i] for i in idxs)
                right = max(data["left"][i] + data["width"][i] for i in idxs)
                bottom = max(data["top"][i] + data["height"][i] for i in idxs)
                lines.append(
                    OCRLine(
                        text=" ".join(words),
                        confidence=round(sum(confs) / len(confs), 4),
                        page=page_no,
                        bbox=BoundingBox(left=left / w, top=top / h, width=(right - left) / w, height=(bottom - top) / h),
                    )
                )
        return OCRResult(provider=self.name, page_count=len(images), lines=lines)
