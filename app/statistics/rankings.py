"""Employee rankings for a period (daily / weekly / monthly / all-time)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AttemptStatus, TestAttempt, User, UserRole, UserStatus
from app.utils.time import local_period_start, utcnow

PERIODS = ("day", "week", "month", "all")


@dataclass
class RankingRow:
    user: User
    attempts: int
    correct: int
    total: int
    total_seconds: int

    @property
    def percent(self) -> float:
        return round(self.correct / self.total * 100, 1) if self.total else 0.0


async def ranking(
    session: AsyncSession, period: str, min_attempts: int = 1, limit: int = 20, now: datetime | None = None
) -> list[RankingRow]:
    """Weighted accuracy (Σcorrect / Σquestions) over finished attempts in the period.

    Employees with fewer than ``min_attempts`` finished attempts are not ranked.
    Ties: more questions answered first, then less total time.
    """
    since = local_period_start(period, now or utcnow())
    finished_at = func.coalesce(TestAttempt.completed_at, TestAttempt.deadline_at)
    stmt = (
        select(
            TestAttempt.user_id,
            func.count(TestAttempt.id),
            func.sum(TestAttempt.correct_count),
            func.sum(TestAttempt.total_questions),
            func.coalesce(func.sum(TestAttempt.duration_seconds), 0),
        )
        .join(User, User.id == TestAttempt.user_id)
        .where(
            TestAttempt.status.in_([AttemptStatus.COMPLETED, AttemptStatus.EXPIRED]),
            User.role == UserRole.EMPLOYEE,
            User.status == UserStatus.ACTIVE,
        )
        .group_by(TestAttempt.user_id)
        .having(func.count(TestAttempt.id) >= min_attempts)
    )
    if since is not None:
        stmt = stmt.where(finished_at >= since)
    rows = (await session.execute(stmt)).all()
    if not rows:
        return []
    users = {u.id: u for u in (await session.execute(select(User).where(User.id.in_([r[0] for r in rows])))).scalars()}
    result = [RankingRow(users[uid], int(n), int(c or 0), int(t or 0), int(s or 0)) for uid, n, c, t, s in rows]
    result.sort(key=lambda r: (-r.percent, -r.total, r.total_seconds))
    return result[:limit]
