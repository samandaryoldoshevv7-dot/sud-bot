from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.handlers.admin.common import show
from app.keyboards.admin import admin_main_kb
from app.keyboards.callbacks import AdminCB
from app.locales import t

router = Router(name="admin_menu")


async def admin_menu_view(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    """The admin panel: title and the section buttons."""
    return t("admin.menu.title"), admin_main_kb()


@router.callback_query(AdminCB.filter(F.s == "menu"))
async def cb_menu(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await show(callback, *await admin_menu_view(session))


@router.callback_query(AdminCB.filter(F.s == "cancel"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await callback.answer(t("common.cancelled"))
    await show(callback, *await admin_menu_view(session), answer=False)


@router.callback_query(AdminCB.filter(F.s == "noop"))
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()
