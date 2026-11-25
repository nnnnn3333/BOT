"""Single navigation message with a media header."""
import asyncio
from collections import defaultdict
from pathlib import Path
from collections import OrderedDict
from uuid import uuid4
from aiogram import BaseMiddleware, Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InputMediaPhoto, Message, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from utils import resolve_storage_path
from handlers.section_headers import SectionHeader, header_upload

_screens = {}
_media = set()
_locks = defaultdict(asyncio.Lock)
HEADER = Path(__file__).with_name("workbot-header.png")
router = Router(name="screen_pages")
_pages = OrderedDict()


def page_markup(markup, key, index, total):
    rows = list(markup.inline_keyboard) if markup else []
    if total > 1:
        navigation = []
        if index:
            navigation.append(InlineKeyboardButton(text="‹ Назад", callback_data=f"pg:{key}:{index-1}"))
        navigation.append(InlineKeyboardButton(text=f"{index+1} / {total}", callback_data=f"pg:{key}:{index}"))
        if index + 1 < total:
            navigation.append(InlineKeyboardButton(text="Далее ›", callback_data=f"pg:{key}:{index+1}"))
        rows.insert(0, navigation)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


@router.callback_query(F.data.startswith("pg:"))
async def page_button(cb: CallbackQuery):
    parts = (cb.data or "").split(":", 2)
    if len(parts) != 3:
        return await cb.answer("Откройте запись заново.", show_alert=True)
    _, key, raw = parts
    entry = _pages.get(key)
    if entry is None or entry["chat"] != cb.from_user.id or not raw.isdigit():
        return await cb.answer("Откройте запись или список заново.", show_alert=True)
    index = int(raw)
    if index >= len(entry["pages"]):
        return await cb.answer()
    await cb.answer()
    if isinstance(cb.message, Message):
        await _render(cb.message, "", entry["markup"], entry["photo"], page=(key,index))



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


def photo_input(path):
    # Telegram accepts JPEG previews; originals stay untouched in storage.
    from io import BytesIO
    from PIL import Image, ImageOps
    with Image.open(path) as source:
        preview = ImageOps.exif_transpose(source).convert("RGB")
        preview.thumbnail((2560, 2560))
        output = BytesIO()
        preview.save(output, format="JPEG", quality=90)
    return BufferedInputFile(output.getvalue(), filename="preview.jpg")


async def _render(message, text, markup, photo=None, page=None):
    chat = message.chat.id
    async with _locks[chat]:
        previous = _previous(message)
        is_media = previous and (chat, previous) in _media
        photo = photo or (HEADER if is_media else None)
        if page is None:
            from handlers.text_pages import split_html
            chunks = split_html(text, 900)
            key = uuid4().hex[:12]
            _pages[key] = {"chat": chat, "pages": chunks, "markup": markup, "photo": photo}
            while len(_pages) > 20:
                _pages.popitem(last=False)
            index = 0
        else:
            key,index = page
            chunks = _pages[key]["pages"]
        upload = None
        if isinstance(photo, SectionHeader):
            upload = await asyncio.to_thread(header_upload, photo)
        elif photo:
            upload = await asyncio.to_thread(photo_input, photo)
        text = chunks[index]
        markup = page_markup(markup, key, index, len(chunks))
        if previous and not isinstance(markup, ReplyKeyboardMarkup):
            try:
                if photo:
                    result = await message.bot.edit_message_media(
                        chat_id=chat, message_id=previous,
                        media=InputMediaPhoto(media=upload, caption=text, parse_mode="HTML"),
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
                if "not found" in str(error).lower():
                    from handlers.navigation import _anchors, _keyboards
                    _anchors.pop(chat, None)
                    _keyboards.pop(chat, None)
                    _media.discard((chat, previous))
        if photo:
            sent = await message.answer_photo(upload, caption=text,
                                              parse_mode="HTML", reply_markup=markup)
            _media.add((chat, sent.message_id))
        else:
            sent = await message.answer(text, parse_mode="HTML", reply_markup=markup)
        _screens[chat] = sent.message_id
        if previous and previous != sent.message_id:
            await _retire(message.bot, chat, previous)
        return sent


async def show_text(message: Message, text: str, **kwargs):
    markup = kwargs.get("reply_markup")
    if isinstance(markup, ReplyKeyboardMarkup):
        markup = None
    return await _render(message, text, markup, kwargs.get("header"))


async def show_card(message, row, settings, markup, text):
    path = resolve_storage_path(settings.storage_dir, row["file_path"])
    photo = path if row["category"] == "screen" else None
    if photo is None and row["current_section_id"] is not None:
        photo = SectionHeader(row["current_section_id"], row["section_name"])
    from handlers.workspace import card_keyboard
    markup = card_keyboard(row["id"], bool(row["file_path"]), favorite=bool(row["favorite"]))
    return await _render(message, text, markup, photo)



class NavigationMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        from handlers.navigation import is_navigation, command_name, menu_text
        text = menu_text(event.text)
        command = command_name(text)
        if command in {"/start", "/menu"} or text == "🏠 Меню":
            chat = event.chat.id
            _screens.pop(chat, None)
            _media.difference_update(key for key in list(_media) if key[0] == chat)
            from handlers.navigation import _anchors, _keyboards
            _anchors.pop(chat, None)
            _keyboards.pop(chat, None)
        if is_navigation(text):
            state = data.get("state")
            if state is not None:
                await state.clear()
                data["raw_state"] = None
        return await handler(event, data)
