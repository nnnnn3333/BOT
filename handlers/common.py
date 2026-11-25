"""/start, /help и ручной запуск бэкапа."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from backup import save_backup
from handlers.ui import show_text
from config import Settings
from database import Database

router = Router(name="common")
log = logging.getLogger(__name__)

HELP_TEXT = (
    "<b>Личная рабочая база</b>\n\n"
    "<b>Что сохраняется</b>\n"
    "• Текст → заметка. <code>#теги</code> в тексте попадают в поле тегов, "
    "<code>#kb</code> — в базу знаний.\n"
    "• Фото/скриншот → <code>storage/screens/</code>, подпись индексируется.\n"
    "• .pdf .docx .xlsx .txt → <code>storage/files/</code>, текст извлекается "
    "и ищется по содержимому.\n\n"
    "<b>Команды</b>\n"
    "/find <code>запрос</code> — полнотекстовый поиск\n"
    "/timer <code>название</code> — таймер задачи (Старт / Пауза / Стоп)\n"
    "/stats — тайминги за неделю в .xlsx\n"
    "/backup — сохранить копию на сервере (авто — раз в сутки)"
)


@router.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    await show_text(message, HELP_TEXT)


@router.message(Command("backup"))
async def cmd_backup(message: Message, db: Database, settings: Settings) -> None:
    await show_text(message, "⏳ Создаю резервную копию…")
    try:
        archive = await save_backup(db, settings)
    except Exception:
        log.exception("Не удалось создать резервную копию")
        await show_text(message, "⚠️ Не удалось сохранить копию. Проверь журнал на хостинге.")
        return
    if archive is None:
        await show_text(message, "⏳ Резервная копия уже создаётся.")
        return
    await show_text(message, f"✅ Копия сохранена на сервере.\n<code>{archive.name}</code>")
