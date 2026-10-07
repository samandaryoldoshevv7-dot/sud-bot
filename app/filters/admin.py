from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from app.services.users import is_admin_telegram_id


class IsAdmin(BaseFilter):
    """Server-side admin check against ADMIN_TELEGRAM_IDS (never trusts client input)."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        return user is not None and is_admin_telegram_id(user.id)
