"""Auth middleware: пропускает только ALLOWED_USER_ID, остальных молча игнорим."""

from typing import Any, Awaitable, Callable

import structlog
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from src.config import settings

log = structlog.get_logger()


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = _extract_user_id(event)
        if user_id is None:
            return  # системное событие, не наше дело
        if user_id != settings.allowed_user_id:
            log.warning("auth.rejected", user_id=user_id)
            return  # молча игнорим
        return await handler(event, data)


def _extract_user_id(event: TelegramObject) -> int | None:
    if isinstance(event, Update):
        if event.message and event.message.from_user:
            return event.message.from_user.id
        if event.callback_query and event.callback_query.from_user:
            return event.callback_query.from_user.id
        if event.edited_message and event.edited_message.from_user:
            return event.edited_message.from_user.id
    return None
