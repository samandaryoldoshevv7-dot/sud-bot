"""/start, /help, /cancel, registration and generic fallbacks."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from app.handlers.formatting import user_line
from app.keyboards.admin import admin_main_kb
from app.keyboards.callbacks import AdminCB
from app.keyboards.common import kb
from app.keyboards.employee import employee_reply_kb
from app.locales import t
from app.models import User, UserStatus
from app.services import groups as group_service
from app.services import settings_service
from app.services import users as user_service
from app.services.notifications import notify_admins

logger = logging.getLogger(__name__)
router = Router(name="common")
router.message.filter(F.chat.type == "private")
fallback_router = Router(name="fallback")


class Registration(StatesGroup):
    full_name = State()


async def check_group_membership(bot: Bot, session: AsyncSession, user: User) -> bool:
    """Ask Telegram whether the user belongs to any registered active group; record it."""
    found = False
    for group, _ in await group_service.list_groups(session, active_only=True):
        try:
            member = await bot.get_chat_member(group.chat_id, user.telegram_id)
        except Exception as exc:  # bot removed from group, chat not found, etc.
            logger.debug("get_chat_member failed", extra={"chat_id": group.chat_id, "error": str(exc)[:100]})
            continue
        is_member = member.status in ("creator", "administrator", "member") or (
            member.status == "restricted" and getattr(member, "is_member", False)
        )
        await group_service.mark_membership(session, group, user, is_member)
        found = found or is_member
    return found


async def try_auto_approve(bot: Bot, session: AsyncSession, user: User) -> bool:
    if user.status != UserStatus.PENDING:
        return user.status == UserStatus.ACTIVE
    in_group = await check_group_membership(bot, session, user)
    settings = await settings_service.get_all(session)
    if settings["auto_approve_all"] or (in_group and settings["auto_approve_group_members"]):
        await user_service.set_status(session, user.id, UserStatus.ACTIVE)
        await session.refresh(user)
        return True
    return False


async def _send_admin_menu(message: Message, session: AsyncSession | None) -> None:
    if session is None:
        await message.answer(t("admin.menu.title"), reply_markup=admin_main_kb())
        return
    from app.handlers.admin.menu import admin_menu_view

    text, markup = await admin_menu_view(session)
    await message.answer(text, reply_markup=markup)


async def show_home(
    message: Message, user: User, is_admin: bool, bot: Bot | None = None, session: AsyncSession | None = None
) -> None:
    if is_admin:
        await message.answer(t("start.admin", name=user_line(user)), reply_markup=employee_reply_kb(is_admin=True))
        await _send_admin_menu(message, session)
        return
    await message.answer(t("start.employee", name=user_line(user)), reply_markup=employee_reply_kb())
    if bot is not None and session is not None:
        # Show running tests right away (a new employee should not have to look for them).
        from app.handlers.test_listing import available_tests_view

        view = await available_tests_view(bot, session, user)
        if view is not None:
            await message.answer(view[0], reply_markup=view[1])


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    user: User | None,
    is_admin: bool,
) -> None:
    await state.clear()
    if user is None:
        return
    payload = (command.args or "").strip()
    if payload.startswith("test_"):
        await state.update_data(pending_test=payload.removeprefix("test_"))
    elif payload.startswith("run_"):  # ▶️ TESTNI BOSHLASH pressed in a group
        await state.update_data(pending_test=payload.removeprefix("run_"), pending_run=True)

    if is_admin:
        await show_home(message, user, is_admin)
        await _open_pending_test(message, state, session, user, bot)
        return

    if user.status in (UserStatus.INACTIVE, UserStatus.BLOCKED):
        await message.answer(t("start.inactive"), reply_markup=ReplyKeyboardRemove())
        return
    if user.status == UserStatus.PENDING and await settings_service.get_value(session, "auto_approve_all"):
        # Automatic activation: the employee is stored and gets access immediately (the admin can
        # still block or deactivate them at any time).
        await user_service.set_status(session, user.id, UserStatus.ACTIVE)
        await session.refresh(user)
        await check_group_membership(bot, session, user)
        await notify_admins(
            bot,
            t("admin.new_employee_auto", name=user_line(user), tg_id=user.telegram_id),
            kb([(t("emp_admin.btn.profile"), AdminCB(s="emp_v", id=user.id))]),
        )
        logger.info("Employee activated automatically", extra={"user_id": user.id})
    if user.status == UserStatus.PENDING:
        if not user.full_name:
            await state.set_state(Registration.full_name)
            await message.answer(t("register.ask_name"), reply_markup=ReplyKeyboardRemove())
            return
        if not await try_auto_approve(bot, session, user):
            await message.answer(t("register.pending"))
            return
    await _home_with_tests(message, state, session, bot, user, is_admin)
    await _open_pending_test(message, state, session, user, bot)


async def _home_with_tests(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot, user: User, is_admin: bool
) -> None:
    """Home screen; lists running tests unless a deep link already points at a specific one."""
    pending = str((await state.get_data()).get("pending_test") or "")
    if pending.isdigit():
        await show_home(message, user, is_admin)
    else:
        await show_home(message, user, is_admin, bot, session)


async def _open_pending_test(
    message: Message, state: FSMContext, session: AsyncSession, user: User, bot: Bot | None = None
) -> None:
    data = await state.get_data()
    pending = str(data.get("pending_test", ""))
    run = bool(data.get("pending_run"))
    await state.update_data(pending_test=None, pending_run=None)
    if not pending.isdigit():
        return
    from app.handlers.employee import START_ERRORS, open_test_in_private, send_test_card

    if run and bot is not None:
        # Coming from the group button: continue right away with this employee's own attempt.
        error, _ = await open_test_in_private(bot, message.chat.id, session, user, int(pending))
        if error is None:
            return
        await message.answer(t(START_ERRORS[error]))
    await send_test_card(message, session, user, int(pending))


@router.message(Registration.full_name, F.text)
async def registration_name(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot, user: User | None, is_admin: bool
) -> None:
    if user is None:
        return
    name = " ".join((message.text or "").split())
    if len(name) < 5 or len(name) > 120 or len(name.split()) < 2 or any(ch.isdigit() for ch in name):
        await message.answer(t("register.bad_name"))
        return
    await user_service.set_full_name(session, user, name)
    data = await state.get_data()
    await state.set_state(None)
    await state.set_data({"pending_test": data.get("pending_test")})
    if await try_auto_approve(bot, session, user):
        await message.answer(t("register.approved"))
        await _home_with_tests(message, state, session, bot, user, is_admin)
        await _open_pending_test(message, state, session, user, bot)
        return
    await message.answer(t("register.pending"))
    await notify_admins(
        bot,
        t("admin.new_employee", name=user_line(user), tg_id=user.telegram_id),
        kb(
            [
                (t("emp_admin.btn.approve"), AdminCB(s="emp_appr", id=user.id)),
                (t("emp_admin.btn.reject"), AdminCB(s="emp_deact", id=user.id)),
            ],
            [(t("emp_admin.btn.profile"), AdminCB(s="emp_v", id=user.id))],
        ),
    )
    logger.info("Employee registered (pending approval)", extra={"user_id": user.id})


@router.message(Registration.full_name)
async def registration_name_invalid(message: Message) -> None:
    await message.answer(t("register.bad_name"))


@router.message(Command("help"))
@router.message(F.text == t("emp.btn.help"))
async def cmd_help(message: Message, is_admin: bool) -> None:
    await message.answer(t("help.employee"))
    if is_admin:
        await message.answer(t("help.admin"))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, is_admin: bool, session: AsyncSession) -> None:
    current = await state.get_state()
    await state.clear()
    if current is None:
        await message.answer(t("common.nothing_to_cancel"))
        return
    await message.answer(t("common.cancelled"))
    if is_admin:
        await _send_admin_menu(message, session)


@router.message(Command("admin"))
@router.message(F.text == t("emp.btn.admin_panel"))
async def cmd_admin(message: Message, state: FSMContext, is_admin: bool, session: AsyncSession) -> None:
    if not is_admin:
        logger.warning(
            "Non-admin tried /admin", extra={"telegram_id": message.from_user.id if message.from_user else None}
        )
        await message.answer(t("errors.not_admin"))
        return
    await state.clear()
    await _send_admin_menu(message, session)


@fallback_router.callback_query(AdminCB.filter())
async def admin_callback_denied(callback: CallbackQuery) -> None:
    """Reached only when IsAdmin rejected the callback (forged or stale admin buttons)."""
    await callback.answer(t("errors.not_admin"), show_alert=True)


@fallback_router.callback_query()
async def unknown_callback(callback: CallbackQuery) -> None:
    await callback.answer(t("errors.stale_button"), show_alert=False)


@fallback_router.message(F.chat.type == "private", StateFilter(None))
async def unknown_message(message: Message, user: User | None, is_admin: bool) -> None:
    if user is None:
        return
    if user.status == UserStatus.ACTIVE or is_admin:
        await message.answer(t("common.unknown_input"), reply_markup=employee_reply_kb(is_admin))
    elif user.status == UserStatus.PENDING:
        await message.answer(t("register.pending") if user.full_name else t("start.press_start"))
    else:
        await message.answer(t("start.inactive"))


@fallback_router.message(F.chat.type == "private")
async def unexpected_in_state(message: Message) -> None:
    await message.answer(t("common.unexpected_input"))
