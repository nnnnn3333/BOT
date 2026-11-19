"""Расширения Workbot: дерево разделов, метаданные, дедлайны.

Не меняет и не пересоздаёт исходные items/items_fts/timings.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from database import Database
from utils import build_fts_query, esc

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES work_sections(id) ON DELETE SET NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_work_sections_parent ON work_sections(parent_id, sort_order);
CREATE TABLE IF NOT EXISTS work_item_meta (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    section_id INTEGER REFERENCES work_sections(id) ON DELETE SET NULL,
    price TEXT NOT NULL DEFAULT '',
    valid_until TEXT,
    details TEXT NOT NULL DEFAULT '',
    ocr_processed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_work_item_meta_section ON work_item_meta(section_id);
CREATE INDEX IF NOT EXISTS ix_work_item_meta_valid ON work_item_meta(valid_until);
CREATE TABLE IF NOT EXISTS work_deadlines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    due_at TEXT NOT NULL,
    item_id INTEGER UNIQUE REFERENCES items(id) ON DELETE SET NULL,
    done INTEGER NOT NULL DEFAULT 0,
    sent_day INTEGER NOT NULL DEFAULT 0,
    sent_hour INTEGER NOT NULL DEFAULT 0,
    sent_due INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_work_deadlines_due ON work_deadlines(done, due_at);
"""

DEFAULT_SECTIONS = {
    "Товары и цены": ("Смартфоны", "Аксессуары", "SIM-карты"),
    "Акции": ("Действующие", "Предстоящие", "Завершённые"),
    "Инструкции": ("Продажи", "Оформление", "Возвраты"),
    "Документы": ("Таблицы", "Файлы"),
    "Другое": (),
}


async def initialize(db: Database) -> None:
    await db.conn.executescript(SCHEMA)
    # Совместимо с базами, которые уже получили раннюю версию расширения.
    async with db.conn.execute("PRAGMA table_info(work_item_meta)") as cur:
        columns = {row[1] for row in await cur.fetchall()}
    if "ocr_processed" not in columns:
        await db.conn.execute("ALTER TABLE work_item_meta ADD COLUMN ocr_processed INTEGER NOT NULL DEFAULT 0")
    async with db.conn.execute("SELECT COUNT(*) FROM work_sections") as cur:
        count = (await cur.fetchone())[0]
    if count == 0:
        for order, (parent, children) in enumerate(DEFAULT_SECTIONS.items()):
            section_id = await create_section(db, parent, None, order)
            for idx, child in enumerate(children):
                await create_section(db, child, section_id, idx)
    await db.conn.commit()


async def create_section(db: Database, name: str, parent_id: int | None = None, order: int = 0) -> int:
    name = " ".join(name.strip().split())[:70]
    if not name:
        raise ValueError("Название не может быть пустым")
    if parent_id is not None:
        parent = await get_section(db, parent_id)
        if parent is None or parent["parent_id"] is not None:
            raise ValueError("Подразделы могут быть только у корневых разделов")
    async with db.conn.execute(
        "SELECT id FROM work_sections WHERE parent_id IS ? AND lower(name)=lower(?)", (parent_id, name)
    ) as cur:
        existing = await cur.fetchone()
    if existing:
        return int(existing["id"])
    async with db.conn.execute(
        "INSERT INTO work_sections(name,parent_id,sort_order) VALUES (?,?,?)", (name, parent_id, order)
    ) as cur:
        section_id = cur.lastrowid
    await db.conn.commit()
    return int(section_id)


async def get_section(db: Database, section_id: int):
    async with db.conn.execute("SELECT * FROM work_sections WHERE id=?", (section_id,)) as cur:
        return await cur.fetchone()


async def list_sections(db: Database, parent_id: int | None = None):
    async with db.conn.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM work_item_meta m WHERE m.section_id=s.id) AS count_items "
        "FROM work_sections s WHERE s.parent_id IS ? ORDER BY s.sort_order, s.name", (parent_id,)
    ) as cur:
        return list(await cur.fetchall())


async def get_item(db: Database, item_id: int):
    async with db.conn.execute("""
        SELECT i.*, m.section_id, COALESCE(m.price,'') AS price,
               m.valid_until, COALESCE(m.details,'') AS details,
               COALESCE(s.name,'Без раздела') AS section_name,
               s.parent_id AS section_parent_id
        FROM items i LEFT JOIN work_item_meta m ON m.item_id=i.id
        LEFT JOIN work_sections s ON s.id=m.section_id
        WHERE i.id=?
    """, (item_id,)) as cur:
        return await cur.fetchone()


