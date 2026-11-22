"""Конфигурация проекта: читает .env и отдаёт неизменяемый объект Settings."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Ограничения Bot API (облачный сервер Telegram)
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # бот может скачать файл не больше 20 МБ
TG_UPLOAD_LIMIT = 50 * 1024 * 1024  # бот может отправить файл не больше 50 МБ


class ConfigError(RuntimeError):
    """Ошибка в .env — бот не должен стартовать."""


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_id: int
    db_path: Path
    storage_dir: Path
    tmp_dir: Path
    tz_name: str
    tz: ZoneInfo
    backup_hour: int
    backup_minute: int
    max_content_chars: int


def _path_from_env(name: str, default: str) -> Path:
    raw = os.getenv(name, "").strip()
    path = Path(raw).expanduser() if raw else Path(default)
    return path if path.is_absolute() else BASE_DIR / path


def _parse_hhmm(raw: str) -> tuple[int, int]:
    try:
        hh, mm = raw.strip().split(":")
        hour, minute = int(hh), int(mm)
    except ValueError as exc:
        raise ConfigError(f"BACKUP_TIME должен быть в формате HH:MM, получено {raw!r}") from exc
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ConfigError(f"BACKUP_TIME вне диапазона: {raw!r}")
    return hour, minute


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("BOT_TOKEN не задан в .env")

    admin_raw = os.getenv("ADMIN_ID", "").strip()
    if not admin_raw.isdigit():
        raise ConfigError("ADMIN_ID должен быть числовым Telegram ID (узнать: @userinfobot)")

    tz_name = os.getenv("TIMEZONE", "Europe/Kaliningrad").strip()
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:  # ZoneInfoNotFoundError, ValueError
        raise ConfigError(f"Неизвестный TIMEZONE: {tz_name!r}") from exc

    backup_hour, backup_minute = _parse_hhmm(os.getenv("BACKUP_TIME", "03:00"))

    data_dir = _path_from_env("DATA_DIR", ".") if os.getenv("DATA_DIR", "").strip() else BASE_DIR

    return Settings(
        bot_token=token,
        admin_id=int(admin_raw),
        db_path=_path_from_env("DB_PATH", str(data_dir / "work_base.db")),
        storage_dir=_path_from_env("STORAGE_DIR", str(data_dir / "storage")),
        tmp_dir=_path_from_env("TMP_DIR", str(data_dir / "tmp")),
        tz_name=tz_name,
        tz=tz,
        backup_hour=backup_hour,
        backup_minute=backup_minute,
        max_content_chars=int(os.getenv("MAX_CONTENT_CHARS", "1000000")),
    )
