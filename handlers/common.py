"""/start, /help и ручной запуск бэкапа."""
from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from backup import send_backup
from handlers.ui import show_text
from config import Settings
from database import Database

router = Router(name="common")

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
    "/backup — прислать бэкап прямо сейчас (авто — раз в сутки)"
)


@router.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    await show_text(message, HELP_TEXT)


@router.message(Command("backup"))
async def cmd_backup(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    await show_text(message, "⏳ Собираю бэкап…")
    await send_backup(bot, db, settings)
