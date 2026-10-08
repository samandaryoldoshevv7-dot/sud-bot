"""Who gets a test: selected employees, taking a test away from someone, allowing a retake."""

from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AssignmentStatus,
    AttemptStatus,
    GroupMember,
    TestAssignment,
    TestAttempt,
    User,
    UserRole,
    UserStatus,
)
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


async def _upsert(session: AsyncSession, test_id: int, user_id: int, by: int | None) -> TestAssignment:
    row = (
        await session.execute(
            select(TestAssignment).where(TestAssignment.test_id == test_id, TestAssignment.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = TestAssignment(test_id=test_id, user_id=user_id, status=AssignmentStatus.ASSIGNED, assigned_by_id=by)
        session.add(row)
        await session.flush()
    return row


async def assign_users(session: AsyncSession, test_id: int, user_ids: list[int], by: int | None = None) -> int:
    for user_id in dict.fromkeys(user_ids):
        row = await _upsert(session, test_id, user_id, by)
        row.status = AssignmentStatus.ASSIGNED
    await session.commit()
    logger.info("Test assigned", extra={"test_id": test_id, "users": len(user_ids)})
    return len(user_ids)


async def assigned_user_ids(session: AsyncSession, test_id: int) -> list[int]:
    rows = await session.execute(
        select(TestAssignment.user_id).where(
            TestAssignment.test_id == test_id, TestAssignment.status == AssignmentStatus.ASSIGNED
        )
    )
    return list(rows.scalars().all())


async def remove_from_test(session: AsyncSession, test_id: int, user_id: int, by: int | None = None) -> None:
    """Take the test away from one employee; a running attempt is cancelled (answers are kept)."""
    row = await _upsert(session, test_id, user_id, by)
    row.status = AssignmentStatus.REMOVED
    row.retake_allowed = False
    now = utcnow()
    await session.execute(
        update(TestAttempt)
        .where(
            TestAttempt.test_id == test_id,
            TestAttempt.user_id == user_id,
            TestAttempt.status == AttemptStatus.IN_PROGRESS,
        )
        .values(status=AttemptStatus.CANCELLED, cancelled_reason="removed_by_admin", completed_at=now)
    )
    await session.commit()
    logger.info("Test taken away from employee", extra={"test_id": test_id, "user_id": user_id})


async def allow_retake(session: AsyncSession, test_id: int, user_id: int, by: int | None = None) -> None:
    row = await _upsert(session, test_id, user_id, by)
    row.status = AssignmentStatus.ASSIGNED
    row.retake_allowed = True
    await session.commit()
    logger.info("Retake allowed", extra={"test_id": test_id, "user_id": user_id})


async def active_employees(session: AsyncSession, group_id: int | None = None) -> list[User]:
    stmt = select(User).where(User.role == UserRole.EMPLOYEE, User.status == UserStatus.ACTIVE)
    if group_id is not None:
        stmt = stmt.join(GroupMember, GroupMember.user_id == User.id).where(
            GroupMember.group_id == group_id, GroupMember.is_member.is_(True)
        )
    from sqlalchemy import func

    stmt = stmt.order_by(func.coalesce(User.full_name, User.first_name, User.username), User.id)
    return list((await session.execute(stmt)).scalars().all())


async def audience_users(session: AsyncSession, test) -> list[User]:
    """Active employees the test was given to (all / group members / selected), minus removed ones."""
    from app.models import TestAudience

    rows = list((await session.execute(select(TestAssignment).where(TestAssignment.test_id == test.id))).scalars())
    removed = {a.user_id for a in rows if a.status == AssignmentStatus.REMOVED}
    assigned = {a.user_id for a in rows if a.status == AssignmentStatus.ASSIGNED}
    users: dict[int, User] = {}
    if test.audience != TestAudience.USERS:
        for user in await active_employees(session, test.group_id):
            users[user.id] = user
    if assigned - users.keys():
        extra = select(User).where(
            User.id.in_(assigned - users.keys()), User.role == UserRole.EMPLOYEE, User.status == UserStatus.ACTIVE
        )
        for user in (await session.execute(extra)).scalars():
            users[user.id] = user
    return [u for uid, u in users.items() if uid not in removed]
