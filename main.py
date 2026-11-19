"""Точка входа: python main.py"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from config import ConfigError, load_settings
from database import Database
from handlers import get_routers
from middlewares import AdminOnlyMiddleware
from scheduler import setup_scheduler
from workspace_db import initialize, notify_deadlines

log = logging.getLogger("workbot")

COMMANDS = [
    BotCommand(command='menu', description='Главное меню'),
    BotCommand(command='sections', description='Разделы базы знаний'),
    BotCommand(command='deadlines', description='Мои дедлайны'),
    BotCommand(command='deadline', description='Добавить дедлайн'),
    BotCommand(command='reindex', description='OCR старых скриншотов'),
    BotCommand(command="find", description="Поиск по базе: /find запрос"),
    BotCommand(command="timer", description="Таймер задачи: /timer название"),
    BotCommand(command="stats", description="Тайминги за неделю (.xlsx)"),
    BotCommand(command="backup", description="Бэкап прямо сейчас"),
    BotCommand(command="help", description="Справка"),
]


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,  # journald подхватит
    )

    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("Ошибка конфигурации: %s", exc)
        sys.exit(1)

    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)

    db = Database(settings.db_path)
    await db.connect()
    await initialize(db)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    # Безопасность: на уровне Update, до любых хендлеров.
    dp.update.outer_middleware(AdminOnlyMiddleware(settings.admin_id))

    # Зависимости для хендлеров (подставляются по имени параметра).
    dp["db"] = db
    dp["settings"] = settings

    for router in get_routers():
        dp.include_router(router)

    scheduler = setup_scheduler(bot, db, settings)
    scheduler.add_job(notify_deadlines, 'interval', minutes=1,
                      args=[bot, db, settings], id='deadline_reminders',
                      replace_existing=True, max_instances=1, coalesce=True)
    scheduler.start()
    log.info("Бэкап по расписанию: %02d:%02d (%s)", settings.backup_hour, settings.backup_minute, settings.tz_name)

    try:
        await bot.set_my_commands(COMMANDS)
        log.info("Бот запущен")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await db.close()
        await bot.session.close()
        log.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
