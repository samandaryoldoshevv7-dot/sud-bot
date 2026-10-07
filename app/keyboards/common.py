"""Keyboard building helpers."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.keyboards.callbacks import AdminCB
from app.locales import t

Button = tuple[str, CallbackData | str]


def btn(text: str, data: CallbackData | str) -> InlineKeyboardButton:
    if isinstance(data, CallbackData):
        return InlineKeyboardButton(text=text, callback_data=data.pack())
    if data.startswith(("http://", "https://", "tg://")):
        return InlineKeyboardButton(text=text, url=data)
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows: list[Button] | None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn(text, data) for text, data in row] for row in rows if row])


def pager(action: str, page: int, total: int, page_size: int, *, id_: int = 0, v: str = "") -> list[Button]:
    pages = max(1, (total + page_size - 1) // page_size)
    if pages <= 1:
        return []
    row: list[Button] = []
    row.append(("◀️", AdminCB(s=action, id=id_, p=page - 1, v=v)) if page > 0 else (" ", AdminCB(s="noop")))
    row.append((f"{page + 1}/{pages}", AdminCB(s="noop")))
    row.append(("▶️", AdminCB(s=action, id=id_, p=page + 1, v=v)) if page + 1 < pages else (" ", AdminCB(s="noop")))
    return row


def back_row(action: str = "menu", id_: int = 0, p: int = 0, v: str = "") -> list[Button]:
    return [(t("btn.back"), AdminCB(s=action, id=id_, p=p, v=v))]


def back_menu_row(action: str, id_: int = 0, p: int = 0, v: str = "") -> list[Button]:
    return [(t("btn.back"), AdminCB(s=action, id=id_, p=p, v=v)), (t("btn.main_menu"), AdminCB(s="menu"))]


def cancel_kb() -> InlineKeyboardMarkup:
    return kb([(t("btn.cancel"), AdminCB(s="cancel"))])


def skip_cancel_kb(skip_action: str = "skip") -> InlineKeyboardMarkup:
    return kb([(t("btn.skip"), AdminCB(s=skip_action)), (t("btn.cancel"), AdminCB(s="cancel"))])


def confirm_kb(yes: AdminCB, no: AdminCB) -> InlineKeyboardMarkup:
    return kb([(t("btn.yes_confirm"), yes), (t("btn.no"), no)])
