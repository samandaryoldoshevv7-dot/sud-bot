"""Telegram group registration and membership tracking."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Group, GroupMember, User
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


async def get_by_chat(session: AsyncSession, chat_id: int) -> Group | None:
    return (await session.execute(select(Group).where(Group.chat_id == chat_id))).scalar_one_or_none()


async def register_group(session: AsyncSession, chat_id: int, title: str, admin: User | None) -> tuple[Group, bool]:
    """Register (or re-activate) a group. Returns ``(group, created)``."""
    group = await get_by_chat(session, chat_id)
    created = group is None
    if group is None:
        group = Group(chat_id=chat_id, title=title[:255] or str(chat_id), registered_by_id=admin.id if admin else None)
        session.add(group)
    else:
        group.is_active = True
        group.title = title[:255] or group.title
    await session.commit()
    logger.info("Group registered", extra={"chat_id": chat_id, "created": created})
    return group, created


async def set_active(session: AsyncSession, group_id: int, active: bool) -> Group | None:
    group = await session.get(Group, group_id)
    if group:
        group.is_active = active
        await session.commit()
    return group


async def delete_group(session: AsyncSession, group_id: int) -> None:
    group = await session.get(Group, group_id)
    if group:
        await session.delete(group)
        await session.commit()


async def update_title(session: AsyncSession, chat_id: int, title: str) -> None:
    group = await get_by_chat(session, chat_id)
    if group and title and group.title != title:
        group.title = title[:255]
        await session.commit()


async def migrate_chat(session: AsyncSession, old_chat_id: int, new_chat_id: int) -> None:
    """Group upgraded to supergroup: Telegram assigns a new chat id."""
    group = await get_by_chat(session, old_chat_id)
    if group and await get_by_chat(session, new_chat_id) is None:
        group.chat_id = new_chat_id
        await session.commit()


async def mark_membership(session: AsyncSession, group: Group, user: User, is_member: bool = True) -> None:
    stmt = insert(GroupMember).values(group_id=group.id, user_id=user.id, is_member=is_member)
    stmt = stmt.on_conflict_do_update(
        index_elements=[GroupMember.group_id, GroupMember.user_id],
        set_={"is_member": is_member, "updated_at": utcnow()},
    )
    await session.execute(stmt)
    await session.commit()


async def list_groups(session: AsyncSession, active_only: bool = False) -> list[tuple[Group, int]]:
    members = (
        select(GroupMember.group_id, func.count(GroupMember.id).label("n"))
        .where(GroupMember.is_member.is_(True))
        .group_by(GroupMember.group_id)
        .subquery()
    )
    stmt = select(Group, func.coalesce(members.c.n, 0)).outerjoin(members, members.c.group_id == Group.id)
    if active_only:
        stmt = stmt.where(Group.is_active.is_(True))
    stmt = stmt.order_by(Group.title)
    return [(g, int(n)) for g, n in await session.execute(stmt)]


async def group_members(session: AsyncSession, group_id: int, page: int, page_size: int = 15) -> tuple[list[User], int]:
    base = (
        select(User)
        .join(GroupMember, GroupMember.user_id == User.id)
        .where(GroupMember.group_id == group_id, GroupMember.is_member.is_(True))
    )
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = await session.execute(
        base.order_by(func.coalesce(User.full_name, User.first_name)).offset(page * page_size).limit(page_size)
    )
    return list(rows.scalars().all()), total
