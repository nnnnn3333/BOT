"""Track navigation messages across restarts and retain only the current screen."""
import asyncio
import logging
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass, field

from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

log = logging.getLogger(__name__)
_current = ContextVar("workbot_screen", default=None)
_keyboards = {}
_anchors = {}


# Telegram requires a message to attach a persistent reply keyboard.
KEYBOARD_CARRIER = "⠀"
COMMANDS = {"/start", "/menu", "/cancel", "/sections", "/find", "/deadline",
            "/deadlines", "/timer", "/stats", "/backup", "/help", "/reindex"}


def command_name(text):
    return text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""


def is_navigation(text):
    from handlers.workspace import MENU
    labels = {button.text for row in MENU.keyboard for button in row}
    return text in labels or text in {"🏠 Меню", "❌ Отмена"} or command_name(text) in COMMANDS


@dataclass
class Screen:
    chat: int
    messages: set = field(default_factory=set)
    saved_input: bool = False


def mark_saved_input():
    screen = _current.get()
    if screen is not None:
        screen.saved_input = True


@contextmanager
def preserve_messages():
    token = _current.set(None)
    try:
        yield
    finally:
        _current.reset(token)


class ScreenRequests(BaseRequestMiddleware):
    def __init__(self, db):
        self.db = db

    async def __call__(self, make_request, bot, method):
        screen = _current.get()
        try:
            result = await make_request(bot, method)
        except TelegramBadRequest as error:
            if (screen and method.__api_method__ in {"editMessageText", "editMessageCaption", "editMessageMedia"}
                    and getattr(method, "chat_id", None) == screen.chat
                    and "message is not modified" in str(error).lower()):
                screen.messages.add(method.message_id)
            raise
        markup = getattr(method, "reply_markup", None)
        if screen and isinstance(markup, ReplyKeyboardMarkup):
            labels = [button.text for row in markup.keyboard for button in row]
            _keyboards[screen.chat] = "input" if labels == ["❌ Отмена", "🏠 Меню"] else "menu"
        screen = _current.get()
        if screen is None:
            return result
        messages = result if isinstance(result, list) else [result]
        track = method.__api_method__ != "editMessageReplyMarkup"
        for message in messages:
            if track and isinstance(message, Message) and message.chat.id == screen.chat:
                if isinstance(markup, ReplyKeyboardMarkup) and getattr(method, "text", None) == KEYBOARD_CARRIER:
                    _anchors[screen.chat] = message.message_id
                screen.messages.add(message.message_id)
                await self.db.conn.execute(
                    "INSERT OR IGNORE INTO ui_messages(chat, message) VALUES (?, ?)",
                    (screen.chat, message.message_id),
                )
                await self.db.conn.commit()
        # Telegram sometimes returns True for message edits.
        if method.__api_method__ in ("editMessageText", "editMessageCaption", "editMessageMedia"):
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
                    state = data.get("state")
                    mode = "input" if state and await state.get_state() else "menu"
                    if _keyboards.get(screen.chat) != mode or screen.chat not in _anchors:
                        from handlers.workspace import MENU
                        keyboard = MENU
                        if mode == "input":
                            keyboard = ReplyKeyboardMarkup(
                                keyboard=[[KeyboardButton(text="❌ Отмена"), KeyboardButton(text="🏠 Меню")]],
                                resize_keyboard=True, is_persistent=True, one_time_keyboard=False,
                            )
                        await data["bot"].send_message(screen.chat, KEYBOARD_CARRIER, reply_markup=keyboard,
                                                       disable_notification=True)
                        _keyboards[screen.chat] = mode
                    await self.clean(data["bot"], screen)
                if incoming and (screen.saved_input or is_navigation((incoming.text or "").strip())):
                    await self.remove(data["bot"], screen.chat, incoming.message_id)
                return result
            finally:
                _current.reset(token)

    async def remove(self, bot, chat, message):
        try:
            await bot.delete_message(chat_id=chat, message_id=message)
        except (TelegramBadRequest, TelegramForbiddenError) as error:
            if isinstance(error, TelegramBadRequest):
                reason = str(error).lower()
                if "message to delete not found" in reason or "message can't be deleted" in reason:
                    return True
            log.info("Telegram refused UI cleanup: %s", type(error).__name__)
            return False
        return True

    async def clean(self, bot, screen):
        async with self.db.conn.execute(
            "SELECT message FROM ui_messages WHERE chat = ?", (screen.chat,)
        ) as cursor:
            previous = [row[0] for row in await cursor.fetchall()]
        for message_id in previous:
            if message_id not in screen.messages and message_id != _anchors.get(screen.chat):
                if await self.remove(bot, screen.chat, message_id):
                    await self.db.conn.execute(
                        "DELETE FROM ui_messages WHERE chat = ? AND message = ?",
                        (screen.chat, message_id),
                    )
        await self.db.conn.commit()
