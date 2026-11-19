"""Middleware безопасности: пропускает апдейты только от ADMIN_ID."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

log = logging.getLogger(__name__)


class AdminOnlyMiddleware(BaseMiddleware):
    """Регистрируется на dp.update.outer_middleware — отсекает ВСЁ до хендлеров.

    Чужим пользователям бот не отвечает вообще (не раскрываем, что он существует).
    Апдейты без отправителя блокируются по умолчанию.
    """

    def __init__(self, admin_id: int) -> None:
        self.admin_id = admin_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None and isinstance(event, Update):  # страховка
            for part in (event.message, event.edited_message, event.callback_query):
                if part is not None and part.from_user is not None:
                    user = part.from_user
                    break

        if user is None or user.id != self.admin_id:
            log.warning("Доступ запрещён: user_id=%s", getattr(user, "id", None))
            return None
        return await handler(event, data)
