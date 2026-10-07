"""Helpers shared by admin handlers."""

from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.utils.text import TELEGRAM_MESSAGE_LIMIT

logger = logging.getLogger(__name__)


def fit_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT - 20) -> str:
    """Shorten an HTML message at a line boundary (every template line closes its own tags)."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if "\n" in cut:
        cut = cut.rsplit("\n", 1)[0]
    return cut + "\n…"


async def show(
    target: CallbackQuery | Message, text: str, markup: InlineKeyboardMarkup | None = None, *, answer: bool = True
) -> None:
    """Edit the callback's message in place (fallback: send a new message)."""
    text = fit_message(text)
    if isinstance(target, CallbackQuery):
        if answer:
            try:
                await target.answer()
            except TelegramBadRequest:
                pass
        message = target.message
        if isinstance(message, Message) and message.text is not None:
            try:
                await message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc):
                    return
                logger.debug("edit_text failed, sending new message", extra={"error": str(exc)[:120]})
        if isinstance(message, Message):
            await message.answer(text, reply_markup=markup, disable_web_page_preview=True)
        return
    await target.answer(text, reply_markup=markup, disable_web_page_preview=True)
