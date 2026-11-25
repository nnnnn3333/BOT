"""Save SQLite and working files as a verified server-side archive."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

from config import Settings
from database import Database

log = logging.getLogger(__name__)
_lock = asyncio.Lock()


def _write_zip(archive: Path, snapshot: Path, storage_dir: Path) -> None:
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(snapshot, "work_base.db")
        if storage_dir.exists():
            for path in sorted(storage_dir.rglob("*")):
                if path.is_file():
                    zf.write(path, Path("storage") / path.relative_to(storage_dir))


def _create_archive(archive: Path, snapshot: Path, storage: Path) -> None:
    with closing(sqlite3.connect(snapshot)) as conn:
        if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Резервная база не прошла проверку")
    _write_zip(archive, snapshot, storage)
    with zipfile.ZipFile(archive) as checked:
        if checked.testzip() is not None:
            raise ValueError("Резервный архив не прошёл проверку")


async def save_backup(db: Database, settings: Settings) -> Path | None:
    if _lock.locked():
        return None
    async with _lock:
        destination = settings.backup_dir.resolve()
        if destination.is_relative_to(settings.storage_dir.resolve()):
            raise ValueError("BACKUP_DIR должен находиться вне STORAGE_DIR")
        destination.mkdir(parents=True, exist_ok=True)
        now = datetime.now(settings.tz)
        filename = f"workbot_backup_{now:%Y-%m-%d_%H%M%S_%f}.zip"
        archive = destination / filename
        with tempfile.TemporaryDirectory(prefix=".building_", dir=destination) as temp:
            workdir = Path(temp)
            snapshot = workdir / "work_base.db"
            partial = workdir / filename
            await db.snapshot(snapshot)
            await asyncio.to_thread(_create_archive, partial, snapshot, settings.storage_dir)
            partial.replace(archive)
        await db.conn.execute(
            "INSERT OR REPLACE INTO work_system(key,value) VALUES ('last_local_backup',?)",
            (now.isoformat(timespec="seconds"),),
        )
        await db.conn.commit()
        log.info("Резервная копия сохранена: %s", archive)
        return archive
