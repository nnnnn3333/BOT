"""Мелкие чистые функции: теги, FTS-запросы, время, имена файлов."""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TAG_RE = re.compile(r"(?<!\S)#([\w-]+)")  # '#тег' в начале слова (не якорь в URL)
WORD_RE = re.compile(r"\w+")
_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")
_BAD_FILENAME_CHARS = re.compile(r"[^\w.\- ()]+")


def esc(value: object) -> str:
    """Экранирование для parse_mode=HTML."""
    return html.escape(str(value), quote=False)


# ---------------------------------------------------------------- текст / теги
def parse_tags(text: str) -> list[str]:
    tags: list[str] = []
    for match in TAG_RE.finditer(text or ""):
        tag = match.group(1).strip("-_").lower()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def make_title(text: str, fallback: str = "Без названия", limit: int = 60) -> str:
    """Первая непустая строка без #тегов, обрезанная до limit символов."""
    cleaned = TAG_RE.sub("", text or "")
    for line in cleaned.splitlines():
        line = " ".join(line.split())
        if line:
            return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
    return fallback


def _light_stem(word: str) -> str:
    """Грубое отсечение русских окончаний (без внешних библиотек).

    «договорами» -> «договор», «отчёта» -> «отчё»: после этого префиксный поиск
    находит любую словоформу. Латиница и цифры не трогаются.
    """
    if not _CYRILLIC_RE.search(word):
        return word
    length = len(word)
    cut = 3 if length >= 9 else 2 if length >= 6 else 1 if length == 5 else 0
    return word[: length - cut]


def build_fts_query(raw: str, max_terms: int = 12) -> str:
    """Превращает пользовательский запрос в безопасное выражение FTS5.

    Каждое слово берётся в кавычки (спецсимволы FTS не сработают), для русских слов
    отсекается окончание, затем добавляется префиксный '*'. Слова связываются через AND.
    """
    words = [w for w in WORD_RE.findall(raw or "") if any(ch.isalnum() for ch in w)]
    return " ".join(f'"{_light_stem(w)}"*' for w in words[:max_terms])


def fmt_duration(seconds: float) -> str:
    total = max(int(seconds), 0)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


# ---------------------------------------------------------------- время
def parse_db_dt(value: str) -> datetime:
    """'YYYY-MM-DD HH:MM:SS' (UTC из SQLite) -> aware datetime."""
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def to_local(value: str | None, tz: ZoneInfo) -> datetime | None:
    return parse_db_dt(value).astimezone(tz) if value else None


# ---------------------------------------------------------------- файловое хранилище
def month_dir(root: Path, kind: str, tz: ZoneInfo) -> Path:
    """storage/<kind>/YYYY-MM/ — создаёт при необходимости."""
    folder = root / kind / datetime.now(tz).strftime("%Y-%m")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def safe_filename(name: str | None, default: str = "file") -> str:
    base = Path(name or "").name
    stem, ext = Path(base).stem, Path(base).suffix.lower()
    stem = _BAD_FILENAME_CHARS.sub("_", stem).strip(" ._") or default
    return f"{stem[:100]}{ext[:10]}"


def unique_path(folder: Path, filename: str, tz: ZoneInfo) -> Path:
    """Добавляет префикс ДД_ЧЧММСС_, чтобы одноимённые файлы не перетирали друг друга."""
    prefix = datetime.now(tz).strftime("%d_%H%M%S")
    candidate = folder / f"{prefix}_{filename}"
    counter = 1
    while candidate.exists():
        candidate = folder / f"{prefix}_{counter}_{filename}"
        counter += 1
    return candidate


def resolve_storage_path(storage_dir: Path, relative: str | None) -> Path | None:
    """Путь из БД -> реальный файл. None, если файла нет или путь выходит за storage/."""
    if not relative:
        return None
    root = storage_dir.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path
