"""Track navigation messages across restarts and retain only the current screen."""
import asyncio
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field

from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message

log = logging.getLogger(__name__)
_current = ContextVar("workbot_screen", default=None)


@dataclass
class Screen:
    chat: int
    messages: set = field(default_factory=set)


class ScreenRequests(BaseRequestMiddleware):
    def __init__(self, db):
        self.db = db

    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        screen = _current.get()
        if screen is None:
            return result
        messages = result if isinstance(result, list) else [result]
        for message in messages:
            if isinstance(message, Message) and message.chat.id == screen.chat:
                screen.messages.add(message.message_id)
                await self.db.conn.execute(
                    "INSERT OR IGNORE INTO ui_messages(chat, message) VALUES (?, ?)",
                    (screen.chat, message.message_id),
                )
                await self.db.conn.commit()
        # Telegram sometimes returns True for message edits.
        if method.__api_method__.startswith("editMessage"):
            if getattr(method, "chat_id", None) == screen.chat:
                message_id = getattr(method, "message_id", None)
                if message_id:
                    screen.messages.add(message_id)
        return result


class ScreenNavigation(BaseMiddleware):
    def __init__(self, db):
        self.db = db
        self.lock = asyncio.Lock()

    async def __call__(self, handler, event, data):
        callback = event.callback_query
        incoming = event.message
        message = callback.message if callback else incoming
        if not isinstance(message, Message) or message.chat.type != "private":
            return await handler(event, data)
        async with self.lock:
            screen = Screen(message.chat.id)
            token = _current.set(screen)
            try:
                if callback:
                    await self.db.conn.execute(
                        "INSERT OR IGNORE INTO ui_messages(chat, message) VALUES (?, ?)",
                        (screen.chat, message.message_id),
                    )
                    await self.db.conn.commit()
                result = await handler(event, data)
                if screen.messages:
                    await self.clean(data["bot"], screen)
                    if incoming:
                        # Uploaded originals have already been stored by the handler.
                        await self.remove(data["bot"], screen.chat, incoming.message_id)
                return result
            finally:
                _current.reset(token)

    async def remove(self, bot, chat, message):
        try:
            await bot.delete_message(chat_id=chat, message_id=message)
        except (TelegramBadRequest, TelegramForbiddenError) as error:
            log.info("Telegram refused UI cleanup: %s", type(error).__name__)
            return False
        return True

    async def clean(self, bot, screen):
        async with self.db.conn.execute(
            "SELECT message FROM ui_messages WHERE chat = ?", (screen.chat,)
        ) as cursor:
            previous = [row[0] for row in await cursor.fetchall()]
        for message_id in previous:
            if message_id not in screen.messages:
                if await self.remove(bot, screen.chat, message_id):
                    await self.db.conn.execute(
                        "DELETE FROM ui_messages WHERE chat = ? AND message = ?",
                        (screen.chat, message_id),
                    )
        await self.db.conn.commit()
