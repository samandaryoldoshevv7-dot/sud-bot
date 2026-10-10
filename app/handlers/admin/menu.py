from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.handlers.admin.common import show
from app.keyboards.admin import admin_main_kb
from app.keyboards.callbacks import AdminCB
from app.locales import t
from app.services import settings_service

router = Router(name="admin_menu")


async def admin_menu_view(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    """The admin panel: title, the current test mode and the section buttons."""
    in_group = bool(await settings_service.get_value(session, "tests_in_group"))
    mode = t("menu.mode_group") if in_group else t("menu.mode_private")
    return t("admin.menu.title") + "\n\n" + t("admin.menu.mode", m=mode), admin_main_kb(in_group)


@router.callback_query(AdminCB.filter(F.s == "menu"))
async def cb_menu(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await show(callback, *await admin_menu_view(session))


@router.callback_query(AdminCB.filter(F.s == "cancel"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await callback.answer(t("common.cancelled"))
    await show(callback, *await admin_menu_view(session), answer=False)


@router.callback_query(AdminCB.filter(F.s == "mode"))
async def cb_mode(callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession) -> None:
    """👥 Guruhda ishlash / 💬 Shaxsiy chatda ishlash: stored in the database (survives restarts)."""
    in_group = callback_data.v == "group"
    await settings_service.set_value(session, "tests_in_group", in_group)
    await callback.answer(t("admin.mode.set_group" if in_group else "admin.mode.set_private"), show_alert=True)
    await show(callback, *await admin_menu_view(session), answer=False)


@router.callback_query(AdminCB.filter(F.s == "noop"))
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()
