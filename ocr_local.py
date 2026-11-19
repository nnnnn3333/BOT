"""Локальное OCR скриншотов: данные не уходят в облачные OCR/ИИ API."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def _recognize(path: Path) -> str:
    import pytesseract
    from PIL import Image, ImageOps, ImageEnhance
    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        image.thumbnail((3000, 3000))
        image = ImageEnhance.Contrast(image).enhance(1.3)
        text = pytesseract.image_to_string(image, lang="rus+eng", config="--psm 3")
    return " ".join(text.split())[:100_000]


async def recognize(path: Path) -> tuple[str, str]:
    try:
        text = await asyncio.to_thread(_recognize, path)
    except Exception as exc:
        log.warning("OCR failed for %s: %s", path, exc)
        return "", "OCR не запущен (установите tesseract-ocr-rus). Скрин сохранён."
    if not text:
        return "", "Текст на изображении не распознан. Скрин сохранён."
    return text, f"OCR: распознано {len(text)} символов. Проверьте даты и цены вручную."
