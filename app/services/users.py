"""Employee registration and administration."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram.types import User as TgUser
from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Group, GroupMember, User, UserRole, UserStatus
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

PAGE_SIZE = 10


def is_admin_telegram_id(telegram_id: int) -> bool:
    """Server-side admin check: ONLY the ADMIN_TELEGRAM_IDS environment variable grants admin."""
    return telegram_id in get_settings().admin_ids


async def upsert_user(session: AsyncSession, tg_user: TgUser, *, private_chat: bool = False) -> User:
    """Create or refresh a user from Telegram data (idempotent, race-safe via ON CONFLICT)."""
    role = UserRole.ADMIN if is_admin_telegram_id(tg_user.id) else UserRole.EMPLOYEE
    now = utcnow()
    values = {
        "telegram_id": tg_user.id,
        "first_name": (tg_user.first_name or "")[:255] or None,
        "last_name": (tg_user.last_name or "")[:255] or None,
        "username": (tg_user.username or "")[:64] or None,
        "role": role,
        "last_seen_at": now,
        "language": "uz",
    }
    insert_values = dict(values)
    insert_values["status"] = UserStatus.ACTIVE if role == UserRole.ADMIN else UserStatus.PENDING
    insert_values["has_private_chat"] = private_chat
    stmt = insert(User).values(**insert_values)
    update_set = {k: stmt.excluded[k] for k in ("first_name", "last_name", "username", "role", "last_seen_at")}
    if private_chat:
        update_set["has_private_chat"] = True
        update_set["bot_blocked"] = False
    stmt = stmt.on_conflict_do_update(index_elements=[User.telegram_id], set_=update_set).returning(User.id)
    user_id = (await session.execute(stmt)).scalar_one()
    await session.commit()
    user = await session.get(User, user_id, populate_existing=True)
    assert user is not None
    if user.role == UserRole.ADMIN and user.status != UserStatus.ACTIVE:
        user.status = UserStatus.ACTIVE
        await session.commit()
    return user


async def sync_admin_roles(session: AsyncSession) -> None:
    """Align stored roles with ADMIN_TELEGRAM_IDS (called on startup)."""
    admin_ids = list(get_settings().admin_ids)
    await session.execute(
        update(User).where(User.telegram_id.in_(admin_ids)).values(role=UserRole.ADMIN, status=UserStatus.ACTIVE)
    )
    await session.execute(
        update(User)
        .where(User.telegram_id.not_in(admin_ids), User.role == UserRole.ADMIN)
        .values(role=UserRole.EMPLOYEE)
    )
    await session.commit()


async def set_full_name(session: AsyncSession, user: User, full_name: str) -> None:
    user.full_name = " ".join(full_name.split())[:255]
    await session.commit()


async def set_status(session: AsyncSession, user_id: int, status: UserStatus) -> User | None:
    user = await session.get(User, user_id)
    if user is None:
        return None
    if user.role == UserRole.ADMIN and status != UserStatus.ACTIVE:
        return user  # admins are controlled by the environment only
    user.status = status
    await session.commit()
    logger.info("User status changed", extra={"user_id": user_id, "status": status.value})
    return user


async def is_member_of_active_group(session: AsyncSession, user_id: int) -> bool:
    stmt = (
        select(func.count())
        .select_from(GroupMember)
        .join(Group, Group.id == GroupMember.group_id)
        .where(GroupMember.user_id == user_id, GroupMember.is_member.is_(True), Group.is_active.is_(True))
    )
    return bool((await session.execute(stmt)).scalar_one())


@dataclass
class UserFilter:
    status: str = "all"  # all | active | pending | inactive
    query: str = ""


def _filtered(stmt: Select, flt: UserFilter) -> Select:
    stmt = stmt.where(User.role == UserRole.EMPLOYEE)
    if flt.status in ("active", "pending", "inactive"):
        stmt = stmt.where(User.status == UserStatus(flt.status))
    q = flt.query.strip()
    if q:
        conditions = [
            User.full_name.ilike(f"%{q}%"),
            User.first_name.ilike(f"%{q}%"),
            User.last_name.ilike(f"%{q}%"),
            User.username.ilike(f"%{q.lstrip('@')}%"),
        ]
        if q.lstrip("-").isdigit():
            conditions.append(User.telegram_id == int(q))
        stmt = stmt.where(or_(*conditions))
    return stmt


async def list_employees(session: AsyncSession, flt: UserFilter, page: int) -> tuple[list[User], int]:
    total = (await session.execute(_filtered(select(func.count(User.id)), flt))).scalar_one()
    stmt = (
        _filtered(select(User), flt)
        .order_by(User.status, func.coalesce(User.full_name, User.first_name, User.username), User.id)
        .offset(page * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    return list((await session.execute(stmt)).scalars().all()), total


async def employee_counts(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(
        select(User.status, func.count(User.id)).where(User.role == UserRole.EMPLOYEE).group_by(User.status)
    )
    counts = {s.value: 0 for s in UserStatus}
    for status, n in rows:
        counts[status.value] = n
    counts["total"] = sum(counts.values())
    return counts


async def user_groups(session: AsyncSession, user_id: int) -> list[Group]:
    stmt = (
        select(Group)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .where(GroupMember.user_id == user_id, GroupMember.is_member.is_(True))
        .order_by(Group.title)
    )
    return list((await session.execute(stmt)).scalars().all())


async def admins(session: AsyncSession) -> list[User]:
    ids = list(get_settings().admin_ids)
    return list((await session.execute(select(User).where(User.telegram_id.in_(ids)))).scalars().all())
