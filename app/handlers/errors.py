from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ErrorEvent

from app.locales import t

logger = logging.getLogger(__name__)
router = Router(name="errors")


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    exc = event.exception
    if isinstance(exc, TelegramBadRequest) and "message is not modified" in str(exc):
        return True  # harmless double click
    update = event.update
    logger.error(
        "Unhandled error while processing update",
        extra={"update_id": update.update_id, "error_type": type(exc).__name__},
        exc_info=exc,
    )
    try:
        if update.callback_query:
            await update.callback_query.answer(t("errors.generic"), show_alert=True)
        elif update.message and update.message.chat.type == "private":
            await update.message.answer(t("errors.generic"))
    except Exception:
        logger.debug("Could not notify user about error", exc_info=True)
    return True
