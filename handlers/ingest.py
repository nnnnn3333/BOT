"""Приём рабочих заметок, скриншотов и файлов с карточками и локальным OCR.

Заменяет только catch-all маршруты старого handlers/content.py, не меняя
таблицу items и расположение файлов. Запускается раньше старого content.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from config import Settings, TG_DOWNLOAD_LIMIT
from database import Database
from extractors import DOC_EXTENSIONS, extract_text
from ocr_local import recognize
from utils import esc, make_title, month_dir, parse_tags, safe_filename, unique_path
from handlers.workspace import card_keyboard, card_text
from handlers.ui import show_card
from handlers.navigation import mark_saved_input, preserve_messages
from workspace_db import mark_ocr_processed, get_item, update_item

log = logging.getLogger(__name__)
router = Router(name="ingest")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


_background = set()


async def stop_background():
    for task in list(_background):
        task.cancel()
    if _background:
        await asyncio.gather(*_background, return_exceptions=True)


async def _saved(message, item_id, db, settings, *, note=""):
    mark_saved_input()
    row = await get_item(db, item_id)
    text = card_text(row, settings)
    if note:
        text += "\n\n" + esc(note)
    await show_card(message, row, settings, card_keyboard(item_id, True), text)


async def _recognize_saved(message, db, settings, dest, item_id, original_title, caption):
    with preserve_messages():
        try:
            recognized, hint = await recognize(dest)
            row = await get_item(db, item_id)
            if row is None:
                return
            if recognized:
                content = "\n\n".join(part for part in (caption, recognized) if part)
                await db.conn.execute(
                    """UPDATE items SET content=?, title=CASE WHEN title=? AND ?='' THEN ? ELSE title END
                    WHERE id=? AND content=?""",
                    (content[:settings.max_content_chars], original_title, caption,
                     make_title(recognized, fallback=original_title), item_id, caption),
                )
                await db.conn.commit()
            if not hint.startswith("OCR не запущен"):
                await mark_ocr_processed(db, item_id)
            await message.answer(f"Запись #{item_id}: {esc(hint)}")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка фонового OCR записи %s", item_id)
            try:
                await message.answer(f"Запись #{item_id} сохранена. Распознавание можно повторить позже.")
            except Exception:
                log.exception("Не удалось отправить результат OCR записи %s", item_id)


async def _fetch(bot: Bot, file_obj, dest: Path) -> bool:
    try:
        await bot.download(file_obj, destination=dest)
        return True
    except (TelegramBadRequest, OSError) as exc:
        log.warning("Не удалось скачать вложение: %s", exc)
        dest.unlink(missing_ok=True)
        return False


async def _save_image(message: Message, bot: Bot, db: Database,
                      settings: Settings, file_obj, filename: str) -> None:
    size = getattr(file_obj, "file_size", None)
    if size and size > TG_DOWNLOAD_LIMIT:
        await message.answer("Файл больше 20 МБ. Telegram не позволяет его скачать через Bot API.")
        return
    folder = month_dir(settings.storage_dir, "screens", settings.tz)
    dest = unique_path(folder, safe_filename(filename, default="screen"), settings.tz)
    if not await _fetch(bot, file_obj, dest):
        await message.answer("Не получилось скачать скриншот. Попробуй отправить его ещё раз.")
        return
    caption = (message.caption or "").strip()
    stamp = datetime.now(settings.tz).strftime("%d.%m.%Y %H:%M")
    title = make_title(caption, fallback=f"Скриншот {stamp}")
    content = caption[:settings.max_content_chars]
    item_id = await db.add_item(
        category="screen", title=title, content=content,
        file_path=dest.relative_to(settings.storage_dir).as_posix(),
        tags=" ".join(parse_tags(caption)),
    )
    await _saved(message, item_id, db, settings)
    task = asyncio.create_task(_recognize_saved(message, db, settings, dest, item_id, title, caption))
    _background.add(task)
    task.add_done_callback(_background.discard)


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    image = message.photo[-1]
    await _save_image(message, bot, db, settings, image, f"{image.file_unique_id}.jpg")


@router.message(F.document)
async def on_document(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    doc = message.document
    original = doc.file_name or doc.file_unique_id
    ext = Path(original).suffix.lower()
    is_image = ext in IMAGE_EXTS or (doc.mime_type or "").startswith("image/")
    if is_image:
        if ext not in IMAGE_EXTS:
            await message.answer("Для OCR поддерживаются JPG, PNG, WEBP, BMP и TIFF. Пришли изображение в одном из этих форматов.")
            return
        await _save_image(message, bot, db, settings, doc, original)
        return
    if ext not in DOC_EXTENSIONS:
        await message.answer("Поддерживаются изображения, PDF, DOCX, XLSX и TXT.")
        return
    if doc.file_size and doc.file_size > TG_DOWNLOAD_LIMIT:
        await message.answer("Документ превышает 20 МБ, скачать его через Bot API не получится.")
        return
    folder = month_dir(settings.storage_dir, "files", settings.tz)
    dest = unique_path(folder, safe_filename(original, default=doc.file_unique_id), settings.tz)
    if not await _fetch(bot, doc, dest):
        await message.answer("Не удалось скачать файл; попробуй повторить отправку.")
        return
    try:
        extracted = await asyncio.to_thread(extract_text, dest, settings.max_content_chars)
    except Exception:
        log.exception("Не удалось разобрать документ %s", dest)
        extracted = ""
    caption = (message.caption or "").strip()
    content = "\n\n".join(part for part in (caption, extracted or "") if part)[:settings.max_content_chars]
    item_id = await db.add_item(
        category="table" if ext == ".xlsx" else "file",
        title=make_title(caption, fallback=Path(original).name),
        content=content,
        tags=" ".join(parse_tags(caption)),
        file_path=dest.relative_to(settings.storage_dir).as_posix(),
    )
    note = f"Извлечено {len(extracted):,} символов" if extracted else "Текст извлечь не удалось: файл доступен по названию. Для PDF-сканов пока нет OCR."
    await _saved(message, item_id, db, settings, note=note)


@router.message(F.text, ~F.text.startswith("/"))
async def on_text(message: Message, db: Database) -> None:
    value = (message.text or "").strip()
    if not value:
        return
    tags = parse_tags(value)
    item_id = await db.add_item(
        category="kb" if "kb" in tags else "note",
        title=make_title(value, fallback="Заметка"),
        content=value,
        tags=" ".join(tags),
    )
    mark_saved_input()
    await message.answer(
        f"✅ <b>Запись сохранена: #{item_id}</b>\nМожешь сразу выбрать категорию, цену или срок.",
        reply_markup=card_keyboard(item_id),
    )
