"""Group chat handlers: registration, membership tracking, deep links to the private chat."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter, Command
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.keyboards.callbacks import AdminCB
from app.keyboards.common import kb
from app.locales import t
from app.services import groups as group_service
from app.services.notifications import deep_link, notify_admins
from app.services.users import is_admin_telegram_id, upsert_user
from app.utils.text import esc

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
async def cmd_start_in_group(message: Message, bot: Bot) -> None:
    link = await deep_link(bot, "group")
    await message.reply(t("group.open_private"), reply_markup=kb([(t("group.btn.open_bot"), link)]))


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
async def member_joined(event: ChatMemberUpdated, session: AsyncSession) -> None:
    group = await group_service.get_by_chat(session, event.chat.id)
    member = event.new_chat_member.user
    if group and group.is_active and not member.is_bot:
        user = await upsert_user(session, member)
        await group_service.mark_membership(session, group, user, True)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def member_left(event: ChatMemberUpdated, session: AsyncSession) -> None:
    group = await group_service.get_by_chat(session, event.chat.id)
    member = event.new_chat_member.user
    if group and not member.is_bot:
        user = await upsert_user(session, member)
        await group_service.mark_membership(session, group, user, False)
