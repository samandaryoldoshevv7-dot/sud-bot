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
from app.utils.text import esc, pct, truncate
from app.utils.time import fmt_hours, fmt_span, utcnow

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


STATE_KEY = {
    attempt_service.MyTestState.NOT_STARTED: "emp.state.new",
    attempt_service.MyTestState.IN_PROGRESS: "emp.state.progress",
    attempt_service.MyTestState.COMPLETED: "emp.state.done",
    attempt_service.MyTestState.EXPIRED: "emp.state.expired",
}


async def available_tests_view(bot: Bot, session: AsyncSession, user: User) -> tuple[str, InlineKeyboardMarkup] | None:
    """📚 TESTLARIM: every test given to the employee with its state and one button each."""
    await refresh_test_memberships(bot, session, user)
    items = await attempt_service.my_tests(session, user)
    if not items:
        return None
    now = utcnow()
    lines = [t("emp.my_tests.title")]
    rows = []
    for item in items:
        test, attempt = item.test, item.attempt
        state = t(STATE_KEY[item.state])
        lines += [
            "",
            f"{state.split()[0]} <b>{esc(test.title)}</b>",
            t("emp.card.questions", n=test.question_count),
            t("emp.card.duration", d=fmt_hours(test.duration_seconds / 3600)),
            state.split(maxsplit=1)[1],
        ]
        if item.state == attempt_service.MyTestState.IN_PROGRESS and attempt is not None:
            lines.append(t("emp.card.progress", done=attempt.answered_count, total=attempt.total_questions))
            lines.append(t("emp.card.left", d=fmt_span((attempt.deadline_at - now).total_seconds())))
        elif attempt is not None and attempt.status != AttemptStatus.IN_PROGRESS:
            lines.append(t("emp.card.score", score=pct(attempt.score_percent)))
        title = truncate(test.title, 38)
        if item.can_start and item.state == attempt_service.MyTestState.NOT_STARTED:
            rows.append([(t("emp.list.btn.start", title=title), EmpCB(a="start", id=test.id))])
        elif item.can_start and item.state == attempt_service.MyTestState.IN_PROGRESS:
            rows.append([(t("emp.list.btn.continue", title=title), EmpCB(a="start", id=test.id))])
        elif item.can_start:
            rows.append([(t("emp.list.btn.retake", title=title), EmpCB(a="card", id=test.id))])
        elif attempt is not None and attempt.status != AttemptStatus.IN_PROGRESS:
            rows.append([(t("emp.list.btn.result", title=title), EmpCB(a="res", id=attempt.id))])
    return "\n".join(lines), kb(*rows)
