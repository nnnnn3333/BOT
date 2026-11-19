"""Маршруты: меню/FSM раньше универсального приёма, файл обработчики последними."""
from __future__ import annotations
from aiogram import Router
from . import workspace, ingest, common, search, timers, content

def get_routers() -> list[Router]:
    return [workspace.router, ingest.router, common.router, search.router, timers.router, content.router]
