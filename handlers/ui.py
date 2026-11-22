"""Navigation through edits; user messages and saved files are retained."""
import asyncio
from collections import defaultdict

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardMarkup, Message, ReplyKeyboardMarkup
from utils import esc, resolve_storage_path

_screens = {}
_locks = defaultdict(asyncio.Lock)


async def _retire(bot, chat, message_id):
    try:
        await bot.edit_message_reply_markup(chat_id=chat, message_id=message_id, reply_markup=None)
    except TelegramBadRequest:
        pass


async def show_text(message: Message, text: str, **kwargs):
    chat = message.chat.id
    async with _locks[chat]:
        previous = _screens.get(chat)
        if message.from_user and message.from_user.is_bot and message.text:
            previous = message.message_id
        if previous and not isinstance(kwargs.get("reply_markup"), ReplyKeyboardMarkup):
            try:
                result = await message.bot.edit_message_text(
                    chat_id=chat, message_id=previous, text=text, **kwargs
                )
                _screens[chat] = previous
                return result
            except TelegramBadRequest as error:
                if "message is not modified" in str(error).lower():
                    _screens[chat] = previous
                    return message
        sent = await message.answer(text, **kwargs)
        _screens[chat] = sent.message_id
        if previous and previous != sent.message_id:
            await _retire(message.bot, chat, previous)
        return sent


async def show_card(message, row, settings, markup, text):
    path = resolve_storage_path(settings.storage_dir, row["file_path"])
    if row["category"] == "screen" and path is not None:
        title = f"<b>{esc(row['title'][:180])}</b>"
        try:
            await message.answer_photo(FSInputFile(path), caption=title)
        except TelegramBadRequest:
            await message.answer_document(FSInputFile(path), caption=title)
    return await show_text(message, text, reply_markup=markup)
