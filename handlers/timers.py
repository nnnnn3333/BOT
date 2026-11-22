"""/timer (Старт / Пауза / Стоп) и /stats (отчёт за неделю в .xlsx)."""
from __future__ import annotations

import asyncio
from datetime import datetime
import sqlite3

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Settings
from database import Database
from reports import build_weekly_report, total_duration_label
from handlers.ui import show_text
from utils import esc, fmt_duration

router = Router(name="timers")

STATUS_LABELS = {
    "new": "🆕 не запущен",
    "running": "▶️ идёт",
    "paused": "⏸ на паузе",
    "stopped": "⏹ завершён",
}
REPORT_DAYS = 7


class TimerCb(CallbackData, prefix="tm"):
    action: str  # "start" | "pause" | "stop"
    id: int


def timer_keyboard(timing_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="▶️ Старт", callback_data=TimerCb(action="start", id=timing_id))
    builder.button(text="⏸ Пауза", callback_data=TimerCb(action="pause", id=timing_id))
    builder.button(text="⏹ Стоп", callback_data=TimerCb(action="stop", id=timing_id))
    builder.adjust(3)
    return builder.as_markup()


def render_timer(row: sqlite3.Row) -> str:
    label = "Итого" if row["status"] == "stopped" else "Затрачено"
    text = (
        f"<b>⏱ {esc(row['task_name'])}</b>\n\n"
        f"Статус: {STATUS_LABELS[row['status']]}\n"
        f"{label}: {fmt_duration(row['elapsed_sec'])}"
    )
    if row["status"] == "running":
        text += "\nВремя на момент обновления; «Старт» обновит его ещё раз."
    return text


@router.message(Command("timer"))
async def cmd_timer(message: Message, command: CommandObject, db: Database) -> None:
    name = " ".join((command.args or "").split())
    if not name:
        await show_text(message, "Использование: /timer <code>название задачи</code>")
        return

    timing_id = await db.create_timing(name[:200])
    row = await db.get_timing(timing_id)
    await show_text(message, render_timer(row), reply_markup=timer_keyboard(timing_id))


@router.callback_query(TimerCb.filter())
async def on_timer_button(cb: CallbackQuery, callback_data: TimerCb, db: Database) -> None:
    timing_id = callback_data.id
    before = await db.get_timing(timing_id)
    if before is None:
        await cb.answer("Таймер не найден", show_alert=True)
        return

    toast: str | None = None
    if callback_data.action == "start":
        if not await db.start_timing(timing_id):
            toast = "Таймер уже идёт — время обновлено" if before["status"] == "running" else "Таймер уже завершён"
    elif callback_data.action == "pause":
        if not await db.pause_timing(timing_id):
            toast = {
                "paused": "Уже на паузе",
                "new": "Сначала нажмите «Старт»",
                "stopped": "Таймер уже завершён",
            }.get(before["status"])
    elif callback_data.action == "stop":
        if not await db.stop_timing(timing_id):
            toast = "Таймер уже завершён"

    row = await db.get_timing(timing_id)
    if isinstance(cb.message, Message):
        markup = None if row["status"] == "stopped" else timer_keyboard(timing_id)
        try:
            await cb.message.edit_text(render_timer(row), reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise
    await cb.answer(toast)


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database, settings: Settings) -> None:
    rows = await db.timings_since(days=REPORT_DAYS)
    if not rows:
        await show_text(message, f"За последние {REPORT_DAYS} дней таймингов нет.")
        return

    data = await asyncio.to_thread(build_weekly_report, rows, settings.tz)
    today = datetime.now(settings.tz).strftime("%Y-%m-%d")
    await message.answer_document(
        BufferedInputFile(data, filename=f"timings_{today}.xlsx"),
        caption=f"📊 Тайминги за {REPORT_DAYS} дней · всего {total_duration_label(rows)}",
    )
