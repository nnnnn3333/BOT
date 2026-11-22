"""Single navigation message with a media header."""
import asyncio
from collections import defaultdict
from pathlib import Path
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InputMediaPhoto, Message, ReplyKeyboardMarkup
from utils import resolve_storage_path

_screens = {}
_media = set()
_locks = defaultdict(asyncio.Lock)
HEADER = Path(__file__).with_name("workbot-header.png")


async def _retire(bot, chat, message_id):
    try:
        await bot.edit_message_reply_markup(chat_id=chat, message_id=message_id, reply_markup=None)
    except TelegramBadRequest:
        pass


def _previous(message):
    if message.from_user and message.from_user.is_bot:
        if message.photo or message.document:
            _media.add((message.chat.id, message.message_id))
        return _screens.get(message.chat.id, message.message_id)
    return _screens.get(message.chat.id)


async def _render(message, text, markup, photo=None):
    chat = message.chat.id
    async with _locks[chat]:
        previous = _previous(message)
        is_media = previous and (chat, previous) in _media
        photo = photo or (HEADER if is_media else None)
        if previous and not isinstance(markup, ReplyKeyboardMarkup):
            try:
                if photo:
                    result = await message.bot.edit_message_media(
                        chat_id=chat, message_id=previous,
                        media=InputMediaPhoto(media=FSInputFile(photo), caption=text, parse_mode="HTML"),
                        reply_markup=markup,
                    )
                    _media.add((chat, previous))
                else:
                    result = await message.bot.edit_message_text(
                        chat_id=chat, message_id=previous, text=text, reply_markup=markup, parse_mode="HTML",
                    )
                _screens[chat] = previous
                return result
            except TelegramBadRequest as error:
                if "message is not modified" in str(error).lower():
                    return message
        if photo:
            sent = await message.answer_photo(FSInputFile(photo), caption=text,
                                              parse_mode="HTML", reply_markup=markup)
            _media.add((chat, sent.message_id))
        else:
            sent = await message.answer(text, parse_mode="HTML", reply_markup=markup)
        _screens[chat] = sent.message_id
        if previous and previous != sent.message_id:
            await _retire(message.bot, chat, previous)
        return sent


async def show_text(message: Message, text: str, **kwargs):
    # Reply keyboards need a separate text message to restore the bottom menu.
    if isinstance(kwargs.get("reply_markup"), ReplyKeyboardMarkup) and not _previous(message):
        async with _locks[message.chat.id]:
            previous = _previous(message)
            if previous:
                await _retire(message.bot, message.chat.id, previous)
            sent = await message.answer(text, **kwargs)
            _screens[message.chat.id] = sent.message_id
            return sent
    markup = kwargs.get("reply_markup")
    if isinstance(markup, ReplyKeyboardMarkup):
        markup = None
    return await _render(message, text, markup)


async def show_card(message, row, settings, markup, text):
    path = resolve_storage_path(settings.storage_dir, row["file_path"])
    photo = path if row["category"] == "screen" else None
    if photo:
        from handlers.workspace import card_text
        # Captions allow 1024 UTF-16 units. Full content remains available separately.
        from html import unescape
        import re
        def length(value):
            return len(unescape(re.sub("<[^>]*>", "", value)).encode("utf-16-le")) // 2
        limit = 900
        while length(text) > 1000 and limit >= 0:
            text = card_text(row, settings, content_limit=limit, compact=True)
            limit -= 100
        if length(text) > 1000:
            text = card_text(row, settings, content_limit=0, compact=True)
    return await _render(message, text, markup, photo)


class NavigationMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        commands = {
            "/start", "/menu", "/cancel", "/sections", "/find", "/deadline",
            "/deadlines", "/timer", "/stats", "/backup", "/help", "/reindex",
        }
        labels = {
            "🔎 Поиск", "🗂 База знаний", "➕ Добавить", "📅 Дедлайны",
            "⏱ Таймеры", "📊 Отчёт", "🏠 Меню", "❌ Отмена",
            "🗄 Резервная копия", "❓ Помощь",
        }
        text = (event.text or "").strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""
        if text in labels or command in commands:
            state = data.get("state")
            if state is not None:
                await state.clear()
                data["raw_state"] = None
        return await handler(event, data)
