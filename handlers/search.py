"""/find — полнотекстовый поиск по FTS5 + кнопки «файл» / «полный текст»."""
from __future__ import annotations

import sqlite3

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Settings
from database import HL_CLOSE, HL_OPEN, Database
from utils import build_fts_query, esc, resolve_storage_path, to_local

router = Router(name="search")

ICONS = {"note": "📝", "file": "📄", "screen": "🖼", "table": "📊", "kb": "📚"}
RESULT_LIMIT = 10
MAX_INLINE_TEXT = 3500  # длиннее — отправляем .txt


class ItemCb(CallbackData, prefix="it"):
    action: str  # "text" | "file"
    id: int


def _snippet(raw: str | None) -> str:
    flat = " ".join((raw or "").split())
    return esc(flat).replace(HL_OPEN, "<b>").replace(HL_CLOSE, "</b>")


@router.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer("Использование: /find <code>слова для поиска</code>")
        return

    fts_query = build_fts_query(query)
    if not fts_query:
        await message.answer("В запросе нет слов для поиска.")
        return

    try:
        rows = await db.search(fts_query, limit=RESULT_LIMIT)
    except sqlite3.Error:
        await message.answer("Не удалось разобрать запрос, попробуйте проще.")
        return

    if not rows:
        await message.answer(f"Ничего не найдено по запросу «{esc(query)}».")
        return

    lines = [f"🔎 <b>Найдено: {len(rows)}</b> по запросу «{esc(query)}»\n"]
    keyboard = InlineKeyboardBuilder()

    for number, row in enumerate(rows, start=1):
        icon = ICONS.get(row["category"], "•")
        local = to_local(row["created_at"], settings.tz)
        meta = [local.strftime("%d.%m.%Y")] if local else []
        if row["tags"]:
            meta.append(" ".join(f"#{t}" for t in row["tags"].split()))

        lines.append(f"<b>{number}.</b> {icon} <b>{esc(row['title'][:80])}</b> <i>#{row['id']}</i>")
        snippet = _snippet(row["snippet"])
        if snippet:
            lines.append(snippet)
        if meta:
            lines.append(f"<i>{esc(' · '.join(meta))}</i>")
        lines.append("")

        buttons = [
            InlineKeyboardButton(
                text=f"📝 {number} · текст",
                callback_data=ItemCb(action="text", id=row["id"]).pack(),
            )
        ]
        if row["file_path"]:
            label = "🖼 скрин" if row["category"] == "screen" else "📎 файл"
            buttons.append(
                InlineKeyboardButton(
                    text=f"{label} {number}",
                    callback_data=ItemCb(action="file", id=row["id"]).pack(),
                )
            )
        keyboard.row(*buttons)

    await message.answer("\n".join(lines).strip(), reply_markup=keyboard.as_markup())


@router.callback_query(ItemCb.filter(F.action == "text"))
async def cb_full_text(cb: CallbackQuery, callback_data: ItemCb, bot: Bot, db: Database) -> None:
    item = await db.get_item(callback_data.id)
    if item is None:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    await cb.answer()

    chat_id = cb.from_user.id
    icon = ICONS.get(item["category"], "•")
    header = f"{icon} <b>{esc(item['title'])}</b>  <i>#{item['id']}</i>"
    text = item["content"] or ""

    if not text:
        await bot.send_message(chat_id, f"{header}\n\n<i>Текста нет (только файл или подпись пуста).</i>")
    elif len(text) <= MAX_INLINE_TEXT:
        await bot.send_message(chat_id, f"{header}\n\n{esc(text)}")
    else:
        await bot.send_document(
            chat_id,
            BufferedInputFile(text.encode("utf-8"), filename=f"item_{item['id']}.txt"),
            caption=f"{header}\nТекст длинный — отправляю файлом.",
        )


@router.callback_query(ItemCb.filter(F.action == "file"))
async def cb_file(cb: CallbackQuery, callback_data: ItemCb, bot: Bot, db: Database, settings: Settings) -> None:
    item = await db.get_item(callback_data.id)
    if item is None:
        await cb.answer("Запись не найдена", show_alert=True)
        return

    path = resolve_storage_path(settings.storage_dir, item["file_path"])
    if path is None:
        await cb.answer("Файл не найден на диске", show_alert=True)
        return
    await cb.answer()

    chat_id = cb.from_user.id
    caption = f"<b>{esc(item['title'][:200])}</b>  <i>#{item['id']}</i>"
    upload = FSInputFile(path)

    if item["category"] == "screen":
        try:
            await bot.send_photo(chat_id, upload, caption=caption)
            return
        except TelegramBadRequest:  # слишком большое/странное изображение — шлём как файл
            upload = FSInputFile(path)
    await bot.send_document(chat_id, upload, caption=caption)
