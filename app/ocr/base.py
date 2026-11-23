from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.schemas import OCRResult


class OCRError(RuntimeError):
    pass


class OCRProvider(ABC):
    name: str

    @abstractmethod
    def extract(self, content: bytes, mime_type: str) -> OCRResult:
        """Return text lines (with confidences) for the document."""


def detect_mime(content: bytes, filename: str = "") -> str:
    if content[:4] == b"%PDF":
        return "application/pdf"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if content[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if content[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if filename.lower().endswith(".txt"):
        return "text/plain"
    try:
        text = content[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    if any(ord(ch) < 32 and ch not in "\t\n\r\f" for ch in text):
        return "application/octet-stream"
    return "text/plain"
