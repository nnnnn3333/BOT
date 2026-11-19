"""Отчёт по таймингам в .xlsx (openpyxl)."""
from __future__ import annotations

import io
import sqlite3
from typing import Sequence
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from utils import fmt_duration, to_local

STATUS_RU = {
    "new": "не запущен",
    "running": "идёт",
    "paused": "пауза",
    "stopped": "завершён",
}

_HEADER_FONT = Font(bold=True)
_HEADER_FILL = PatternFill("solid", fgColor="E7EBF0")
_DT_FORMAT = "DD.MM.YYYY HH:MM:SS"
_DURATION_FORMAT = "[h]:mm:ss"  # корректно показывает больше 24 часов


def _style_header(sheet, widths: Sequence[int]) -> None:
    for cell in sheet[1]:
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(vertical="center")
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"


def build_weekly_report(rows: Sequence[sqlite3.Row], tz: ZoneInfo) -> bytes:
    """rows — результат Database.timings_since(); возвращает содержимое .xlsx."""
    workbook = Workbook()

    # ---- Сводка по задачам
    summary = workbook.active
    summary.title = "Сводка"
    summary.append(["Задача", "Сессий", "Время", "Часов"])

    totals: dict[str, list] = {}  # ключ -> [отображаемое имя, сессий, секунд]
    for row in rows:
        key = row["task_name"].strip().casefold()
        entry = totals.setdefault(key, [row["task_name"].strip(), 0, 0])
        entry[1] += 1
        entry[2] += row["elapsed_sec"]

    grand_seconds = 0
    for name, count, seconds in sorted(totals.values(), key=lambda e: e[2], reverse=True):
        summary.append([name, count, seconds / 86400, round(seconds / 3600, 2)])
        summary.cell(row=summary.max_row, column=3).number_format = _DURATION_FORMAT
        grand_seconds += seconds

    summary.append(["ИТОГО", len(rows), grand_seconds / 86400, round(grand_seconds / 3600, 2)])
    last = summary.max_row
    summary.cell(row=last, column=3).number_format = _DURATION_FORMAT
    for cell in summary[last]:
        cell.font = _HEADER_FONT
    _style_header(summary, (40, 10, 14, 10))

    # ---- Подробный список
    detail = workbook.create_sheet("Тайминги")
    detail.append(["ID", "Задача", "Начало", "Конец", "Длительность", "Часов", "Статус"])
    for row in rows:
        start = to_local(row["start_time"], tz)
        end = to_local(row["end_time"], tz)
        seconds = row["elapsed_sec"]
        detail.append(
            [
                row["id"],
                row["task_name"],
                start.replace(tzinfo=None) if start else None,  # Excel не знает про tzinfo
                end.replace(tzinfo=None) if end else None,
                seconds / 86400,
                round(seconds / 3600, 2),
                STATUS_RU.get(row["status"], row["status"]),
            ]
        )
        current = detail.max_row
        detail.cell(row=current, column=3).number_format = _DT_FORMAT
        detail.cell(row=current, column=4).number_format = _DT_FORMAT
        detail.cell(row=current, column=5).number_format = _DURATION_FORMAT
    _style_header(detail, (8, 40, 20, 20, 14, 10, 14))

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def total_duration_label(rows: Sequence[sqlite3.Row]) -> str:
    return fmt_duration(sum(r["elapsed_sec"] for r in rows))
