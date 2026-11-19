"""Приём и индексация контента: текст, фото/скриншоты, документы."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from config import TG_DOWNLOAD_LIMIT, Settings
from database import Database
from extractors import DOC_EXTENSIONS, extract_text
from utils import esc, make_title, month_dir, parse_tags, safe_filename, unique_path

log = logging.getLogger(__name__)
router = Router(name="content")

TOO_BIG = "Файл больше 20 МБ — Bot API не позволяет боту его скачать."


def _tags_line(tags: list[str]) -> str:
    return "  " + " ".join(f"#{t}" for t in tags) if tags else ""


async def _download(bot: Bot, file_obj, dest: Path) -> bool:
    """Скачивает файл Telegram на диск. False — если Telegram отказал (напр. слишком большой)."""
    try:
        await bot.download(file_obj, destination=dest)
    except TelegramBadRequest as exc:
        log.warning("Не удалось скачать файл: %s", exc)
        dest.unlink(missing_ok=True)
        return False
    return True


# ------------------------------------------------------------------ текст
@router.message(F.text, ~F.text.startswith("/"))
async def on_text(message: Message, db: Database) -> None:
    text = message.text.strip()
    tags = parse_tags(text)
    category = "kb" if "kb" in tags else "note"

    item_id = await db.add_item(
        category=category,
        title=make_title(text, fallback="Заметка"),
        content=text,
        tags=" ".join(tags),
    )
    label = "база знаний" if category == "kb" else "заметка"
    await message.answer(f"✅ Сохранено #{item_id} ({label}){_tags_line(tags)}")


# ------------------------------------------------------------------ фото / скриншоты
async def _save_image(
    message: Message,
    bot: Bot,
    db: Database,
    settings: Settings,
    file_obj,
    filename: str,
) -> None:
    folder = month_dir(settings.storage_dir, "screens", settings.tz)
    dest = unique_path(folder, filename, settings.tz)
    if not await _download(bot, file_obj, dest):
        await message.answer(f"❌ {TOO_BIG}")
        return

    caption = (message.caption or "").strip()
    tags = parse_tags(caption)
    stamp = datetime.now(settings.tz).strftime("%Y-%m-%d %H:%M")
    item_id = await db.add_item(
        category="screen",
        title=make_title(caption, fallback=f"Скриншот {stamp}"),
        content=caption,
        file_path=dest.relative_to(settings.storage_dir).as_posix(),
        tags=" ".join(tags),
    )
    hint = "" if caption else "\n<i>Подписи нет — найдёте только по дате; добавляйте caption.</i>"
    await message.answer(f"🖼 Скриншот сохранён #{item_id}{_tags_line(tags)}{hint}")


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    photo = message.photo[-1]  # самое большое разрешение
    await _save_image(message, bot, db, settings, photo, f"{photo.file_unique_id}.jpg")


# ------------------------------------------------------------------ документы
@router.message(F.document)
async def on_document(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    doc = message.document
    original_name = doc.file_name or doc.file_unique_id
    ext = Path(original_name).suffix.lower()
    is_image = (doc.mime_type or "").startswith("image/")

    if ext not in DOC_EXTENSIONS and not is_image:
        allowed = " ".join(sorted(DOC_EXTENSIONS))
        await message.answer(f"⚠️ Формат {esc(ext or 'без расширения')} не поддерживается. Принимаю: {allowed} и изображения.")
        return
    if doc.file_size and doc.file_size > TG_DOWNLOAD_LIMIT:
        await message.answer(f"❌ {TOO_BIG}")
        return

    filename = safe_filename(original_name, default=doc.file_unique_id)

    # Картинка, присланная «файлом» (без сжатия) — это тоже скриншот.
    if is_image and ext not in DOC_EXTENSIONS:
        await _save_image(message, bot, db, settings, doc, filename)
        return

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    folder = month_dir(settings.storage_dir, "files", settings.tz)
    dest = unique_path(folder, filename, settings.tz)
    if not await _download(bot, doc, dest):
        await message.answer(f"❌ {TOO_BIG}")
        return

    # Парсинг PDF/DOCX/XLSX — блокирующий, уводим в поток, чтобы бот не «замирал».
    extracted = await asyncio.to_thread(extract_text, dest, settings.max_content_chars)

    caption = (message.caption or "").strip()
    tags = parse_tags(caption)
    content = f"{caption}\n\n{extracted}".strip() if caption else extracted
    item_id = await db.add_item(
        category="table" if ext == ".xlsx" else "file",
        title=make_title(caption, fallback=Path(original_name).name) if caption else Path(original_name).name,
        content=content,
        file_path=dest.relative_to(settings.storage_dir).as_posix(),
        tags=" ".join(tags),
    )

    if extracted:
        status = f"📄 Текст извлечён: {len(extracted):,} симв. — файл ищется по содержимому.".replace(",", " ")
    else:
        status = (
            "⚠️ Текст извлечь не удалось (скан без текстового слоя, пустой или защищённый файл). "
            "Файл сохранён, но искать его можно только по названию и тегам."
        )
    await message.answer(f"✅ Файл сохранён #{item_id}: {esc(Path(original_name).name)}{_tags_line(tags)}\n{status}")
