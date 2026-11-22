"""Replace navigation screens while preserving saved content and notifications."""
import asyncio
from collections import defaultdict

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, Message
from utils import esc, resolve_storage_path

_screens = {}
_locks = defaultdict(asyncio.Lock)


async def _delete(bot, chat, message_id):
    try:
        await bot.delete_message(chat, message_id)
    except TelegramBadRequest:
        # Telegram can refuse deletion of older messages.
        pass


async def show_text(message: Message, text: str, **kwargs):
    """Replace only the previous navigation screen, after successful delivery."""
    chat = message.chat.id
    async with _locks[chat]:
        previous = _screens.get(chat, [])
        if message.from_user and message.from_user.is_bot:
            previous = list(dict.fromkeys([*previous, message.message_id]))
        elif message.text:
            previous = list(dict.fromkeys([*previous, message.message_id]))
        sent = await message.answer(text, **kwargs)
        _screens[chat] = [sent.message_id]
        for message_id in previous:
            await _delete(message.bot, chat, message_id)
        return sent


async def show_card(message, row, settings, markup, text):
    chat = message.chat.id
    path = resolve_storage_path(settings.storage_dir, row["file_path"])
    if row["category"] != "screen" or path is None:
        return await show_text(message, text, reply_markup=markup)
    async with _locks[chat]:
        previous = _screens.get(chat, [])
        if message.from_user and message.from_user.is_bot:
            previous = list(dict.fromkeys([*previous, message.message_id]))
        title = f"<b>{esc(row['title'][:180])}</b>"
        try:
            photo = await message.answer_photo(FSInputFile(path), caption=title)
        except TelegramBadRequest:
            photo = await message.answer_document(FSInputFile(path), caption=title)
        try:
            sent = await message.answer(text, reply_markup=markup)
        except Exception:
            await _delete(message.bot, chat, photo.message_id)
            raise
        _screens[chat] = [photo.message_id, sent.message_id]
        for message_id in previous:
            await _delete(message.bot, chat, message_id)
        return sent
