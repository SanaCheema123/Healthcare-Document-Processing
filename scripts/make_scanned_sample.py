"""Render a text sample as a slightly rotated, noisy 'scanned' PNG for OCR testing.

    python scripts/make_scanned_sample.py samples/claim_cms1500.txt samples/claim_scanned.png
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


def _font(size: int):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "/System/Library/Fonts/Menlo.ttc",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def render(text: str, out: Path, seed: int = 7) -> None:
    random.seed(seed)
    font = _font(30)
    lines = text.splitlines()
    w, line_h = 1900, 46
    img = Image.new("L", (w, 140 + line_h * len(lines)), 255)
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((80, 70 + i * line_h), line, fill=random.randint(0, 40), font=font)
    # scanner artefacts: speckle noise, slight blur and skew
    px = img.load()
    for _ in range(img.width * img.height // 400):
        px[random.randrange(img.width), random.randrange(img.height)] = random.randint(120, 200)
    img = img.filter(ImageFilter.GaussianBlur(0.6)).rotate(0.6, expand=True, fillcolor=255)
    img.save(out, dpi=(300, 300))


if __name__ == "__main__":
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    render(src.read_text(), dst)
    print(f"wrote {dst}")
