"""Outgoing Telegram notifications with rate limiting and error handling."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.keyboards.common import kb
from app.locales import t
from app.models import (
    AttemptStatus,
    Group,
    GroupMember,
    Test,
    TestAttempt,
    User,
    UserRole,
    UserStatus,
)
from app.utils.text import esc
from app.utils.time import fmt_dt, fmt_hours

logger = logging.getLogger(__name__)

_bot_username: str | None = None


async def bot_username(bot: Bot) -> str:
    global _bot_username
    if _bot_username is None:
        me = await bot.me()
        _bot_username = me.username or ""
    return _bot_username


async def deep_link(bot: Bot, payload: str) -> str:
    return f"https://t.me/{await bot_username(bot)}?start={payload}"


@dataclass
class BroadcastResult:
    sent: int = 0
    failed: int = 0
    blocked: int = 0


async def safe_send(bot: Bot, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> bool | None:
    """Send a message. Returns True on success, False on failure, None if the user blocked the bot."""
    for _ in range(3):
        try:
            await bot.send_message(chat_id, text, reply_markup=reply_markup, disable_web_page_preview=True)
            return True
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 0.5)
        except TelegramForbiddenError:
            return None
        except TelegramBadRequest as exc:
            logger.warning("Telegram rejected message", extra={"chat_id": chat_id, "error": str(exc)[:200]})
            return False
        except Exception as exc:
            logger.warning("Failed to send message", extra={"chat_id": chat_id, "error": str(exc)[:200]})
            return False
    return False


async def broadcast_to_users(
    bot: Bot, session: AsyncSession, users: list[User], text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> BroadcastResult:
    result = BroadcastResult()
    delay = get_settings().broadcast_delay_seconds
    blocked_ids: list[int] = []
    for user in users:
        if not user.has_private_chat or user.bot_blocked:
            continue
        ok = await safe_send(bot, user.telegram_id, text, reply_markup)
        if ok:
            result.sent += 1
        elif ok is None:
            result.blocked += 1
            blocked_ids.append(user.id)
        else:
            result.failed += 1
        await asyncio.sleep(delay)
    if blocked_ids:
        await session.execute(update(User).where(User.id.in_(blocked_ids)).values(bot_blocked=True))
        await session.commit()
    return result


async def notify_admins(bot: Bot, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    for admin_id in get_settings().admin_ids:
        await safe_send(bot, admin_id, text, reply_markup)


async def test_audience_users(session: AsyncSession, test: Test) -> list[User]:
    stmt = select(User).where(User.role == UserRole.EMPLOYEE, User.status == UserStatus.ACTIVE)
    if test.group_id is not None:
        stmt = stmt.join(GroupMember, GroupMember.user_id == User.id).where(
            GroupMember.group_id == test.group_id, GroupMember.is_member.is_(True)
        )
    return list((await session.execute(stmt)).scalars().all())


async def test_card_text(test: Test) -> str:
    hours = max(1, round((test.deadline_at - test.starts_at).total_seconds() / 3600))
    text = t(
        "announce.body",
        title=esc(test.title),
        questions=test.question_count,
        duration=fmt_hours(hours),
        deadline=fmt_dt(test.deadline_at),
        passing=test.passing_percent,
    )
    if test.description:
        text += "\n\n" + esc(test.description)
    return text


async def announce_test(bot: Bot, session: AsyncSession, test: Test, *, groups_only: bool = False) -> dict:
    """Post the announcement (with deep-link START button) to groups and DM the audience."""
    from app.services import settings_service

    link = await deep_link(bot, f"test_{test.id}")
    body = await test_card_text(test)
    stats = {"groups": 0, "dm": 0, "dm_failed": 0}

    if await settings_service.get_value(session, "announce_in_groups"):
        group_stmt = select(Group).where(Group.is_active.is_(True))
        if test.group_id is not None:
            group_stmt = group_stmt.where(Group.id == test.group_id)
        for group in (await session.execute(group_stmt)).scalars():
            ok = await safe_send(
                bot, group.chat_id, t("announce.group_header") + "\n\n" + body, kb([(t("btn.start_test_caps"), link)])
            )
            stats["groups"] += int(bool(ok))
    if not groups_only and await settings_service.get_value(session, "notify_employees_dm"):
        users = await test_audience_users(session, test)
        from app.keyboards.callbacks import EmpCB

        result = await broadcast_to_users(
            bot,
            session,
            users,
            t("announce.dm_header") + "\n\n" + body,
            kb([(t("btn.start_test_caps"), EmpCB(a="start", id=test.id))]),
        )
        stats["dm"] = result.sent
        stats["dm_failed"] = result.failed + result.blocked
    logger.info("Test announced", extra={"test_id": test.id, **stats})
    return stats


async def remind_unfinished(bot: Bot, session: AsyncSession, test: Test) -> int:
    """Remind audience members who have not completed the test yet."""
    from app.keyboards.callbacks import EmpCB

    users = await test_audience_users(session, test)
    done = set(
        (
            await session.execute(
                select(TestAttempt.user_id).where(
                    TestAttempt.test_id == test.id, TestAttempt.status == AttemptStatus.COMPLETED
                )
            )
        ).scalars()
    )
    pending = [u for u in users if u.id not in done]
    result = await broadcast_to_users(
        bot,
        session,
        pending,
        t("reminder.body", title=esc(test.title), deadline=fmt_dt(test.deadline_at)),
        kb([(t("btn.open_test"), EmpCB(a="card", id=test.id))]),
    )
    return result.sent
