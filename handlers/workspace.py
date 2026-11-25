"""Кнопочное меню Workbot: категории, карточки, поиск, сроки, редактирование."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from ocr_local import recognize

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Settings
from database import Database
from utils import esc
from handlers.ui import HEADER, show_text, show_card
from handlers.section_headers import SectionHeader
from workspace_db import (add_deadline, create_section, delete_item, get_item, get_section,
                          list_deadlines, list_items, list_sections, parse_datetime,
                          remove_deadline, search_items, set_deadline_done, update_item,
                          mark_ocr_processed, pending_screens, set_favorite, remember_browse,
                          browse_context, reschedule_deadline, rename_section, delete_section)

router = Router(name="workspace")

MENU = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🔎 Поиск"), KeyboardButton(text="🗂 База знаний")],
    [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="📅 Дедлайны")],
    [KeyboardButton(text="⏱ Таймеры"), KeyboardButton(text="📊 Отчёт")],
    [KeyboardButton(text="🗄 Резервная копия"), KeyboardButton(text="❓ Помощь")],
], resize_keyboard=True, is_persistent=True, one_time_keyboard=False, input_field_placeholder="Перешли скрин, документ или заметку")


class SearchInput(StatesGroup):
    query = State()


class EditInput(StatesGroup):
    value = State()


class SectionInput(StatesGroup):
    name = State()
    rename = State()


class DeadlineInput(StatesGroup):
    title = State()
    date = State()


class DeadlineEditInput(StatesGroup):
    date = State()


class TimerInput(StatesGroup):
    name = State()


class WS(CallbackData, prefix="ws"):
    action: str
    id: int = 0
    page: int = 0
    item: int = 0


def btn(text: str, action: str, id: int = 0, page: int = 0, item: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=WS(action=action, id=id, page=page, item=item).pack())


def card_keyboard(item_id: int, has_file: bool = False, favorite: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [btn("✏️ Редактировать", "edit", item_id), btn("📁 Переместить", "choose", item=item_id)],
        [btn("📅 Срок действия", "input", item_id, page=2), btn("💰 Цена", "input", item_id, page=1)],
    ]
    rows.append([btn("★ В избранном" if favorite else "☆ В избранное",
                     "unstar" if favorite else "star", item_id)])
    rows.append([btn("‹ К списку", "return", item_id)])
    rows.append([btn("🗑 Удалить", "delask", item_id), btn("🏠 В меню", "root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _short(value: str, max_len: int = 65) -> str:
    text = " ".join((value or "").split())
    return text if len(text) <= max_len else text[:max_len - 1] + "…"


def card_text(row, settings: Settings) -> str:
    lines = [f"<b>#{row['id']} · {esc(row['title'])}</b>", f"📁 {esc(row['section_name'])}\n"]
    if row["price"]:
        lines.append(f"💰 {esc(row['price'])}")
    if row["valid_until"]:
        today = datetime.now(settings.tz).date().isoformat()
        marker = "🔴 Истёк" if row["valid_until"] < today else "🟢 Действует"
        lines.append(f"📅 До {esc(row['valid_until'])} · {marker}")
    if row["details"]:
        lines.append(f"📋 {esc(row['details'])}")
    if row["content"]:
        lines.append("\n<b>Содержимое:</b>\n" + esc(row["content"]))
    else:
        lines.append("\nТекст не указан")
    return "\n".join(lines)


async def _show(cb: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None,
                *, header=None) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await show_text(cb.message, text, reply_markup=markup, header=header)


async def send_menu(message: Message) -> None:
    await show_text(message,
        "<b>Workbot · рабочая база</b>\n\n"
        "Перешли сюда скриншот, текст или документ — он сохранится и будет доступен через поиск. "
        "После сохранения выбери раздел и при необходимости укажи цену или срок действия.\n\n"
        "Дедлайны напоминают о себе за сутки, за час и в момент наступления срока.",
        reply_markup=MENU,
    )


@router.message(Command("start"))
@router.message(Command("menu"))
@router.message(F.text == "🏠 Меню")
async def cmd_menu(message: Message) -> None:
    await send_menu(message)


@router.message(F.text == "🗄 Резервная копия")
async def backup_button(message: Message, db: Database, settings: Settings) -> None:
    from handlers.common import cmd_backup
    await cmd_backup(message, db, settings)


@router.message(F.text == "❓ Помощь")
async def help_button(message: Message, db: Database) -> None:
    async with db.conn.execute("SELECT value FROM work_system WHERE key='last_local_backup'") as cursor:
        backup = await cursor.fetchone()
    backup_status = esc(backup[0]) if backup else "ещё не сохранена"
    await show_text(
        message,
        "<b>❓ Помощь</b>\n\n"
        "Выбирайте действия в нижнем меню.\n"
        "Чтобы сохранить заметку, фото или документ, отправьте их в чат.\n"
        "При вводе данных нижнее меню заменяется кнопками отмены и возврата.\n\n"
        f"Последняя сохранённая резервная копия: {backup_status}",
    )


@router.message(Command("cancel"))
@router.message(F.text == "❌ Отмена")
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_text(message, "Действие отменено.", reply_markup=MENU)


async def browse_roots(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    for section in await list_sections(db):
        kb.row(btn(f"📁 {section['name']}", "section", section["id"]))
    kb.row(btn("🕓 Последние записи", "items"), btn("🟢 Сроки действуют", "active"))
    kb.row(btn("⭐ Избранное", "favorite"), btn("📅 Сроки сегодня", "today"))
    kb.row(btn("Завтра", "tomorrow"), btn("В ближайшие 3 дня", "soon"))
    kb.row(btn("📥 Без раздела", "unfiled"), btn("➕ Новый раздел", "newsec"))
    return "<b>🗂 База знаний</b>\n\nВыбери раздел или открой последние записи.", kb.as_markup()


@router.message(F.text == "🗂 База знаний")
@router.message(Command("sections"))
async def cmd_sections(message: Message, db: Database) -> None:
    text, markup = await browse_roots(db)
    await show_text(message, text, reply_markup=markup, header=HEADER)


@router.message(F.text == "➕ Добавить")
async def cmd_add(message: Message) -> None:
    await show_text(message,
        "<b>Новая запись</b>\n\nПерешли сообщение, скриншот или файл в этот чат. "
        "Бот сохранит оригинал, извлечёт текст (если возможно) и покажет кнопки для заполнения карточки.\n\n"
        "Если отправляешь несколько скринов, каждый сохранится отдельно."
    )


@router.message(F.text == "🔎 Поиск")
async def ask_search(message: Message, state: FSMContext) -> None:
    await state.set_state(SearchInput.query)
    await show_text(message, "Введи товар, цену, название акции, условие или дату (например, 2026-12-01).\n/cancel — отмена.")


async def send_search(message: Message, db: Database, settings: Settings, query: str) -> None:
    await remember_browse(db, message.chat.id, "search", query=query)
    results = await search_items(db, query)
    if not results:
        await show_text(message, f"По запросу «{esc(query)}» ничего не нашлось.")
        return
    kb = InlineKeyboardBuilder()
    for row in results:
        kb.row(btn(f"📄 {_short(row['title'], 40)} · #{row['id']}", "card", row["id"]))
    await show_text(message, f"🔎 <b>Поиск: {esc(query)}</b>\nНайдено: {len(results)}\n\n" +
                         "\n".join(f"• #{row['id']} {esc(_short(row['title'], 78))}" for row in results),
                         reply_markup=kb.as_markup())


@router.message(Command("find"))
async def find_command(message: Message, command: CommandObject, db: Database,
                       settings: Settings, state: FSMContext) -> None:
    query = (command.args or "").strip()
    if not query:
        await ask_search(message, state)
    else:
        await send_search(message, db, settings, query)


@router.message(SearchInput.query)
async def find_text(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    await state.clear()
    if not message.text:
        await show_text(message, "Пришли текст для поиска.")
        return
    await send_search(message, db, settings, message.text.strip()[:150])


async def deadline_screen(db, settings, page=0):
    rows = await list_deadlines(db, limit=9, page=page, page_size=8)
    more = len(rows) > 8
    rows = rows[:8]
    kb = InlineKeyboardBuilder()
    lines = ["<b>📅 Дедлайны</b>", ""]
    for row in rows:
        due = datetime.strptime(row["due_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        local = due.astimezone(settings.tz)
        marker = "🔴" if due < datetime.now(timezone.utc) else "⏳"
        lines.append(f"{marker} #{row['id']} · {esc(_short(row['title'], 100))} · {local:%d.%m.%Y %H:%M}")
        kb.row(btn(f"✅ #{row['id']}", "done", row["id"]),
               btn("✏️ Перенести", "dedit", row["id"]), btn("🗑 Удалить", "ddelask", row["id"]))
    if not rows:
        lines.append("Нет активных дедлайнов.")
    arrows = []
    if page:
        arrows.append(btn("‹ Назад", "deadlines", page=page-1))
    if more:
        arrows.append(btn("Далее ›", "deadlines", page=page+1))
    if arrows:
        kb.row(*arrows)
    kb.row(btn("➕ Добавить", "newdeadline"), btn("🏠 Меню", "root"))
    return "\n".join(lines), kb.as_markup()


async def show_deadlines(message: Message, db: Database, settings: Settings) -> None:
    text, markup = await deadline_screen(db, settings)
    await show_text(message, text, reply_markup=markup)


@router.message(F.text == "📅 Дедлайны")
@router.message(Command("deadlines"))
async def deadlines_command(message: Message, db: Database, settings: Settings) -> None:
    await show_deadlines(message, db, settings)


@router.message(Command("deadline"))
async def new_deadline_command(message: Message, state: FSMContext) -> None:
    await state.set_state(DeadlineInput.title)
    await show_text(message, "Как называется задача?\nНапример: Отправить отчёт по продажам. /cancel — отмена.")


@router.message(DeadlineInput.title)
async def deadline_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()[:200]
    if not title:
        await show_text(message, "Введи название текстом")
        return
    await state.update_data(title=title)
    await state.set_state(DeadlineInput.date)
    await show_text(message, "Укажи срок: 25.11.2026 18:00\nЕсли время не указано, будет 12:00. /cancel — отмена.")


@router.message(DeadlineEditInput.date)
async def deadline_edit_date(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    try:
        when = parse_datetime(message.text or "", settings.tz)
        if when.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise ValueError("Укажите будущее время")
    except ValueError as exc:
        await show_text(message, f"Ошибка даты: {esc(exc)}. Пример: 25.11.2026 18:00")
        return
    data = await state.get_data()
    if not await reschedule_deadline(db, data["deadline_id"], when):
        await state.clear()
        await show_text(message, "Дедлайн уже удалён.", reply_markup=MENU)
        return
    await state.clear()
    await show_text(message, f"✅ Дедлайн перенесён на {when:%d.%m.%Y %H:%M}", reply_markup=MENU)


@router.message(DeadlineInput.date)
async def deadline_date(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    try:
        when = parse_datetime(message.text or "", settings.tz)
        if when.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise ValueError("Время уже прошло")
    except ValueError as exc:
        await show_text(message, f"Ошибка даты: {esc(exc)}. Пример: 25.11.2026 18:00")
        return
    data = await state.get_data()
    await add_deadline(db, data["title"], when)
    await state.clear()
    await show_text(message, f"✅ Дедлайн создан: {esc(data['title'])}\n{when:%d.%m.%Y %H:%M}", reply_markup=MENU)


@router.message(F.text == "⏱ Таймеры")
async def cmd_timer_menu(message: Message, db: Database, state: FSMContext) -> None:
    async with db.conn.execute(
        "SELECT id,task_name,status FROM timings WHERE status IN ('new','running','paused') ORDER BY id DESC LIMIT 6"
    ) as cur:
        rows = list(await cur.fetchall())
    kb = InlineKeyboardBuilder()
    for record in rows:
        kb.row(btn(f"⏱ {_short(record['task_name'], 45)}", "timer", record["id"]))
    await state.set_state(TimerInput.name)
    await show_text(message, "<b>⏱ Таймеры</b>\n\nВыберите таймер или введите название новой задачи.",
                    reply_markup=kb.as_markup())


@router.message(TimerInput.name)
async def timer_name(message: Message, db: Database, state: FSMContext) -> None:
    name = (message.text or "").strip()[:200]
    if not name:
        await show_text(message, "Нужно название задачи.")
        return
    from handlers.timers import render_timer, timer_keyboard
    timer_id = await db.create_timing(name)
    await state.clear()
    await show_text(message, render_timer(await db.get_timing(timer_id)), reply_markup=timer_keyboard(timer_id))


@router.message(F.text == "📊 Отчёт")
async def stats_menu(message: Message, db: Database, settings: Settings) -> None:
    from handlers.timers import cmd_stats
    await cmd_stats(message, db, settings)


@router.message(SectionInput.name)
async def section_name(message: Message, state: FSMContext, db: Database) -> None:
    name = (message.text or "").strip()
    if not name:
        await show_text(message, "Раздел должен иметь название.")
        return
    data = await state.get_data()
    try:
        section_id = await create_section(db, name, data.get("parent_id"))
    except ValueError as exc:
        await show_text(message, esc(exc))
        return
    await state.clear()
    await show_text(message, f"✅ Раздел создан: {esc(name)} (#{section_id})", reply_markup=MENU)


EDIT_FIELDS = {1: ("price", "цену, например 19 990 ₽"),
               2: ("valid_until", "дату окончания, например 25.11.2026"),
               3: ("title", "новое название"),
               4: ("details", "условия акции или пояснение"),
               5: ("content", "текст записи"),
               6: ("tags", "теги через пробел, без #")}


@router.message(EditInput.value)
async def edit_value(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    data = await state.get_data()
    field = data.get("field")
    item_id = data.get("item_id")
    if field not in {entry[0] for entry in EDIT_FIELDS.values()} or not item_id:
        await state.clear()
        await show_text(message, "Действие истекло, открой карточку ещё раз.")
        return
    raw = (message.text or "").strip()
    if field == "valid_until":
        if raw.lower() in {"нет", "-", "удалить"}:
            value = None
        else:
            try:
                value = parse_datetime(raw, settings.tz, end_of_day=True).date().isoformat()
            except ValueError as exc:
                await show_text(message, "Не понял дату: " + esc(exc) + ". Например, 25.11.2026")
                return
    elif field == "title":
        value = raw[:150]
        if not value:
            await show_text(message, "Название не может быть пустым")
            return
    elif field == "content":
        value = raw[:100_000]
    else:
        value = raw[:1500]
    await update_item(db, item_id, field, value)
    if field == "valid_until":
        if value:
            item = await get_item(db, item_id)
            when = parse_datetime(value, settings.tz, end_of_day=True)
            if when.astimezone(timezone.utc) > datetime.now(timezone.utc):
                await add_deadline(db, f"Истекает срок: {item['title']}", when, item_id=item_id)
            else:
                await db.conn.execute("DELETE FROM work_deadlines WHERE item_id=?", (item_id,))
                await db.conn.commit()
        else:
            await db.conn.execute("DELETE FROM work_deadlines WHERE item_id=?", (item_id,))
            await db.conn.commit()
    await state.clear()
    item = await get_item(db, item_id)
    await show_card(message, item, settings, card_keyboard(item_id, bool(item["file_path"])),
                    card_text(item, settings))


async def section_screen(db, action, section, page, settings):
    id_ = section["id"] if section else 0
    rows, more = await list_items(
        db, section_id=id_ or None, page=page, active_only=(action == "active"),
        filter_name=action, today=datetime.now(settings.tz).date().isoformat(),
    )
    kb = InlineKeyboardBuilder()
    if section and section["parent_id"] is None:
        for child in await list_sections(db, id_):
            kb.row(btn(f"📂 {child['name']}", "section", child["id"]))
    for row in rows:
        kb.row(btn(f"📄 {_short(row['title'], 45)}", "card", row["id"]))
    arrows = []
    if page > 0:
        arrows.append(btn("‹ Назад", action, id_, page - 1))
    if more:
        arrows.append(btn("Далее ›", action, id_, page + 1))
    if arrows:
        kb.row(*arrows)
    if section:
        if section["parent_id"] is None:
            kb.row(btn("➕ Подраздел", "newsec", id_))
        kb.row(btn("⚙️ Управление разделом", "secmanage", id_))
        kb.row(btn("‹ Разделы", "sections" if section["parent_id"] is None else "section",
                   section["parent_id"] or 0))
    else:
        kb.row(btn("‹ Разделы", "sections"))
    titles = {"unfiled": "📥 Без раздела", "favorite": "⭐ Избранное", "today": "📅 Сроки сегодня",
              "tomorrow": "📅 Сроки завтра", "soon": "📅 Сроки в ближайшие 3 дня"}
    if section:
        title = f"📁 {esc(section['name'])}"
    elif action == "active":
        title = "🟢 Действующие предложения"
    else:
        title = titles.get(action, "📄 Последние записи")
    description = "В этом разделе нет записей."
    if rows:
        description = "Выберите запись."
        if page or more:
            description = f"Страница {page + 1} · {description}"
    return f"<b>{title}</b>\n\n{description}", kb.as_markup()


@router.message(SectionInput.rename)
async def section_rename(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    section_id = (await state.get_data()).get("section_id")
    try:
        changed = bool(section_id) and await rename_section(db, section_id, message.text or "")
    except ValueError as error:
        await show_text(message, esc(error), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [btn("Отмена", "secmanage", section_id)]]))
        return
    await state.clear()
    if not changed:
        text, markup = await browse_roots(db)
        await show_text(message, text, reply_markup=markup, header=HEADER)
        return
    section = await get_section(db, section_id)
    text, markup = await section_screen(db, "section", section, 0, settings)
    await show_text(message, text, reply_markup=markup, header=SectionHeader(section_id, section["name"]))


@router.callback_query(WS.filter())
async def workspace_button(cb: CallbackQuery, callback_data: WS, db: Database,
                           settings: Settings, state: FSMContext) -> None:
    a, id_, page, item_id = callback_data.action, callback_data.id, callback_data.page, callback_data.item
    if not isinstance(cb.message, Message):
        return await cb.answer("Открой бота и повтори действие.", show_alert=True)
    if a in {"secmanage", "secrename", "secdelask", "secdel"}:
        # All routes already pass AdminOnlyMiddleware before reaching this handler.
        section = await get_section(db, id_)
        if section is None:
            return await cb.answer("Раздел уже удалён.", show_alert=True)
        await state.clear()
        header = SectionHeader(id_, section["name"])
        back = [btn("‹ К разделу", "section", id_)]
        if a == "secmanage":
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [btn("✏️ Переименовать", "secrename", id_)],
                [btn("🗑 Удалить раздел", "secdelask", id_)], back,
            ])
            return await _show(cb, f"<b>{esc(section['name'])}</b>\nУправление разделом", markup, header=header)
        if a == "secrename":
            await state.set_state(SectionInput.rename)
            await state.update_data(section_id=id_)
            return await _show(cb, "Введи новое название.", InlineKeyboardMarkup(inline_keyboard=[back]),
                               header=header)
        if a == "secdelask":
            parent = await get_section(db, section["parent_id"]) if section["parent_id"] else None
            destination = parent["name"] if parent else "Без раздела"
            text = f"<b>Удалить «{esc(section['name'])}»?</b>\nЗаписи переместятся в «{esc(destination)}»."
            if not parent and await list_sections(db, id_):
                text += "\nПодразделы станут самостоятельными разделами."
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [btn("Удалить раздел", "secdel", id_), btn("Отмена", "secmanage", id_)],
            ])
            return await _show(cb, text, markup, header=header)
        deleted, parent_id = await delete_section(db, id_)
        if not deleted:
            return await cb.answer("Раздел уже удалён.", show_alert=True)
        target = WS(action="section", id=parent_id) if parent_id else WS(action="sections")
        return await workspace_button(cb, target, db, settings, state)
    if a == "return":
        context = await browse_context(db, cb.from_user.id)
        if context and context["action"] == "search":
            await state.clear()
            await cb.answer()
            return await send_search(cb.message, db, settings, context["query"])
        if context:
            target = WS(action=context["action"], id=context["section"], page=context["page"])
        else:
            row = await get_item(db, id_)
            target = WS(action="section", id=row["section_id"]) if row and row["current_section_id"] else WS(action="sections")
        return await workspace_button(cb, target, db, settings, state)
    if a in {"star", "unstar"}:
        await set_favorite(db, id_, a == "star")
        return await workspace_button(cb, WS(action="card", id=id_), db, settings, state)
    if a == "timer":
        row = await db.get_timing(id_)
        if row is None:
            return await cb.answer("Таймер не найден", show_alert=True)
        await state.clear()
        from handlers.timers import render_timer, timer_keyboard
        return await _show(cb, render_timer(row), timer_keyboard(id_))
    if a == "sections":
        await state.clear()
        title, kb = await browse_roots(db)
        return await _show(cb, title, kb, header=HEADER)
    if a == "root":
        await state.clear()
        await cb.answer()
        if isinstance(cb.message, Message):
            await send_menu(cb.message)
        return
    if a in {"section", "items", "active", "card", "deadlines", "favorite", "today", "tomorrow", "soon", "unfiled"}:
        await state.clear()
    if a in {"section", "items", "active", "favorite", "today", "tomorrow", "soon", "unfiled"}:
        section = await get_section(db, id_) if id_ else None
        if id_ and not section:
            return await cb.answer("Раздел больше не существует", show_alert=True)
        await remember_browse(db, cb.from_user.id, a, id_, page)
        text, markup = await section_screen(db, a, section, page, settings)
        header = SectionHeader(id_, section["name"]) if section else HEADER
        return await _show(cb, text, markup, header=header)
    if a == "card":
        row = await get_item(db, id_)
        if not row:
            return await cb.answer("Запись удалена", show_alert=True)
        await cb.answer()
        if isinstance(cb.message, Message):
            return await show_card(cb.message, row, settings,
                                   card_keyboard(id_, bool(row["file_path"])), card_text(row, settings))
        return
    if a == "edit":
        if not await get_item(db, id_):
            return await cb.answer("Запись удалена", show_alert=True)
        kb = InlineKeyboardBuilder()
        for field_id, (_, prompt) in EDIT_FIELDS.items():
            kb.row(btn(f"✏️ {prompt.split(',')[0][:45]}", "input", id_, field_id))
        kb.row(btn("📁 Раздел", "choose", item=id_), btn("↩️ Карточка", "card", id_))
        return await _show(cb, "<b>Что редактировать?</b>", kb.as_markup())
    if a == "input":
        if page not in EDIT_FIELDS or not await get_item(db, id_):
            return await cb.answer("Запись не найдена", show_alert=True)
        field, prompt = EDIT_FIELDS[page]
        await state.set_state(EditInput.value)
        await state.update_data(item_id=id_, field=field)
        await cb.answer()
        await show_text(cb.message, f"Введи {prompt}.\nЧтобы удалить срок, напиши нет. /cancel — отмена.")
        return
    if a == "choose":
        if not await get_item(db, item_id):
            return await cb.answer("Запись удалена", show_alert=True)
        kb = InlineKeyboardBuilder()
        for section in await list_sections(db):
            kb.row(btn(f"📁 {section['name']}", "pickroot", section["id"], item=item_id))
        return await _show(cb, f"<b>Выбери раздел для записи #{item_id}</b>", kb.as_markup())
    if a == "pickroot":
        if not await get_section(db, id_) or not await get_item(db, item_id):
            return await cb.answer("Раздел или запись уже удалены.", show_alert=True)
        kb = InlineKeyboardBuilder()
        kb.row(btn("📁 Сохранить прямо сюда", "setsection", id_, item=item_id))
        for child in await list_sections(db, id_):
            kb.row(btn(f"📂 {child['name']}", "setsection", child["id"], item=item_id))
        kb.row(btn("⬅️ Выбрать другой", "choose", item=item_id))
        return await _show(cb, "Выбери подраздел (или сохрани в корневой раздел).", kb.as_markup())
    if a == "setsection":
        if not await get_section(db, id_):
            return await cb.answer("Раздел уже удалён.", show_alert=True)
        if not await update_item(db, item_id, "section_id", id_):
            return await cb.answer("Запись уже удалена.", show_alert=True)
        await remember_browse(db, cb.from_user.id, "section", id_)
        row = await get_item(db, item_id)
        await cb.answer()
        return await show_card(cb.message, row, settings, card_keyboard(item_id, bool(row["file_path"])),
                               card_text(row, settings))
    if a == "newsec":
        await state.set_state(SectionInput.name)
        await state.update_data(parent_id=id_ or None)
        await cb.answer()
        await show_text(cb.message, "Введи название нового раздела / подраздела. /cancel — отмена.")
        return
    if a == "delask":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [btn("Да, удалить запись", "delete", id_), btn("Отмена", "card", id_)]
        ])
        return await _show(cb, f"Удалить карточку #{id_}? Эта операция необратима для записи в базе. "
                           "Исходный файл останется на диске для защиты от случайной потери.", kb)
    if a == "delete":
        success = await delete_item(db, id_)
        return await _show(cb, "✅ Запись удалена из базы." if success else "Запись уже удалена.")
    if a == "newdeadline":
        await state.set_state(DeadlineInput.title)
        await cb.answer()
        await show_text(cb.message, "Как называется задача? /cancel — отмена.")
        return
    if a == "dedit":
        await state.set_state(DeadlineEditInput.date)
        await state.update_data(deadline_id=id_)
        await cb.answer()
        await show_text(cb.message, "Укажи новую дату дедлайна: 25.11.2026 18:00. /cancel — отмена.")
        return
    if a == "done":
        await set_deadline_done(db, id_, True)
        return await _show(cb, "✅ Дедлайн помечен выполненным.",
                           InlineKeyboardMarkup(inline_keyboard=[[btn("📅 Открыть дедлайны", "deadlines")]]))
    if a == "ddelask":
        return await _show(cb, "Точно удалить дедлайн?", InlineKeyboardMarkup(inline_keyboard=[
            [btn("🗑 Удалить", "ddel", id_), btn("Отмена", "deadlines")]]))
    if a == "ddel":
        await remove_deadline(db, id_)
        return await _show(cb, "✅ Дедлайн удалён.",
                           InlineKeyboardMarkup(inline_keyboard=[[btn("📅 К списку", "deadlines")]]))
    if a == "deadlines":
        text, markup = await deadline_screen(db, settings, page)
        return await _show(cb, text, markup)
    await cb.answer("Неизвестная команда", show_alert=True)


@router.message(Command("reindex"))
async def reindex_old_screens(message: Message, db: Database, settings: Settings) -> None:
    """Переиндексировать до 50 старых скринов; исходные файлы не изменяются."""
    pending = await pending_screens(db, limit=50)
    if not pending:
        await show_text(message, "Все скриншоты уже обработаны OCR или скриншотов нет.")
        return
    await show_text(message, f"Распознаю до {len(pending)} старых скриншотов. Текст добавится в поиск.")
    good, failed = 0, 0
    root = settings.storage_dir.resolve()
    for row in pending:
        file_path = (root / row["file_path"]).resolve()
        if not file_path.is_relative_to(root) or not file_path.is_file():
            failed += 1
            await mark_ocr_processed(db, row["id"])
            continue
        text, hint = await recognize(file_path)
        if text:
            base = row["content"] or ""
            result = (base + "\n\n" + text if base else text)[:settings.max_content_chars]
            await update_item(db, row["id"], "content", result)
            good += 1
        else:
            failed += 1
        if not hint.startswith("OCR не запущен"):
            await mark_ocr_processed(db, row["id"])
    await show_text(message, f"Готово: распознано {good}, без текста/файла {failed}. Повтори /reindex для следующей порции.")
