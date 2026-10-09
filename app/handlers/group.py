"""Group chat handlers: registration, membership tracking, deep links to the private chat."""

from __future__ import annotations

import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter, Command
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.keyboards.callbacks import AdminCB, GroupAnsCB, GroupStartCB
from app.keyboards.common import kb
from app.locales import t
from app.models import AnswerReveal
from app.services import background, group_tests
from app.services import groups as group_service
from app.services.group_tests import GroupAnswerCode
from app.services.notifications import deep_link, notify_admins
from app.services.users import is_admin_telegram_id, upsert_user
from app.utils.text import esc, truncate
from app.utils.time import fmt_dt

logger = logging.getLogger(__name__)
router = Router(name="group")
GROUP_CHATS = F.chat.type.in_({"group", "supergroup"})
router.message.filter(GROUP_CHATS)
router.my_chat_member.filter(GROUP_CHATS)
router.chat_member.filter(GROUP_CHATS)


@router.message(Command("register", "royxat"))
async def cmd_register(message: Message, session: AsyncSession, bot: Bot, is_admin: bool) -> None:
    if not is_admin or message.from_user is None:
        return  # silently ignore: never reveal admin commands in groups
    admin = await upsert_user(session, message.from_user)
    group, created = await group_service.register_group(session, message.chat.id, message.chat.title or "", admin)
    await message.reply(t("group.registered" if created else "group.reactivated", title=esc(group.title)))


@router.message(Command("start", "test"))
async def cmd_start_in_group(message: Message, bot: Bot, session: AsyncSession) -> None:
    tests = await group_tests.active_group_tests(session, message.chat.id)
    if tests:
        await message.reply(await _active_tests_text(tests), reply_markup=await _active_tests_kb(bot, tests))
        return
    link = await deep_link(bot, "group")
    await message.reply(t("group.open_private"), reply_markup=kb([(t("group.btn.open_bot"), link)]))


async def _active_tests_text(tests) -> str:
    lines = [t("group.active_tests")]
    for test in tests:
        lines.append(
            t("group.active_test_line", title=esc(test.title), n=test.question_count, end=fmt_dt(test.deadline_at))
        )
    lines.append("")
    lines.append(t("group.active_tests_hint"))
    return "\n".join(lines)


async def _active_tests_kb(bot: Bot, tests):
    rows = []
    for test in tests[:5]:
        rows.append(
            [(t("group.btn.take_in_bot", title=truncate(test.title, 30)), await deep_link(bot, f"test_{test.id}"))]
        )
    return kb(*rows)


# A join can arrive both as a chat_member update and as a service message: greet only once.
_greeted: dict[tuple[int, int], float] = {}
GREET_TTL = 600.0


async def greet_new_member(bot: Bot, session: AsyncSession, chat_id: int, member) -> None:
    """Tell a new member about running tests (they may not see older group messages)."""
    now = time.monotonic()
    for key, ts in list(_greeted.items()):
        if now - ts > GREET_TTL:
            del _greeted[key]
    key = (chat_id, member.id)
    if key in _greeted:
        return
    tests = await group_tests.active_group_tests(session, chat_id)
    if not tests:
        return
    _greeted[key] = now
    name = esc(member.full_name or member.first_name or "")
    text = t("group.welcome_new_member", name=name) + "\n\n" + await _active_tests_text(tests)
    try:
        await bot.send_message(chat_id, text, reply_markup=await _active_tests_kb(bot, tests))
    except Exception as exc:
        logger.warning("Could not greet new member", extra={"chat_id": chat_id, "error": str(exc)[:200]})


@router.message(F.new_chat_members)
async def on_new_members(message: Message, session: AsyncSession, bot: Bot) -> None:
    group = await group_service.get_by_chat(session, message.chat.id)
    if group is None or not group.is_active:
        return
    for member in message.new_chat_members or []:
        if member.is_bot:
            continue
        user = await upsert_user(session, member)
        await group_service.mark_membership(session, group, user, True)
        await greet_new_member(bot, session, message.chat.id, member)


@router.message(F.migrate_to_chat_id)
async def on_migrate(message: Message, session: AsyncSession) -> None:
    if message.migrate_to_chat_id:
        await group_service.migrate_chat(session, message.chat.id, message.migrate_to_chat_id)


@router.message(F.new_chat_title)
async def on_title(message: Message, session: AsyncSession) -> None:
    await group_service.update_title(session, message.chat.id, message.new_chat_title or "")


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def bot_added(event: ChatMemberUpdated, session: AsyncSession, bot: Bot) -> None:
    title = event.chat.title or str(event.chat.id)
    if event.from_user and is_admin_telegram_id(event.from_user.id):
        admin = await upsert_user(session, event.from_user)
        group, _ = await group_service.register_group(session, event.chat.id, title, admin)
        await bot.send_message(event.chat.id, t("group.registered", title=esc(group.title)))
        return
    existing = await group_service.get_by_chat(session, event.chat.id)
    if existing and existing.is_active:
        return
    await notify_admins(
        bot,
        t("group.added_by_other", title=esc(title), chat_id=event.chat.id),
        kb([(t("group.btn.register"), AdminCB(s="grp_reg", id=0, v=str(event.chat.id)))]),
    )


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def bot_removed(event: ChatMemberUpdated, session: AsyncSession) -> None:
    group = await group_service.get_by_chat(session, event.chat.id)
    if group:
        await group_service.set_active(session, group.id, False)
        logger.info("Bot removed from group; group deactivated", extra={"chat_id": event.chat.id})


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def member_joined(event: ChatMemberUpdated, session: AsyncSession, bot: Bot) -> None:
    group = await group_service.get_by_chat(session, event.chat.id)
    member = event.new_chat_member.user
    if group and group.is_active and not member.is_bot:
        user = await upsert_user(session, member)
        await group_service.mark_membership(session, group, user, True)
        await greet_new_member(bot, session, event.chat.id, member)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def member_left(event: ChatMemberUpdated, session: AsyncSession) -> None:
    group = await group_service.get_by_chat(session, event.chat.id)
    member = event.new_chat_member.user
    if group and not member.is_bot:
        user = await upsert_user(session, member)
        await group_service.mark_membership(session, group, user, False)


