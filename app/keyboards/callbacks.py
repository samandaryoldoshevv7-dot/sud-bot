"""Compact callback-data factories (Telegram limit: 64 bytes)."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class AdminCB(CallbackData, prefix="a"):
    """Admin navigation/action. ``s`` = action, ``id`` = entity id, ``p`` = page, ``v`` = extra value."""

    s: str
    id: int = 0
    p: int = 0
    v: str = ""


class EmpCB(CallbackData, prefix="e"):
    """Employee actions: ``a`` = action, ``id`` = test or attempt id."""

    a: str
    id: int = 0
    p: int = 0


class AnsCB(CallbackData, prefix="q"):
    """An answer click: attempt id, question position, displayed letter."""

    at: int
    pos: int
    o: str


class WizCB(CallbackData, prefix="w"):
    """Test creation wizard choice: ``f`` = field, ``v`` = value."""

    f: str
    v: str = ""
