"""APScheduler: ежедневный бэкап в заданное время (по умолчанию 03:00)."""
from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from backup import save_backup
from config import Settings
from database import Database


def setup_scheduler(db: Database, settings: Settings) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.tz_name)
    scheduler.add_job(
        save_backup,
        trigger=CronTrigger(
            hour=settings.backup_hour,
            minute=settings.backup_minute,
            timezone=settings.tz_name,
        ),
        kwargs={"db": db, "settings": settings},
        id="daily_backup",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,  # если VPS был недоступен в 03:00 — выполнить в течение часа
    )
    return scheduler
