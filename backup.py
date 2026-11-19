"""Ежесуточный бэкап: work_base.db + storage/ -> zip -> Telegram."""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile

from config import TG_UPLOAD_LIMIT, Settings
from database import Database
from utils import esc

log = logging.getLogger(__name__)

_PART_SIZE = 45 * 1024 * 1024  # запас до лимита 50 МБ
_lock = asyncio.Lock()


def _write_zip(archive: Path, snapshot: Path, storage_dir: Path) -> None:
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(snapshot, "work_base.db")
        if storage_dir.exists():
            for path in sorted(storage_dir.rglob("*")):
                if path.is_file():
                    zf.write(path, Path("storage") / path.relative_to(storage_dir))


def _split_if_needed(archive: Path) -> list[Path]:
    """Архив > лимита Telegram режем на части .zip.001, .zip.002 ... (склейка через cat)."""
    if archive.stat().st_size <= TG_UPLOAD_LIMIT - 1024 * 1024:
        return [archive]
    parts: list[Path] = []
    with archive.open("rb") as src:
        while chunk := src.read(_PART_SIZE):
            part = archive.with_name(f"{archive.name}.{len(parts) + 1:03d}")
            part.write_bytes(chunk)
            parts.append(part)
    archive.unlink()
    return parts


async def send_backup(bot: Bot, db: Database, settings: Settings) -> None:
    """Собирает архив и шлёт админу. Ошибки не пробрасывает — сообщает в чат."""
    if _lock.locked():
        await bot.send_message(settings.admin_id, "⏳ Бэкап уже выполняется.")
        return

    async with _lock:
        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="backup_", dir=settings.tmp_dir))
        try:
            stamp = datetime.now(settings.tz).strftime("%Y-%m-%d_%H%M")
            snapshot = workdir / "work_base.db"
            archive = workdir / f"workbot_backup_{stamp}.zip"

            await db.snapshot(snapshot)
            await asyncio.to_thread(_write_zip, archive, snapshot, settings.storage_dir)
            snapshot.unlink(missing_ok=True)
            parts = await asyncio.to_thread(_split_if_needed, archive)

            for index, part in enumerate(parts, start=1):
                size_mb = part.stat().st_size / 1024 / 1024
                caption = f"🗄 Бэкап {stamp} · {size_mb:.1f} МБ"
                if len(parts) > 1:
                    caption += (
                        f"\nЧасть {index}/{len(parts)}. Склейка:\n"
                        f"<code>cat workbot_backup_{stamp}.zip.* > workbot_backup_{stamp}.zip</code>"
                    )
                await bot.send_document(
                    settings.admin_id,
                    FSInputFile(part),
                    caption=caption,
                    request_timeout=600,
                )
            log.info("Бэкап отправлен (%d файл(ов))", len(parts))
        except Exception as exc:
            log.exception("Бэкап не удался")
            try:
                await bot.send_message(settings.admin_id, f"❌ Бэкап не удался: {esc(exc)}")
            except Exception:
                log.exception("Не удалось уведомить об ошибке бэкапа")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
