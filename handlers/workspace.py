"""Кнопочное меню Workbot: категории, карточки, поиск, сроки, редактирование."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from ocr_local import recognize

from aiogram import F, Router, Bot
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
from workspace_db import (add_deadline, create_section, delete_item, get_item, get_section,
                          list_deadlines, list_items, list_sections, parse_datetime,
                          remove_deadline, search_items, set_deadline_done, update_item,
                          mark_ocr_processed, pending_screens)

router = Router(name="workspace")

MENU = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🔎 Поиск"), KeyboardButton(text="🗂 База знаний")],
    [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="📅 Дедлайны")],
    [KeyboardButton(text="⏱ Таймеры"), KeyboardButton(text="📊 Отчёт")],
], resize_keyboard=True, input_field_placeholder="Перешли скрин, документ или заметку")


class SearchInput(StatesGroup):
    query = State()


class EditInput(StatesGroup):
    value = State()


class SectionInput(StatesGroup):
    name = State()


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


def card_keyboard(item_id: int, has_file: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [btn("✏️ Редактировать", "edit", item_id), btn("📁 Переместить", "choose", item=item_id)],
        [btn("📅 Срок действия", "input", item_id, page=2), btn("💰 Цена", "input", item_id, page=1)],
    ]
    if has_file:
        from handlers.search import ItemCb
        rows.append([InlineKeyboardButton(text="📎 Оригинал", callback_data=ItemCb(action="file", id=item_id).pack())])
    rows.append([btn("🗑 Удалить", "delask", item_id), btn("🏠 В меню", "root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _short(value: str, max_len: int = 65) -> str:
    text = " ".join((value or "").split())
    return text if len(text) <= max_len else text[:max_len - 1] + "…"


def card_text(row, settings: Settings) -> str:
    lines = [f"<b>#{row['id']} · {esc(row['title'])}</b>", f"📁 {esc(row['section_name'])}"]
    if row["price"]:
        lines.append(f"💰 {esc(row['price'])}")
    if row["valid_until"]:
        today = datetime.now(settings.tz).date().isoformat()
        marker = "🔴 Истёк" if row["valid_until"] < today else "🟢 Действует"
        lines.append(f"📅 До {esc(row['valid_until'])} · {marker}")
    if row["details"]:
        lines.append(f"📋 {esc(_short(row['details'], 400))}")
    if row["content"]:
        lines.append("\n<b>Содержимое:</b>\n" + esc(_short(row["content"], 900)))
    else:
        lines.append("\nТекст не указан")
    return "\n".join(lines)


async def _show(cb: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if isinstance(cb.message, Message):
        try:
            await cb.message.edit_text(text[:4000], reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                await cb.message.answer(text[:4000], reply_markup=markup)
    await cb.answer()


async def send_menu(message: Message) -> None:
    await message.answer(
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


@router.message(Command("cancel"))
@router.message(F.text == "❌ Отмена")
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=MENU)


async def browse_roots(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    for section in await list_sections(db):
        kb.row(btn(f"📁 {section['name']}", "section", section["id"]))
    kb.row(btn("🕓 Последние записи", "items"), btn("🟢 Сроки действуют", "active"))
    kb.row(btn("➕ Новый раздел", "newsec"))
    return "<b>🗂 База знаний</b>\nВыбери раздел или открой последние записи.", kb.as_markup()


@router.message(F.text == "🗂 База знаний")
@router.message(Command("sections"))
async def cmd_sections(message: Message, db: Database) -> None:
    text, markup = await browse_roots(db)
    await message.answer(text, reply_markup=markup)


@router.message(F.text == "➕ Добавить")
async def cmd_add(message: Message) -> None:
    await message.answer(
        "<b>Новая запись</b>\n\nПерешли сообщение, скриншот или файл в этот чат. "
        "Бот сохранит оригинал, извлечёт текст (если возможно) и покажет кнопки для заполнения карточки.\n\n"
        "Если отправляешь несколько скринов, каждый сохранится отдельно."
    )


@router.message(F.text == "🔎 Поиск")
async def ask_search(message: Message, state: FSMContext) -> None:
    await state.set_state(SearchInput.query)
    await message.answer("Введи товар, цену, название акции, условие или дату (например, 2026-12-01).\n/cancel — отмена.")


async def send_search(message: Message, db: Database, settings: Settings, query: str) -> None:
    results = await search_items(db, query)
    if not results:
        await message.answer(f"По запросу «{esc(query)}» ничего не нашлось.")
        return
    kb = InlineKeyboardBuilder()
    for row in results:
        kb.row(btn(f"📄 {_short(row['title'], 40)} · #{row['id']}", "card", row["id"]))
    await message.answer(f"🔎 <b>Поиск: {esc(query)}</b>\nНайдено: {len(results)}\n\n" +
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
        await message.answer("Пришли текст для поиска.")
        return
    await send_search(message, db, settings, message.text.strip()[:150])


async def show_deadlines(message: Message, db: Database, settings: Settings) -> None:
    rows = await list_deadlines(db)
    kb = InlineKeyboardBuilder()
    for row in rows:
        due = datetime.strptime(row["due_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        local = due.astimezone(settings.tz)
        kb.row(btn(f"✅ #{row['id']}", "done", row["id"]),
               btn("✏️", "dedit", row["id"]), btn("🗑", "ddelask", row["id"]))
    text_lines = ["<b>📅 Дедлайны</b>"]
    for row in rows:
        due = datetime.strptime(row["due_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        local = due.astimezone(settings.tz)
        remain = due - datetime.now(timezone.utc)
        marker = "🔴" if remain.total_seconds() < 0 else "⏳"
        hours = int(abs(remain.total_seconds()) // 3600)
        countdown = f"{hours // 24} д. {hours % 24} ч."
        status = "просрочено на " if remain.total_seconds() < 0 else "осталось "
        text_lines.append(f"{marker} <b>#{row['id']} {esc(_short(row['title'], 90))}</b> · {local:%d.%m.%Y %H:%M} ({status}{countdown})")
    if not rows:
        text_lines.append("Пока нет активных дедлайнов.")
    kb.row(btn("➕ Добавить дедлайн", "newdeadline"), btn("🏠 Назад", "root"))
    await message.answer("\n".join(text_lines)[:4000], reply_markup=kb.as_markup())


@router.message(F.text == "📅 Дедлайны")
@router.message(Command("deadlines"))
async def deadlines_command(message: Message, db: Database, settings: Settings) -> None:
    await show_deadlines(message, db, settings)


@router.message(Command("deadline"))
async def new_deadline_command(message: Message, state: FSMContext) -> None:
    await state.set_state(DeadlineInput.title)
    await message.answer("Как называется задача?\nНапример: Отправить отчёт по продажам. /cancel — отмена.")


@router.message(DeadlineInput.title)
async def deadline_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()[:200]
    if not title:
        await message.answer("Введи название текстом")
        return
    await state.update_data(title=title)
    await state.set_state(DeadlineInput.date)
    await message.answer("Укажи срок: <code>25.11.2026 18:00</code>\nЕсли время не указано, будет 12:00. /cancel — отмена.")


@router.message(DeadlineEditInput.date)
async def deadline_edit_date(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    try:
        when = parse_datetime(message.text or "", settings.tz)
        if when.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise ValueError("Укажите будущее время")
    except ValueError as exc:
        await message.answer(f"Ошибка даты: {esc(exc)}. Пример: 25.11.2026 18:00")
        return
    data = await state.get_data()
    await db.conn.execute("""UPDATE work_deadlines SET due_at=?,sent_day=0,sent_hour=0,sent_due=0,done=0
                            WHERE id=?""", (when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), data["deadline_id"]))
    await db.conn.commit()
    await state.clear()
    await message.answer(f"✅ Дедлайн перенесён на {when:%d.%m.%Y %H:%M}", reply_markup=MENU)


@router.message(DeadlineInput.date)
async def deadline_date(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    try:
        when = parse_datetime(message.text or "", settings.tz)
        if when.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise ValueError("Время уже прошло")
    except ValueError as exc:
        await message.answer(f"Ошибка даты: {esc(exc)}. Пример: <code>25.11.2026 18:00</code>")
        return
    data = await state.get_data()
    await add_deadline(db, data["title"], when)
    await state.clear()
    await message.answer(f"✅ Дедлайн создан: {esc(data['title'])}\n{when:%d.%m.%Y %H:%M}", reply_markup=MENU)


@router.message(F.text == "⏱ Таймеры")
async def cmd_timer_menu(message: Message, db: Database, state: FSMContext) -> None:
    async with db.conn.execute(
        "SELECT id,task_name,status FROM timings WHERE status IN ('new','running','paused') ORDER BY id DESC LIMIT 6"
    ) as cur:
        rows = list(await cur.fetchall())
    if rows:
        from handlers.timers import render_timer, timer_keyboard
        for record in rows:
            row = await db.get_timing(record["id"])
            await message.answer(render_timer(row), reply_markup=timer_keyboard(row["id"]))
    await state.set_state(TimerInput.name)
    await message.answer("Чтобы создать новый таймер, введи название задачи. /cancel — отмена.")


@router.message(TimerInput.name)
async def timer_name(message: Message, db: Database, state: FSMContext) -> None:
    name = (message.text or "").strip()[:200]
    if not name:
        await message.answer("Нужно название задачи.")
        return
    from handlers.timers import render_timer, timer_keyboard
    timer_id = await db.create_timing(name)
    await state.clear()
    await message.answer(render_timer(await db.get_timing(timer_id)), reply_markup=timer_keyboard(timer_id))


@router.message(F.text == "📊 Отчёт")
async def stats_menu(message: Message, db: Database, settings: Settings) -> None:
    from handlers.timers import cmd_stats
    await cmd_stats(message, db, settings)


@router.message(SectionInput.name)
async def section_name(message: Message, state: FSMContext, db: Database) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Раздел должен иметь название.")
        return
    data = await state.get_data()
    try:
        section_id = await create_section(db, name, data.get("parent_id"))
    except ValueError as exc:
        await message.answer(esc(exc))
        return
    await state.clear()
    await message.answer(f"✅ Раздел создан: {esc(name)} (#{section_id})", reply_markup=MENU)


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
        await message.answer("Действие истекло, открой карточку ещё раз.")
        return
    raw = (message.text or "").strip()
    if field == "valid_until":
        if raw.lower() in {"нет", "-", "удалить"}:
            value = None
        else:
            try:
                value = parse_datetime(raw, settings.tz, end_of_day=True).date().isoformat()
            except ValueError as exc:
                await message.answer("Не понял дату: " + esc(exc) + ". Например, 25.11.2026")
                return
    elif field == "title":
        value = raw[:150]
        if not value:
            await message.answer("Название не может быть пустым")
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
    await message.answer("✅ Карточка обновлена.\n\n" + card_text(item, settings),
                         reply_markup=card_keyboard(item_id, bool(item["file_path"])))


@router.callback_query(WS.filter())
async def workspace_button(cb: CallbackQuery, callback_data: WS, db: Database,
                           settings: Settings, state: FSMContext) -> None:
    a, id_, page, item_id = callback_data.action, callback_data.id, callback_data.page, callback_data.item
    if a == "root":
        title, kb = await browse_roots(db)
        return await _show(cb, title, kb)
    if a == "section":
        section = await get_section(db, id_)
        if not section:
            return await cb.answer("Раздел больше не существует", show_alert=True)
        kb = InlineKeyboardBuilder()
        if section["parent_id"] is None:
            for child in await list_sections(db, id_):
                kb.row(btn(f"📂 {child['name']}", "section", child["id"]))
            kb.row(btn("➕ Подраздел", "newsec", id_))
        kb.row(btn("📄 Показать записи", "items", id_))
        kb.row(btn("↩️ Назад", "root" if section["parent_id"] is None else "section", section["parent_id"] or 0))
        return await _show(cb, f"<b>📁 {esc(section['name'])}</b>\nВыбери подраздел или записи.", kb.as_markup())
    if a in {"items", "active"}:
        root = None if id_ == 0 else id_
        rows, more = await list_items(db, section_id=root, page=page, active_only=(a == "active"))
        kb = InlineKeyboardBuilder()
        for row in rows:
            kb.row(btn(f"#{row['id']} · {_short(row['title'], 40)}", "card", row["id"]))
        arrows = []
        if page > 0:
            arrows.append(btn("⬅️ Назад", a, id_, page - 1))
        if more:
            arrows.append(btn("Далее ➡️", a, id_, page + 1))
        if arrows:
            kb.row(*arrows)
        kb.row(btn("🏠 Разделы", "root"))
        title = "🟢 Действующие предложения" if a == "active" else "📄 Записи"
        return await _show(cb, f"<b>{title} · страница {page+1}</b>\n" +
                           ("Выбери карточку." if rows else "Записей пока нет."), kb.as_markup())
    if a == "card":
        row = await get_item(db, id_)
        if not row:
            return await cb.answer("Запись удалена", show_alert=True)
        return await _show(cb, card_text(row, settings), card_keyboard(id_, bool(row["file_path"])))
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
        await cb.message.answer(f"Введи {prompt}.\nЧтобы удалить срок, напиши <code>нет</code>. /cancel — отмена.")
        return
    if a == "choose":
        if not await get_item(db, item_id):
            return await cb.answer("Запись удалена", show_alert=True)
        kb = InlineKeyboardBuilder()
        for section in await list_sections(db):
            kb.row(btn(f"📁 {section['name']}", "pickroot", section["id"], item=item_id))
        return await _show(cb, f"<b>Выбери раздел для записи #{item_id}</b>", kb.as_markup())
    if a == "pickroot":
        kb = InlineKeyboardBuilder()
        kb.row(btn("📁 Сохранить прямо сюда", "setsection", id_, item=item_id))
        for child in await list_sections(db, id_):
            kb.row(btn(f"📂 {child['name']}", "setsection", child["id"], item=item_id))
        kb.row(btn("⬅️ Выбрать другой", "choose", item=item_id))
        return await _show(cb, "Выбери подраздел (или сохрани в корневой раздел).", kb.as_markup())
    if a == "setsection":
        await update_item(db, item_id, "section_id", id_)
        row = await get_item(db, item_id)
        return await _show(cb, "✅ Раздел изменён\n\n" + card_text(row, settings),
                           card_keyboard(item_id, bool(row["file_path"])))
    if a == "newsec":
        await state.set_state(SectionInput.name)
        await state.update_data(parent_id=id_ or None)
        await cb.answer()
        await cb.message.answer("Введи название нового раздела / подраздела. /cancel — отмена.")
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
        await cb.message.answer("Как называется задача? /cancel — отмена.")
        return
    if a == "dedit":
        await state.set_state(DeadlineEditInput.date)
        await state.update_data(deadline_id=id_)
        await cb.answer()
        await cb.message.answer("Укажи новую дату дедлайна: 25.11.2026 18:00. /cancel — отмена.")
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
        # Callback-friendly rendering using same logic as the message route.
        rows = await list_deadlines(db)
        kb = InlineKeyboardBuilder()
        lines = ["<b>📅 Дедлайны</b>"]
        for row in rows:
            due = datetime.strptime(row["due_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            local = due.astimezone(settings.tz)
            lines.append(f"• #{row['id']} {esc(_short(row['title'], 75))} — {local:%d.%m %H:%M}")
            kb.row(btn(f"✅ #{row['id']}", "done", row["id"]),
                   btn("✏️", "dedit", row["id"]), btn("🗑", "ddelask", row["id"]))
        if not rows:
            lines.append("Нет активных дедлайнов")
        kb.row(btn("➕ Новый", "newdeadline"), btn("🏠 Меню", "root"))
        return await _show(cb, "\n".join(lines), kb.as_markup())
    await cb.answer("Неизвестная команда", show_alert=True)


@router.message(Command("reindex"))
async def reindex_old_screens(message: Message, db: Database, settings: Settings) -> None:
    """Переиндексировать до 50 старых скринов; исходные файлы не изменяются."""
    pending = await pending_screens(db, limit=50)
    if not pending:
        await message.answer("Все скриншоты уже обработаны OCR или скриншотов нет.")
        return
    await message.answer(f"Распознаю до {len(pending)} старых скриншотов. Текст добавится в поиск.")
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
    await message.answer(f"Готово: распознано {good}, без текста/файла {failed}. Повтори /reindex для следующей порции.")