# ------------------------------------------------------------------------------ tests inside the group


@router.callback_query(GroupStartCB.filter())
async def cb_group_start(
    callback: CallbackQuery, callback_data: GroupStartCB, session: AsyncSession, bot: Bot, session_maker
) -> None:
    """🤖 1. Botda ishlash / 👥 2. Guruhda ishlash. The employee is identified ONLY by
    callback.from_user and gets their own attempt (personal timer starts now)."""
    code, attempt = await group_tests.group_start(session, callback.from_user, callback_data.p)
    if code == GroupAnswerCode.ACCEPTED and attempt is not None:
        done, total, test_id = attempt.answered_count, attempt.total_questions, attempt.test_id
        if callback_data.m == "g":
            # The questions are posted into the group once and shared; each member answers for themself.
            if await group_tests.open_in_group(session, callback_data.p):
                background.spawn(group_tests.send_post(bot, session_maker, callback_data.p),
                                 name=f"group-questions-{callback_data.p}")  # fmt: skip
            await callback.answer(t("gt.alert.in_group", done=done, total=total), show_alert=True)
            return
        # Telegram opens the bot chat with /start run_<id>; the question is shown there.
        await callback.answer(url=await deep_link(bot, f"run_{test_id}"))
        return
    duplicate = code == GroupAnswerCode.DUPLICATE
    await callback.answer(t("gt.alert.already_finished") if duplicate else t(f"gt.alert.{code.value}"), show_alert=True)


async def on_group_poll_answer(bot: Bot, session: AsyncSession, poll_answer, gqm_id: int, letter: str) -> None:
    """An answer to a question posted in the group as a quiz poll (called by the poll_answer handler).
    Telegram already showed this member their own result; the full card goes to their private chat."""
    tg_user = poll_answer.user
    result = await group_tests.submit_group_answer(session, tg_user, gqm_id, letter)
    if result.code != GroupAnswerCode.ACCEPTED:
        return
    if result.private_chat and result.reveal == AnswerReveal.IMMEDIATE and result.question is not None:
        await _send_answer_card(bot, session, tg_user.id, result)
    if result.finished and result.attempt is not None:
        await _send_private_result(bot, session, tg_user.id, result)


@router.callback_query(GroupAnsCB.filter())
async def cb_group_answer(callback: CallbackQuery, callback_data: GroupAnsCB, session: AsyncSession, bot: Bot) -> None:
    # Identity comes ONLY from Telegram (callback.from_user), never from callback data.
    result = await group_tests.submit_group_answer(session, callback.from_user, callback_data.m, callback_data.o)
    await callback.answer(group_tests.answer_alert(result), show_alert=True)
    if result.code != GroupAnswerCode.ACCEPTED:
        return
    if result.private_chat and result.reveal == AnswerReveal.IMMEDIATE and result.question is not None:
        await _send_answer_card(bot, session, callback.from_user.id, result)
    if result.finished and result.attempt is not None:
        await _send_private_result(bot, session, callback.from_user.id, result)


async def _send_answer_card(bot: Bot, session: AsyncSession, telegram_id: int, result) -> None:
    """Legacy in-group questions: the full answer window is sent to the private chat."""
    from app.handlers.formatting import answer_result_text
    from app.services.notifications import safe_send

    text = answer_result_text(
        result.question, result.opts or [], result.position, result.total, result.selected, AnswerReveal.IMMEDIATE
    )
    await safe_send(bot, telegram_id, text)


async def _send_private_result(bot: Bot, session: AsyncSession, telegram_id: int, result) -> None:
    """If the employee has opened the bot privately, send the detailed result there too."""
    from app.handlers.formatting import result_text
    from app.keyboards.callbacks import EmpCB
    from app.models import AnswerReveal, Test, User
    from app.services.notifications import safe_send

    attempt = result.attempt
    user = await session.get(User, attempt.user_id)
    test = await session.get(Test, attempt.test_id)
    if user is None or test is None or not user.has_private_chat or test.answer_reveal == AnswerReveal.NEVER:
        return
    markup = None
    if attempt.incorrect_count:
        markup = kb([(t("emp.btn.corrections"), EmpCB(a="corr", id=attempt.id))])
    from app.statistics.sources import attempt_source_breakdown

    await safe_send(
        bot, telegram_id, result_text(test, attempt, user, await attempt_source_breakdown(session, attempt.id)), markup
    )
