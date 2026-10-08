"""Shows an employee every test they can take right now (used by /start, approval and the menu)."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.keyboards.callbacks import EmpCB
from app.keyboards.common import kb
from app.locales import t
from app.models import AttemptStatus, Group, GroupMember, GroupTestPost, Test, TestStatus, User
from app.services import attempts as attempt_service
from app.services import groups as group_service
from app.utils.text import esc, truncate
from app.utils.time import fmt_dt, utcnow

logger = logging.getLogger(__name__)


async def refresh_test_memberships(bot: Bot, session: AsyncSession, user: User) -> None:
    """Ask Telegram whether the user belongs to the groups of currently running group tests.

    Membership is otherwise only learnt from group activity, so a new member (or an employee
    approved manually by an admin) would not see tests addressed to their group.
    """
    now = utcnow()
    known = set(
        (
            await session.execute(
                select(GroupMember.group_id).where(GroupMember.user_id == user.id, GroupMember.is_member.is_(True))
            )
        ).scalars()
    )
    running = (
        select(Test.id)
        .where(Test.status == TestStatus.ACTIVE, Test.starts_at <= now, Test.deadline_at > now)
        .scalar_subquery()
    )
    targeted = set(
        (await session.execute(select(Test.group_id).where(Test.id.in_(running), Test.group_id.is_not(None)))).scalars()
    )
    # Tests can also be posted into other groups than their target group.
    posted = set(
        (await session.execute(select(GroupTestPost.group_id).where(GroupTestPost.test_id.in_(running)))).scalars()
    )
    group_ids = (targeted | posted) - known
    for group_id in group_ids:
        group = await session.get(Group, group_id)
        if group is None or not group.is_active:
            continue
        try:
            member = await bot.get_chat_member(group.chat_id, user.telegram_id)
        except Exception as exc:  # bot not in the group any more, etc.
            logger.debug("get_chat_member failed", extra={"chat_id": group.chat_id, "error": str(exc)[:100]})
            continue
        is_member = member.status in ("creator", "administrator", "member") or (
            member.status == "restricted" and getattr(member, "is_member", False)
        )
        if is_member:
            await group_service.mark_membership(session, group, user, True)


async def available_tests_view(bot: Bot, session: AsyncSession, user: User) -> tuple[str, InlineKeyboardMarkup] | None:
    await refresh_test_memberships(bot, session, user)
    items = await attempt_service.available_tests_for_user(session, user)
    if not items:
        return None
    rows = []
    lines = [t("emp.my_tests.title"), ""]
    for test, attempt in items:
        if attempt is None:
            mark = "🆕"
        elif attempt.status == AttemptStatus.IN_PROGRESS:
            mark = "⏳"
        elif attempt.status == AttemptStatus.COMPLETED:
            mark = "✅"
        else:
            mark = "⌛"
        lines.append(f"{mark} <b>{esc(test.title)}</b> — {t('emp.my_tests.deadline', d=fmt_dt(test.deadline_at))}")
        rows.append([(f"{mark} {truncate(test.title, 40)}", EmpCB(a="card", id=test.id))])
    return "\n".join(lines), kb(*rows)
