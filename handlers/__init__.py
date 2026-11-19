"""Сборка роутеров. Порядок важен: catch-all обработчик контента — последним."""
from __future__ import annotations

from aiogram import Router

from . import common, content, search, timers


def get_routers() -> list[Router]:
    return [common.router, search.router, timers.router, content.router]