async def update_item(db: Database, item_id: int, field: str, value: Any) -> bool:
    # Поля выбираются только из фиксированного белого списка, не из ввода Telegram.
    if field not in {"title", "content", "tags", "section_id", "price", "valid_until", "details"}:
        raise ValueError("Неизвестное поле")
    if await db.get_item(item_id) is None:
        return False
    if field == "section_id" and value is not None and not await get_section(db, int(value)):
        raise ValueError("Раздел не найден")
    if field in {"section_id", "price", "valid_until", "details"}:
        await db.conn.execute("INSERT OR IGNORE INTO work_item_meta(item_id) VALUES (?)", (item_id,))
        await db.conn.execute(f"UPDATE work_item_meta SET {field}=? WHERE item_id=?", (value, item_id))
    else:
        await db.conn.execute(f"UPDATE items SET {field}=? WHERE id=?", (value, item_id))
    await db.conn.commit()
    return True


async def delete_item(db: Database, item_id: int) -> bool:
    if await db.get_item(item_id) is None:
        return False
    # Файл оставляем до ручной очистки: случайное нажатие не уничтожит оригинал скриншота.
    await db.conn.execute("DELETE FROM work_deadlines WHERE item_id=?", (item_id,))
    await db.conn.execute("DELETE FROM work_item_meta WHERE item_id=?", (item_id,))
    await db.conn.execute("DELETE FROM items WHERE id=?", (item_id,))
    await db.conn.commit()
    return True


async def list_items(db: Database, section_id: int | None = None, page: int = 0, limit: int = 8,
                     active_only: bool = False):
    sql = """SELECT i.id,i.title,i.category,m.price,m.valid_until
        FROM items i LEFT JOIN work_item_meta m ON m.item_id=i.id
        WHERE 1=1"""
    params: list[Any] = []
    if section_id is not None:
        # При открытии корневого раздела также показываем карточки его подразделов.
        sql += " AND (m.section_id=? OR m.section_id IN (SELECT id FROM work_sections WHERE parent_id=?))"
        params.extend((section_id, section_id))
    if active_only:
        sql += " AND m.valid_until IS NOT NULL AND m.valid_until>=date('now')"
    sql += " ORDER BY i.id DESC LIMIT ? OFFSET ?"
    params.extend((limit + 1, max(0, page) * limit))
    async with db.conn.execute(sql, params) as cur:
        rows = list(await cur.fetchall())
    return rows[:limit], len(rows) > limit


async def search_items(db: Database, raw: str, limit: int = 15, active_only: bool = False):
    """FTS по документам/распознанным скриншотам + поиск по метаданным и датам."""
    raw = raw.strip()[:150]
    if not raw:
        return []
    ids: list[int] = []
    fts = build_fts_query(raw)
    if fts:
        try:
            for row in await db.search(fts, limit=60):
                ids.append(row["id"])
        except sqlite3.Error:
            log.warning("FTS query failed: %s", raw, exc_info=True)
    query = f"%{raw}%"
    sql = """SELECT i.id FROM items i
        LEFT JOIN work_item_meta m ON m.item_id=i.id
        LEFT JOIN work_sections s ON s.id=m.section_id
        WHERE i.title LIKE ? OR i.content LIKE ? OR i.tags LIKE ?
           OR m.price LIKE ? OR m.valid_until LIKE ? OR m.details LIKE ? OR s.name LIKE ?
           OR i.created_at LIKE ? OR strftime('%d.%m.%Y', i.created_at) LIKE ?
        ORDER BY i.id DESC LIMIT 60"""
    async with db.conn.execute(sql, (query,) * 9) as cur:
        for row in await cur.fetchall():
            if row["id"] not in ids:
                ids.append(row["id"])
    # SQLite LIKE не делает Unicode casefold для кириллицы. Не читаем огромный
    # OCR-текст целиком: он уже покрыт индексом FTS5, ниже проверяем только метаданные.
    async with db.conn.execute("""SELECT i.id, i.title, i.tags, m.price, m.valid_until,
               m.details, s.name AS section_name
        FROM items i LEFT JOIN work_item_meta m ON m.item_id=i.id
        LEFT JOIN work_sections s ON s.id=m.section_id
        ORDER BY i.id DESC LIMIT 10000""") as cur:
        for row in await cur.fetchall():
            if row["id"] not in ids and raw.casefold() in " ".join(
                str(row[k] or "") for k in ("title", "tags", "price", "valid_until", "details", "section_name")
            ).casefold():
                ids.append(row["id"])
    results = []
    today = datetime.now(timezone.utc).date().isoformat()
    for item_id in ids:
        item = await get_item(db, item_id)
        if item and (not active_only or item["valid_until"] is None or item["valid_until"] >= today):
            results.append(item)
        if len(results) >= limit:
            break
    return results


