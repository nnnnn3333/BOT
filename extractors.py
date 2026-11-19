"""Локальное извлечение текста из документов (без ИИ и внешних API).

PDF  — pypdf (только текстовый слой; сканы без OCR дадут пустую строку)
DOCX — python-docx (абзацы + таблицы)
XLSX — openpyxl (все листы, значения ячеек)
TXT  — подбор кодировки utf-8 / cp1251
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from docx import Document as DocxDocument
from openpyxl import load_workbook
from pypdf import PdfReader

log = logging.getLogger(__name__)

DOC_EXTENSIONS = frozenset({".pdf", ".docx", ".xlsx", ".txt"})
_MAX_XLSX_ROWS = 50_000  # защита от гигантских выгрузок

_SPACES_RE = re.compile(r"[ \t ]+")
_BLANKS_RE = re.compile(r"\n{3,}")


def _normalize(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = _SPACES_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS_RE.sub("\n\n", text).strip()


def _read_pdf(path: Path) -> str:
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        reader.decrypt("")  # пустой пароль; при неудаче исключение уйдёт наверх
    parts: list[str] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # битая страница не должна ронять весь документ
            log.warning("%s: не удалось прочитать страницу %d", path.name, number)
    return "\n\n".join(parts)


def _read_docx(path: Path) -> str:
    doc = DocxDocument(str(path))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            texts: list[str] = []
            for cell in row.cells:
                value = cell.text.strip()
                # объединённые ячейки python-docx повторяет — убираем дубли подряд
                if value and (not texts or texts[-1] != value):
                    texts.append(value)
            if texts:
                parts.append(" | ".join(texts))
    return "\n".join(parts)


def _cell_to_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime):
        has_time = bool(value.hour or value.minute or value.second)
        return value.strftime("%Y-%m-%d %H:%M" if has_time else "%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _read_xlsx(path: Path) -> str:
    workbook = load_workbook(str(path), read_only=True, data_only=True)
    try:
        parts: list[str] = []
        for sheet in workbook.worksheets:
            parts.append(f"[{sheet.title}]")
            for index, row in enumerate(sheet.iter_rows(values_only=True)):
                if index >= _MAX_XLSX_ROWS:
                    break
                line = " | ".join(s for s in (_cell_to_str(c) for c in row) if s)
                if line:
                    parts.append(line)
        return "\n".join(parts)
    finally:
        workbook.close()


def _read_txt(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


_EXTRACTORS: dict[str, Callable[[Path], str]] = {
    ".pdf": _read_pdf,
    ".docx": _read_docx,
    ".xlsx": _read_xlsx,
    ".txt": _read_txt,
}


def extract_text(path: Path, max_chars: int = 1_000_000) -> str:
    """Возвращает нормализованный текст файла или '' (формат неизвестен / ошибка / скан)."""
    extractor = _EXTRACTORS.get(path.suffix.lower())
    if extractor is None:
        return ""
    try:
        text = extractor(path)
    except Exception:
        log.exception("Не удалось извлечь текст из %s", path.name)
        return ""
    return _normalize(text)[:max_chars]
