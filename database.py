"""Слой работы с SQLite (aiosqlite): схема, FTS5-индекс, заметки и тайминги."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Sequence

import aiosqlite

# Маркеры подсветки в snippet(): управляющие символы не встречаются в обычном тексте.
HL_OPEN = "\x02"
HL_CLOSE = "\x03"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    category   TEXT NOT NULL CHECK (category IN ('note','file','screen','table','kb')),
    title      TEXT NOT NULL DEFAULT '',
    content    TEXT NOT NULL DEFAULT '',
    file_path  TEXT,                       -- путь относительно storage/
    tags       TEXT NOT NULL DEFAULT '',   -- теги через пробел, без '#', в нижнем регистре
    created_at TEXT NOT NULL DEFAULT (datetime('now'))   -- UTC
);

CREATE INDEX IF NOT EXISTS idx_items_category ON items(category);
CREATE INDEX IF NOT EXISTS idx_items_created  ON items(created_at);

-- Полнотекстовый индекс: external-content таблица, данные берутся из items.
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, content, tags,
    content='items',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
    INSERT INTO items_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
END;

-- Тайминги. accum_sec / resumed_at нужны для паузы:
--   накопленное время + (сейчас - resumed_at), пока статус 'running'.
CREATE TABLE IF NOT EXISTS timings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name    TEXT NOT NULL,
    start_time   TEXT,                      -- UTC, момент первого запуска
    end_time     TEXT,                      -- UTC, момент остановки
    duration_sec INTEGER,                   -- итог после остановки
    status       TEXT NOT NULL DEFAULT 'new'
                 CHECK (status IN ('new','running','paused','stopped')),
    accum_sec    INTEGER NOT NULL DEFAULT 0,
    resumed_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_timings_start ON timings(start_time);
"""

_NOW_EPOCH = "CAST(strftime('%s','now') AS INTEGER)"
_RESUMED_EPOCH = "CAST(strftime('%s', resumed_at) AS INTEGER)"

# Сколько секунд набежало прямо сейчас — считаем в SQL, чтобы не было гонок.
_ELAPSED_SQL = f"""
CASE status
    WHEN 'running' THEN accum_sec + {_NOW_EPOCH} - {_RESUMED_EPOCH}
    WHEN 'stopped' THEN COALESCE(duration_sec, accum_sec)
    ELSE accum_sec
END
"""


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------ lifecycle
    @property
    def path(self) -> Path:
        return self._path

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path)
        conn.row_factory = sqlite3.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        try:
            await conn.executescript(SCHEMA)
        except sqlite3.OperationalError as exc:
            await conn.close()
            if "fts5" in str(exc).lower():
                raise RuntimeError(
                    "Ваша сборка SQLite без FTS5. На Ubuntu штатный python3 её включает; "
                    "проверьте, что используете системный Python, а не урезанную сборку."
                ) from exc
            raise
        await conn.commit()
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База данных не открыта: вызовите connect()")
        return self._conn

    async def _write(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Выполняет UPDATE/DELETE и возвращает число затронутых строк."""
        async with self.conn.execute(sql, params) as cur:
            affected = cur.rowcount
        await self.conn.commit()
        return affected

    # ------------------------------------------------------------------ items
    async def add_item(
        self,
        *,
        category: str,
        title: str,
        content: str = "",
        file_path: str | None = None,
        tags: str = "",
    ) -> int:
        async with self.conn.execute(
            "INSERT INTO items (category, title, content, file_path, tags) VALUES (?, ?, ?, ?, ?)",
            (category, title, content, file_path, tags),
        ) as cur:
            item_id = cur.lastrowid
        await self.conn.commit()
        return int(item_id)

    async def get_item(self, item_id: int) -> sqlite3.Row | None:
        async with self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)) as cur:
            return await cur.fetchone()

    async def search(self, match_query: str, limit: int = 10) -> list[sqlite3.Row]:
        """Поиск по FTS5. match_query — уже собранное выражение (см. utils.build_fts_query)."""
        sql = """
            SELECT i.id, i.category, i.title, i.file_path, i.tags, i.created_at,
                   snippet(items_fts, -1, ?, ?, '…', 14) AS snippet
            FROM items_fts
            JOIN items AS i ON i.id = items_fts.rowid
            WHERE items_fts MATCH ?
            ORDER BY bm25(items_fts, 8.0, 1.0, 4.0)
            LIMIT ?
        """
        async with self.conn.execute(sql, (HL_OPEN, HL_CLOSE, match_query, limit)) as cur:
            return list(await cur.fetchall())

    # ------------------------------------------------------------------ timings
    async def create_timing(self, task_name: str) -> int:
        async with self.conn.execute(
            "INSERT INTO timings (task_name) VALUES (?)", (task_name,)
        ) as cur:
            timing_id = cur.lastrowid
        await self.conn.commit()
        return int(timing_id)

    async def get_timing(self, timing_id: int) -> sqlite3.Row | None:
        sql = f"SELECT *, {_ELAPSED_SQL} AS elapsed_sec FROM timings WHERE id = ?"
        async with self.conn.execute(sql, (timing_id,)) as cur:
            return await cur.fetchone()

    async def start_timing(self, timing_id: int) -> bool:
        """new/paused -> running. False, если переход невозможен."""
        affected = await self._write(
            """
            UPDATE timings
               SET status = 'running',
                   resumed_at = datetime('now'),
                   start_time = COALESCE(start_time, datetime('now'))
             WHERE id = ? AND status IN ('new', 'paused')
            """,
            (timing_id,),
        )
        return affected > 0

    async def pause_timing(self, timing_id: int) -> bool:
        """running -> paused, копит прошедшее время."""
        affected = await self._write(
            f"""
            UPDATE timings
               SET status = 'paused',
                   accum_sec = accum_sec + {_NOW_EPOCH} - {_RESUMED_EPOCH},
                   resumed_at = NULL
             WHERE id = ? AND status = 'running'
            """,
            (timing_id,),
        )
        return affected > 0

    async def stop_timing(self, timing_id: int) -> bool:
        """любой не завершённый статус -> stopped, фиксирует итог."""
        # В SQLite правые части SET видят значения строки ДО обновления.
        affected = await self._write(
            f"""
            UPDATE timings
               SET duration_sec = accum_sec
                       + CASE WHEN status = 'running'
                              THEN {_NOW_EPOCH} - {_RESUMED_EPOCH} ELSE 0 END,
                   start_time = COALESCE(start_time, datetime('now')),
                   end_time = datetime('now'),
                   resumed_at = NULL,
                   status = 'stopped'
             WHERE id = ? AND status != 'stopped'
            """,
            (timing_id,),
        )
        return affected > 0

    async def timings_since(self, days: int = 7) -> list[sqlite3.Row]:
        sql = f"""
            SELECT *, {_ELAPSED_SQL} AS elapsed_sec
              FROM timings
             WHERE start_time IS NOT NULL AND start_time >= datetime('now', ?)
             ORDER BY start_time
        """
        async with self.conn.execute(sql, (f"-{int(days)} days",)) as cur:
            return list(await cur.fetchall())

    # ------------------------------------------------------------------ backup
    async def snapshot(self, dest: Path) -> None:
        """Консистентная копия БД (VACUUM INTO) — безопасно при работающем боте и WAL."""
        dest.unlink(missing_ok=True)
        await self.conn.commit()
        await self.conn.execute("VACUUM INTO ?", (str(dest),))