def parse_datetime(raw: str, tz, end_of_day: bool = False) -> datetime:
    """ДД.ММ.ГГГГ [ЧЧ:ММ], ДД.ММ [ЧЧ:ММ], ГГГГ-ММ-ДД [ЧЧ:ММ]."""
    chunks = raw.strip().split()
    if len(chunks) not in (1, 2):
        raise ValueError("Введите дату в формате 25.11.2026 18:00")
    part = chunks[0]
    if '-' in part:
        date = datetime.strptime(part, "%Y-%m-%d").date()
    elif part.count('.') == 2:
        date = datetime.strptime(part, "%d.%m.%Y").date()
    elif part.count('.') == 1:
        now = datetime.now(tz)
        date = datetime.strptime(f"{part}.{now.year}", "%d.%m.%Y").date()
        if date < now.date():
            try:
                date = date.replace(year=date.year + 1)
            except ValueError:
                raise ValueError("Для 29 февраля укажите год полностью") from None
    else:
        raise ValueError("Введите дату в формате 25.11.2026")
    hh, mm = (23, 59) if end_of_day else (12, 0)
    if len(chunks) == 2:
        parsed = datetime.strptime(chunks[1], "%H:%M")
        hh, mm = parsed.hour, parsed.minute
    return datetime(date.year, date.month, date.day, hh, mm, tzinfo=tz)


async def add_deadline(db: Database, title: str, when: datetime, item_id: int | None = None) -> int:
    utc_str = when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if item_id is None:
        async with db.conn.execute("INSERT INTO work_deadlines(title,due_at) VALUES (?,?)", (title, utc_str)) as cur:
            identifier = cur.lastrowid
    else:
        await db.conn.execute("""INSERT INTO work_deadlines(title,due_at,item_id) VALUES (?,?,?)
            ON CONFLICT(item_id) DO UPDATE SET title=excluded.title,due_at=excluded.due_at,
            done=0,sent_day=0,sent_hour=0,sent_due=0""", (title, utc_str, item_id))
        async with db.conn.execute("SELECT id FROM work_deadlines WHERE item_id=?", (item_id,)) as cur:
            identifier = (await cur.fetchone())[0]
    await db.conn.commit()
    return int(identifier)


async def list_deadlines(db: Database, include_done: bool = False, limit: int = 35):
    where = "" if include_done else "WHERE done=0"
    async with db.conn.execute(
        f"SELECT * FROM work_deadlines {where} ORDER BY done,due_at LIMIT ?", (limit,)
    ) as cur:
        return list(await cur.fetchall())


async def set_deadline_done(db: Database, deadline_id: int, done: bool) -> bool:
    async with db.conn.execute("UPDATE work_deadlines SET done=? WHERE id=?", (int(done), deadline_id)) as cur:
        changed = cur.rowcount
    await db.conn.commit()
    return changed > 0


async def remove_deadline(db: Database, deadline_id: int) -> None:
    await db.conn.execute("DELETE FROM work_deadlines WHERE id=?", (deadline_id,))
    await db.conn.commit()


async def notify_deadlines(bot, db: Database, settings) -> None:
    """Проверяется каждую минуту; флаги в SQLite сохраняются после перезапуска."""
    async with db.conn.execute("""SELECT * FROM work_deadlines WHERE done=0 AND sent_due=0
        ORDER BY due_at LIMIT 200""") as cur:
        rows = await cur.fetchall()
    now = datetime.now(timezone.utc)
    for row in rows:
        due = datetime.strptime(row["due_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        secs = (due - now).total_seconds()
        # Не дублируем уведомления; если VPS был выключен, уведомляем и после истечения срока.
        if secs <= 0 and not row["sent_due"]:
            field, label = "sent_due", "Срок наступил"
        elif 0 < secs <= 3600 and not row["sent_hour"]:
            field, label = "sent_hour", "Остался час"
        elif 3600 < secs <= 86400 and not row["sent_day"]:
            field, label = "sent_day", "Осталось менее суток"
        else:
            continue
        formatted = due.astimezone(settings.tz).strftime("%d.%m.%Y %H:%M")
        try:
            await bot.send_message(settings.admin_id, f"⏰ <b>{label}</b>\n{esc(row['title'])}\n{formatted}")
        except Exception:
            log.exception("Не отправлено напоминание о дедлайне %s", row["id"])
            continue
        if field == "sent_due":
            await db.conn.execute("UPDATE work_deadlines SET sent_due=1,sent_day=1,sent_hour=1 WHERE id=?", (row["id"],))
        else:
            await db.conn.execute(f"UPDATE work_deadlines SET {field}=1 WHERE id=?", (row["id"],))
        await db.conn.commit()


async def mark_ocr_processed(db: Database, item_id: int) -> None:
    await db.conn.execute("INSERT OR IGNORE INTO work_item_meta(item_id) VALUES (?)", (item_id,))
    await db.conn.execute("UPDATE work_item_meta SET ocr_processed=1 WHERE item_id=?", (item_id,))
    await db.conn.commit()


async def pending_screens(db: Database, limit: int = 50):
    async with db.conn.execute("""SELECT i.id,i.content,i.file_path FROM items i
       LEFT JOIN work_item_meta m ON m.item_id=i.id
       WHERE i.category='screen' AND i.file_path IS NOT NULL
         AND COALESCE(m.ocr_processed,0)=0 ORDER BY i.id LIMIT ?""", (limit,)) as cur:
        return list(await cur.fetchall())
