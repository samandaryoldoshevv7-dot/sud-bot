from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message, TelegramObject, Update
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import groups as group_service
from app.services.users import is_admin_telegram_id, upsert_user

logger = logging.getLogger(__name__)


class UserMiddleware(BaseMiddleware):
    """Registers/refreshes the Telegram user and injects ``user`` and ``is_admin``.

    In groups, users are only registered when the group is a registered, active group
    (their membership is recorded at the same time).
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        session: AsyncSession = data["session"]
        tg_user = None
        chat = None
        inner = event.event if isinstance(event, Update) else event
        if isinstance(inner, Message):
            tg_user, chat = inner.from_user, inner.chat
        elif isinstance(inner, CallbackQuery):
            tg_user = inner.from_user
            chat = inner.message.chat if inner.message else None
        elif isinstance(inner, ChatMemberUpdated):
            tg_user, chat = inner.from_user, inner.chat

        data["user"] = None
        data["is_admin"] = False
        if tg_user is not None and not tg_user.is_bot:
            data["is_admin"] = is_admin_telegram_id(tg_user.id)
            try:
                if chat is None or chat.type == "private":
                    data["user"] = await upsert_user(session, tg_user, private_chat=chat is not None)
                elif isinstance(inner, Message):
                    group = await group_service.get_by_chat(session, chat.id)
                    if group is not None and group.is_active:
                        user = await upsert_user(session, tg_user)
                        await group_service.mark_membership(session, group, user, True)
                        data["user"] = user
                        data["group"] = group
            except Exception:
                await session.rollback()
                logger.exception("User registration middleware failed")
        return await handler(event, data)
